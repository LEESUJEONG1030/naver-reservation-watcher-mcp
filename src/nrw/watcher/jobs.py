"""Handles the ``jobs`` queue - ad-hoc requests coming from the MCP server
(check_availability / reserve_now / approve_reservation / decline)."""
from __future__ import annotations

import logging
from datetime import UTC, datetime

from playwright.async_api import BrowserContext

from nrw import db
from nrw.browser.base_adapter import BookingAdapter
from nrw.config import Config
from nrw.models import JobStatus, JobType, ReservationValidationError, SlotState, WatchStatus
from nrw.notifier.base import Notifier
from nrw.watcher.core import _notify, attempt_reservation, compute_next_check_at

log = logging.getLogger("nrw.watcher.jobs")


async def handle_job(cfg: Config, context: BrowserContext, adapter: BookingAdapter, notifier: Notifier, job: dict) -> None:
    job_id = job["id"]
    try:
        if job["type"] == JobType.CHECK_NOW:
            result = await _handle_check_now(cfg, context, adapter, job)
        elif job["type"] == JobType.RESERVE_NOW:
            result = await _handle_reserve_now(cfg, context, adapter, notifier, job)
        elif job["type"] == JobType.APPROVE:
            result = await _handle_approve(cfg, context, adapter, notifier, job)
        elif job["type"] == JobType.DECLINE:
            result = await _handle_decline(job)
        else:
            result = {"ok": False, "error": f"unknown job type {job['type']}"}
        db.complete_job(job_id, status=JobStatus.DONE, result=result)
    except Exception as e:
        log.exception("Job %s failed", job_id)
        db.complete_job(job_id, status=JobStatus.ERROR, result={"ok": False, "error": str(e)})


def _payload_or_watch(job: dict) -> dict:
    payload = dict(job.get("payload") or {})
    if job.get("watch_id") and (not payload.get("store_url") and not payload.get("store_name")):
        watch = db.get_watch(job["watch_id"])
        if watch:
            payload.setdefault("store_name", watch["store_name"])
            payload.setdefault("store_url", watch["store_url"])
            payload.setdefault("target_date", watch["target_date"])
            payload.setdefault("party_size", watch["party_size"])
    return payload


async def _handle_check_now(cfg: Config, context: BrowserContext, adapter: BookingAdapter, job: dict) -> dict:
    payload = _payload_or_watch(job)
    store = await adapter.resolve_store(context, payload.get("store_name"), payload.get("store_url"))
    snapshot = await adapter.check_availability(
        context, store, payload["target_date"], payload["party_size"]
    )

    watch_id = job.get("watch_id")
    if watch_id:
        watch = db.get_watch(watch_id)
        # NEEDS_HUMAN 상태는 사용자가 직접 확인을 완료했다는 뜻으로 이 ad-hoc 체크가
        # 성공했을 때만 다시 정상 감시로 복귀시킨다 (자동으로는 재시도하지 않음).
        if watch and watch["status"] == WatchStatus.NEEDS_HUMAN:
            resumed_status = WatchStatus.WAITING_OPEN if snapshot.page_state == SlotState.BEFORE_OPEN else WatchStatus.WATCHING
            db.update_watch(
                watch_id, status=resumed_status, consecutive_errors=0,
                next_check_at=compute_next_check_at(cfg, snapshot, 0),
            )
            db.add_event(watch_id=watch_id, level="INFO", message="사용자 확인 완료로 감시를 재개합니다.")

    return {
        "ok": True,
        "store_name": store.name,
        "page_state": str(snapshot.page_state),
        "available_times": snapshot.available_times(),
        "open_at_text": snapshot.open_at_text,
    }


async def _handle_reserve_now(cfg: Config, context: BrowserContext, adapter: BookingAdapter, notifier: Notifier, job: dict) -> dict:
    payload = _payload_or_watch(job)
    time_ = payload.get("time")
    if not time_:
        return {"ok": False, "error": "예약할 time(HH:MM)이 필요합니다"}

    store = await adapter.resolve_store(context, payload.get("store_name"), payload.get("store_url"))

    if payload.get("dry_run"):
        # 실제 예약을 절대 완료하지 않고 최종 확인 버튼 직전까지만 점검한다 -
        # DB에 예약 기록을 남기지도, "예약 완료" 알림을 보내지도 않는다.
        result = await adapter.dry_run_reserve(
            context, store, payload["target_date"], time_, payload["party_size"]
        )
        return {"ok": result.ok, "dry_run": True, **vars(result)}

    try:
        snapshot = await adapter.check_availability(
            context, store, payload["target_date"], payload["party_size"]
        )
    except ReservationValidationError:
        raise
    except Exception as e:
        return {"ok": False, "error": f"가용성 확인 중 오류: {e}"}

    matching = [s for s in snapshot.slots if s.time == time_ and s.available]
    if not matching:
        return {"ok": False, "status": "UNAVAILABLE", "error": f"'{time_}' 시간은 현재 예약할 수 없습니다."}

    outcome = await attempt_reservation(
        cfg, context, adapter, notifier,
        store=store, target_date=payload["target_date"], time_=time_,
        party_size=payload["party_size"], watch_id=job.get("watch_id"),
    )
    if outcome.status == "RESERVED" and job.get("watch_id"):
        db.update_watch(job["watch_id"], status=WatchStatus.RESERVED,
                         reservation_id=outcome.reservation["reservation_id"], next_check_at=None)
    return {"ok": outcome.status == "RESERVED", **outcome.as_dict()}


async def _handle_approve(cfg: Config, context: BrowserContext, adapter: BookingAdapter, notifier: Notifier, job: dict) -> dict:
    watch_id = job.get("watch_id")
    if not watch_id:
        return {"ok": False, "error": "approve job은 watch_id가 필요합니다"}
    watch = db.get_watch(watch_id)
    if not watch:
        return {"ok": False, "error": "해당 watch를 찾을 수 없습니다"}
    if watch["status"] != WatchStatus.AWAITING_APPROVAL or not watch.get("pending_candidate"):
        return {"ok": False, "error": "현재 승인 대기 상태가 아닙니다"}

    candidate = watch["pending_candidate"]
    store = await adapter.resolve_store(context, watch["store_name"], watch["store_url"])

    # 승인 버튼을 눌렀다고 바로 클릭하지 않는다 - 알림이 전송된 후 시간이 지났을 수 있으므로
    # 예약 시도 직전에 그 슬롯이 여전히 유효한지 즉시 다시 조회한다.
    try:
        recheck = await adapter.check_availability(
            context, store, watch["target_date"], candidate["party_size"]
        )
    except Exception as e:
        return {"ok": False, "error": f"재조회 중 오류: {e}"}

    if candidate["time"] not in recheck.available_times():
        message = (
            f"{watch['store_name']} {watch['target_date']} {candidate['time']} 자리는 승인 확인 중 "
            f"이미 사라졌습니다. 계속 감시를 이어갑니다."
        )
        db.update_watch(watch_id, status=WatchStatus.WATCHING, pending_candidate=None,
                         next_check_at=datetime.now(UTC).isoformat())
        db.add_event(watch_id=watch_id, level="WARN", message=message)
        await _notify(notifier, f"{watch['store_name']} - 예약 불가", message, watch_id)
        return {"ok": False, "status": "UNAVAILABLE", "message": message}

    outcome = await attempt_reservation(
        cfg, context, adapter, notifier,
        store=store, target_date=watch["target_date"], time_=candidate["time"],
        party_size=candidate["party_size"], watch_id=watch_id,
    )

    if outcome.status == "RESERVED":
        db.update_watch(watch_id, status=WatchStatus.RESERVED,
                         reservation_id=outcome.reservation["reservation_id"],
                         pending_candidate=None, next_check_at=None)
    elif outcome.status in ("AWAITING_APPROVAL", "NEEDS_HUMAN"):
        db.update_watch(
            watch_id,
            status=WatchStatus.AWAITING_APPROVAL if outcome.status == "AWAITING_APPROVAL" else WatchStatus.NEEDS_HUMAN,
        )
    else:
        db.update_watch(watch_id, status=WatchStatus.WATCHING, pending_candidate=None,
                         next_check_at=datetime.now(UTC).isoformat())
    return {"ok": outcome.status == "RESERVED", **outcome.as_dict()}


async def _handle_decline(job: dict) -> dict:
    watch_id = job.get("watch_id")
    if not watch_id:
        return {"ok": False, "error": "decline job은 watch_id가 필요합니다"}
    watch = db.get_watch(watch_id)
    if not watch:
        return {"ok": False, "error": "해당 watch를 찾을 수 없습니다"}

    # 방금 거절한 후보를 last_snapshot에 기록해둔다 - 같은 후보가 계속 열려 있는
    # 동안은 watcher core가 다시 AWAITING_APPROVAL로 되돌리지 않도록 하기 위함
    # (그러지 않으면 다음 확인 주기에 똑같은 후보로 즉시 재승인 요청이 뜬다).
    declined_time = (watch.get("pending_candidate") or {}).get("time")
    snapshot = dict(watch.get("last_snapshot") or {})
    snapshot["declined_candidate"] = declined_time

    db.update_watch(watch_id, status=WatchStatus.WATCHING, pending_candidate=None,
                     last_snapshot=snapshot, next_check_at=datetime.now(UTC).isoformat())
    db.add_event(watch_id=watch_id, level="INFO", message=f"사용자가 예약 제안을 거절했습니다 ({declined_time}).")
    return {"ok": True}


async def poll_and_handle_jobs(cfg: Config, context_factory, adapter: BookingAdapter, notifier: Notifier) -> int:
    """PENDING job이 있으면 처리한다. 브라우저가 필요한 job만 persistent_context를 연다."""
    jobs = db.claim_pending_jobs(limit=5)
    if not jobs:
        return 0
    async with context_factory() as context:
        for job in jobs:
            await handle_job(cfg, context, adapter, notifier, job)
    return len(jobs)
