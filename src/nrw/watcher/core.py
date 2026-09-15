"""Per-watch check/decide/act logic, and the shared "attempt a reservation
safely" routine reused by AUTO auto-trigger, ASK approval, and the ad-hoc
``reserve_now`` job. Adapter-agnostic: works identically against
``NaverBookingAdapter`` and ``MockBookingAdapter``.
"""
from __future__ import annotations

import asyncio
import logging
import random
from dataclasses import asdict
from datetime import UTC, datetime, timedelta

from playwright.async_api import BrowserContext

from nrw import coupon_utils, db, time_utils
from nrw.browser.base_adapter import BookingAdapter, ResolvedStore
from nrw.config import Config
from nrw.models import (
    AvailabilitySnapshot,
    CouponPick,
    CouponUnavailableError,
    GateBlocked,
    HumanVerificationRequired,
    JobType,
    NotOpenYetError,
    ReservationMode,
    ReservationValidationError,
    SlotState,
    SlotUnavailableError,
    WatchStatus,
)
from nrw.notifier.base import NotifyAction, NotifyMessage, Notifier

log = logging.getLogger("nrw.watcher.core")


def _jittered(lo: int, hi: int) -> int:
    return random.randint(lo, hi)


def compute_next_check_at(cfg: Config, snapshot: AvailabilitySnapshot | None, consecutive_errors: int) -> str:
    now = datetime.now(UTC)
    p = cfg.polling
    if consecutive_errors > 0:
        backoff = min(p.error_backoff_base_sec * (2 ** (consecutive_errors - 1)), p.error_backoff_max_sec)
        delay = backoff + random.uniform(0, backoff * 0.2)
        return (now + timedelta(seconds=delay)).isoformat()

    if snapshot and snapshot.open_at_iso:
        try:
            open_at = datetime.fromisoformat(snapshot.open_at_iso)
            if open_at.tzinfo is None:
                open_at = open_at.replace(tzinfo=UTC)
            seconds_until_open = (open_at - now).total_seconds()
            if 0 <= seconds_until_open <= p.tight_poll_before_open_sec:
                return (now + timedelta(seconds=_jittered(p.tight_interval_min_sec, p.tight_interval_max_sec))).isoformat()
        except ValueError:
            pass

    return (now + timedelta(seconds=_jittered(p.interval_min_sec, p.interval_max_sec))).isoformat()


def _resolved_store_from_watch(watch: dict) -> tuple[str | None, str | None]:
    return watch.get("store_name"), watch.get("store_url")


async def _notify(notifier: Notifier, title: str, body: str, watch_id: int | None,
                   actions: list[NotifyAction] | None = None) -> None:
    await asyncio.to_thread(
        notifier.notify, NotifyMessage(title=title, body=body, actions=actions or [], watch_id=watch_id)
    )


async def get_coupon_pick(
    context: BrowserContext, adapter: BookingAdapter, store: ResolvedStore, watch: dict,
) -> CouponPick:
    """이 업체의 현재 쿠폰을 조회하고, 무료로 받을 수 있는(게이트 없는) 쿠폰은 바로
    다운로드한 뒤, 이 예약 조건(예상 결제금액)에 가장 유리한 쿠폰을 고른다.
    실패해도 예약 흐름 자체를 막지 않도록 예외를 삼키고 빈 결과를 돌려준다."""
    try:
        coupon_ctx = await adapter.list_coupons(
            context, store, watch["target_date"], watch["party_size"]
        )
        if coupon_ctx.coupons:
            coupon_ctx.coupons = await adapter.download_free_coupons(context, store, coupon_ctx.coupons)
        return coupon_utils.pick_best(coupon_ctx.coupons, coupon_ctx.estimated_amount)
    except Exception:
        log.exception("쿠폰 조회 실패 (watch=%s) - 쿠폰 없이 계속 진행", watch.get("id"))
        return CouponPick(coupon=None, estimated_discount=0)


class ReservationAttemptResult:
    def __init__(self, status: str, message: str, reservation: dict | None = None):
        self.status = status  # RESERVED | AWAITING_APPROVAL | NEEDS_HUMAN | UNAVAILABLE | ERROR
        self.message = message
        self.reservation = reservation

    def as_dict(self) -> dict:
        return {"status": self.status, "message": self.message, "reservation": self.reservation}


async def attempt_reservation(
    cfg: Config,
    context: BrowserContext,
    adapter: BookingAdapter,
    notifier: Notifier,
    *,
    store: ResolvedStore,
    target_date: str,
    time_: str,
    party_size: int,
    watch_id: int | None,
) -> ReservationAttemptResult:
    """결제/동의/캡차 안전 게이트 + 클릭 직전 재검증을 항상 적용하는 예약 시도.

    AUTO 자동실행, ASK 승인 후 실행, ad-hoc reserve_now 모두 이 함수를 통해서만
    실제 예약을 시도한다. 쿠폰(무료 다운로드 + 최적 선택 + 클릭 직전 재검증)도
    여기서 한 곳에만 통합되어 있어 모든 예약 경로가 동일하게 처리한다."""
    watch = {"id": watch_id, "target_date": target_date, "party_size": party_size}
    pick = await get_coupon_pick(context, adapter, store, watch)
    if pick.coupon:
        db.add_event(
            watch_id=watch_id, level="INFO",
            message=f"쿠폰 적용 예정: {pick.coupon.name} (예상 할인 {pick.estimated_discount:,}원)",
        )
    if pick.gated_candidates:
        names = ", ".join(c.name for c in pick.gated_candidates)
        db.add_event(watch_id=watch_id, level="INFO", message=f"확인 필요해 자동 적용하지 않은 쿠폰: {names}")

    slot_desc = f"{store.name} {target_date} {time_} {party_size}명"

    try:
        result = await adapter.reserve(
            context, store, target_date, time_, party_size,
            coupon_id=pick.coupon.coupon_id if pick.coupon else None,
        )
    except CouponUnavailableError as e:
        message = (
            f"{slot_desc} - 선택했던 쿠폰 '{e.coupon_name}'을(를) 예약 직전에 더 이상 사용할 수 없게 "
            f"되어 할인 없이 강행하지 않고 잠시 멈췄습니다. ({e.detail}) 그대로 진행하려면 다시 "
            f"시도해주세요."
        )
        db.add_event(watch_id=watch_id, level="WARN", message=message, detail={"slot": time_})
        await _notify(notifier, f"{store.name} - 쿠폰을 사용할 수 없게 되었습니다", message, watch_id)
        return ReservationAttemptResult("AWAITING_APPROVAL", message)
    except NotOpenYetError as e:
        message = f"{slot_desc} - 아직 예약이 열리지 않았습니다: {e}"
        db.add_event(watch_id=watch_id, level="INFO", message=message, detail={"slot": time_})
        return ReservationAttemptResult("UNAVAILABLE", message)
    except SlotUnavailableError as e:
        message = f"{slot_desc} - 예약 시도 중 이 슬롯이 선점되어 더 이상 예약할 수 없습니다: {e}"
        db.add_event(watch_id=watch_id, level="WARN", message=message, detail={"slot": time_})
        await _notify(notifier, f"{store.name} 예약 실패", f"{message} 감시는 계속됩니다.", watch_id)
        return ReservationAttemptResult("UNAVAILABLE", message)
    except ReservationValidationError as e:
        accepted_terms = getattr(e, "accepted_terms", [])
        applied_coupon_name = getattr(e, "applied_coupon_name", None)
        message = (
            f"{slot_desc} - 클릭 직전 재검증에 실패해 자동으로 강행하지 않고 중단했습니다. "
            f"감시는 계속됩니다. 원인: {e}"
        )
        log.error("Reservation validation failed for watch %s: %s", watch_id, e)
        db.add_event(
            watch_id=watch_id, level="ERROR", message=message,
            detail={"slot": time_, "accepted_terms": accepted_terms, "coupon_name": applied_coupon_name},
        )
        await _notify(notifier, f"{store.name} 예약 실패", message, watch_id)
        return ReservationAttemptResult("ERROR", message)
    except GateBlocked as e:
        accepted_terms = getattr(e, "accepted_terms", [])
        applied_coupon_name = getattr(e, "applied_coupon_name", None)
        message = (
            f"{slot_desc} - 결제/선결제/보증금/취소수수료/추가 동의가 필요해 자동으로 진행하지 "
            f"않았습니다. 브라우저 창에서 직접 확인 후 완료해주세요. ({e.detail})"
        )
        db.add_event(
            watch_id=watch_id, level="WARN", message=message,
            detail={"slot": time_, "reason": str(e.reason), "accepted_terms": accepted_terms, "coupon_name": applied_coupon_name},
        )
        await _notify(notifier, f"{store.name} - 확인이 필요합니다", message, watch_id)
        await asyncio.sleep(cfg.polling.human_pause_sec)
        return ReservationAttemptResult("AWAITING_APPROVAL", message)
    except HumanVerificationRequired as e:
        accepted_terms = getattr(e, "accepted_terms", [])
        message = f"{slot_desc} - 사용자 확인이 필요합니다. 브라우저 창에서 직접 확인해주세요. ({e})"
        db.add_event(
            watch_id=watch_id, level="WARN", message=message,
            detail={"slot": time_, "accepted_terms": accepted_terms},
        )
        await _notify(notifier, f"{store.name} - 사용자 확인이 필요합니다", message, watch_id)
        await asyncio.sleep(cfg.polling.human_pause_sec)
        return ReservationAttemptResult("NEEDS_HUMAN", message)
    except Exception as e:
        log.exception("Unexpected error during reserve() for watch %s", watch_id)
        message = f"{slot_desc} - 예약 시도 중 예상치 못한 오류가 발생해 중단했습니다. 감시는 계속됩니다. 원인: {e}"
        db.add_event(watch_id=watch_id, level="ERROR", message=message, detail={"slot": time_})
        await _notify(notifier, f"{store.name} 예약 실패", message, watch_id)
        return ReservationAttemptResult("ERROR", message)

    reservation_id = db.create_reservation(
        watch_id=watch_id,
        store_name=result.store_name,
        date=result.date,
        time_=result.time,
        party_size=result.party_size,
        naver_reservation_no=result.naver_reservation_no,
        raw_confirmation=result.raw_confirmation,
        coupon_name=result.coupon_name,
        coupon_discount=result.coupon_discount,
    )
    coupon_line = ""
    if result.coupon_name:
        coupon_line = f" 쿠폰 '{result.coupon_name}' 적용" + (
            f" (할인 {result.coupon_discount:,}원)" if result.coupon_discount else ""
        )
    terms_line = f" 필수 동의 자동 체크: {', '.join(result.accepted_terms)}" if result.accepted_terms else ""
    success_message = (
        f"{result.store_name} {result.date} {result.time} {result.party_size}명 예약이 완료되었습니다."
        + (f" (예약번호 {result.naver_reservation_no})" if result.naver_reservation_no else "")
        + coupon_line + terms_line
    )
    db.add_event(
        watch_id=watch_id, level="INFO", message=f"예약 성공: {success_message}",
        detail={
            "reservation_id": reservation_id, "slot": result.time, "party_size": result.party_size,
            "naver_reservation_no": result.naver_reservation_no,
            "coupon_name": result.coupon_name, "coupon_discount": result.coupon_discount,
            "accepted_terms": result.accepted_terms,
        },
    )
    await _notify(notifier, "예약 완료", success_message, watch_id)
    return ReservationAttemptResult(
        "RESERVED", "예약이 완료되었습니다.",
        {
            "reservation_id": reservation_id,
            "store_name": result.store_name,
            "date": result.date,
            "time": result.time,
            "party_size": result.party_size,
            "naver_reservation_no": result.naver_reservation_no,
            "coupon_name": result.coupon_name,
            "coupon_discount": result.coupon_discount,
            "accepted_terms": result.accepted_terms,
        },
    )


async def process_one_watch(
    cfg: Config,
    context: BrowserContext,
    adapter: BookingAdapter,
    notifier: Notifier,
    watch: dict,
) -> None:
    watch_id = watch["id"]
    now = datetime.now(UTC)

    watch_end_at = datetime.fromisoformat(watch["watch_end_at"])
    if watch_end_at.tzinfo is None:
        watch_end_at = watch_end_at.replace(tzinfo=UTC)
    if now > watch_end_at:
        db.update_watch(watch_id, status=WatchStatus.EXPIRED, next_check_at=None)
        db.add_event(watch_id=watch_id, level="INFO", message="감시 종료일이 지나 감시를 종료합니다.")
        return

    store_name, store_url = _resolved_store_from_watch(watch)
    try:
        store = await adapter.resolve_store(context, store_name, store_url)
        snapshot = await adapter.check_availability(
            context, store, watch["target_date"], watch["party_size"]
        )
    except HumanVerificationRequired as e:
        # NEEDS_HUMAN 상태는 due_watches 활성 목록에서 제외되므로, 사용자가 직접 확인을
        # 완료하고 check_availability 를 다시 호출하기 전까지 감시가 자동으로 재시도되지
        # 않는다 (CAPTCHA/인증 화면을 반복해서 두드리지 않음).
        db.update_watch(watch_id, status=WatchStatus.NEEDS_HUMAN, next_check_at=None)
        db.add_event(watch_id=watch_id, level="WARN", message=f"사용자 확인 필요: {e}")
        await _notify(notifier, f"{watch['store_name']} - 확인 필요", str(e), watch_id)
        return
    except Exception as e:
        consecutive_errors = watch["consecutive_errors"] + 1
        log.warning("check_availability failed for watch %s: %s", watch_id, e)
        db.add_event(watch_id=watch_id, level="ERROR", message=f"확인 중 오류: {e}")
        db.update_watch(
            watch_id,
            consecutive_errors=consecutive_errors,
            last_message=str(e),
            next_check_at=compute_next_check_at(cfg, None, consecutive_errors),
        )
        return

    prev_snapshot = watch.get("last_snapshot") or {}
    prev_state = prev_snapshot.get("page_state")
    prev_available = set(prev_snapshot.get("available_times") or [])
    now_available = set(snapshot.available_times())

    if prev_state == SlotState.BEFORE_OPEN and snapshot.page_state != SlotState.BEFORE_OPEN:
        db.add_event(watch_id=watch_id, level="INFO", message="예약 오픈이 감지되었습니다.")
    newly_available = now_available - prev_available
    if newly_available and prev_state not in (None,) and prev_state != SlotState.BEFORE_OPEN:
        db.add_event(
            watch_id=watch_id, level="INFO",
            message=f"새로 열린 시간 감지(취소자리 발생 가능): {sorted(newly_available)}",
        )

    candidate = None
    if snapshot.page_state != SlotState.BEFORE_OPEN:
        candidate = time_utils.pick_best_candidate(
            list(now_available), watch["time_min"], watch["time_max"], watch.get("time_priority")
        )

    snapshot_dict = {
        "page_state": str(snapshot.page_state),
        "available_times": sorted(now_available),
        "open_at_text": snapshot.open_at_text,
        "open_at_iso": snapshot.open_at_iso,
        "checked_at": snapshot.checked_at,
        "notified_candidate": prev_snapshot.get("notified_candidate"),
        "declined_candidate": prev_snapshot.get("declined_candidate"),
    }

    base_updates = dict(
        consecutive_errors=0,
        last_checked_at=snapshot.checked_at,
        last_message=None,
    )

    if candidate is None:
        status = WatchStatus.WAITING_OPEN if snapshot.page_state == SlotState.BEFORE_OPEN else WatchStatus.WATCHING
        # 후보가 사라졌으므로 알림/거절 기록도 함께 초기화한다 - 나중에 같은 시간이
        # 마감->재오픈(취소자리 발생)으로 다시 나타나면 새로 알림을 보낼 수 있어야 한다.
        snapshot_dict["notified_candidate"] = None
        snapshot_dict["declined_candidate"] = None
        db.update_watch(
            watch_id, status=status, last_snapshot=snapshot_dict,
            next_check_at=compute_next_check_at(cfg, snapshot, 0), **base_updates,
        )
        return

    mode = watch["mode"]
    already_notified = snapshot_dict.get("notified_candidate") == candidate

    if mode == ReservationMode.NOTIFY:
        if not already_notified:
            pick = await get_coupon_pick(context, adapter, store, watch)
            coupon_line = coupon_utils.format_coupon_summary(pick)
            body = (
                f"{watch['store_name']} {watch['target_date']} {candidate} {watch['party_size']}명 "
                f"자리가 있습니다. (알림 전용 - 직접 예약해주세요)"
            )
            if coupon_line:
                body += f"\n{coupon_line}"
            await _notify(notifier, f"{watch['store_name']} 자리 발견", body, watch_id)
            snapshot_dict["notified_candidate"] = candidate
            db.add_event(watch_id=watch_id, level="INFO", message=f"알림 발송: {candidate} 이용 가능")
        db.update_watch(
            watch_id, status=WatchStatus.WATCHING, last_snapshot=snapshot_dict,
            next_check_at=compute_next_check_at(cfg, snapshot, 0), **base_updates,
        )
        return

    if mode == ReservationMode.ASK:
        if snapshot_dict.get("declined_candidate") == candidate:
            # 사용자가 바로 이 후보를 이미 "이번 자리 넘기기"로 거절했다 - 같은 후보가
            # 계속 열려 있는 동안은 다시 승인 대기 상태로 되돌리지 않는다 (감시만 계속).
            db.update_watch(
                watch_id, status=WatchStatus.WATCHING, last_snapshot=snapshot_dict,
                next_check_at=compute_next_check_at(cfg, snapshot, 0), **base_updates,
            )
            return

        if not already_notified:
            pick = await get_coupon_pick(context, adapter, store, watch)
            coupon_line = coupon_utils.format_coupon_summary(pick)
            body = (
                f"{watch['store_name']} {watch['target_date']} {candidate} {watch['party_size']}명 "
                f"자리가 생겼습니다. 예약할까요?"
            )
            if coupon_line:
                body += f"\n{coupon_line}"
            await _notify(
                notifier, f"{watch['store_name']} 자리 발견", body, watch_id,
                actions=[NotifyAction("예약하기", "approve"), NotifyAction("이번 자리 넘기기", "decline")],
            )
            snapshot_dict["notified_candidate"] = candidate
            db.add_event(watch_id=watch_id, level="INFO", message=f"승인 대기 알림 발송: {candidate}")
        db.update_watch(
            watch_id, status=WatchStatus.AWAITING_APPROVAL, last_snapshot=snapshot_dict,
            pending_candidate={"time": candidate, "party_size": watch["party_size"]},
            next_check_at=None, **base_updates,
        )
        return

    # AUTO
    db.add_event(watch_id=watch_id, level="INFO", message=f"AUTO 모드: {candidate} 예약 시도")
    outcome = await attempt_reservation(
        cfg, context, adapter, notifier,
        store=store, target_date=watch["target_date"], time_=candidate,
        party_size=watch["party_size"], watch_id=watch_id,
    )
    if outcome.status == "RESERVED":
        db.update_watch(
            watch_id, status=WatchStatus.RESERVED, last_snapshot=snapshot_dict,
            reservation_id=outcome.reservation["reservation_id"], next_check_at=None,
            pending_candidate=None, **base_updates,
        )
    elif outcome.status in ("AWAITING_APPROVAL", "NEEDS_HUMAN"):
        db.update_watch(
            watch_id,
            status=WatchStatus.AWAITING_APPROVAL if outcome.status == "AWAITING_APPROVAL" else WatchStatus.NEEDS_HUMAN,
            last_snapshot=snapshot_dict,
            pending_candidate={"time": candidate, "party_size": watch["party_size"]},
            next_check_at=None, **base_updates,
        )
    else:
        # UNAVAILABLE (슬롯이 선점됨/아직 안 열림) 또는 ERROR(재검증 실패 등) - 감시
        # 계속. 알림/이벤트 로그는 attempt_reservation()이 원인과 함께 이미 남겼다.
        snapshot_dict["notified_candidate"] = None
        db.update_watch(
            watch_id, status=WatchStatus.WATCHING, last_snapshot=snapshot_dict,
            next_check_at=compute_next_check_at(cfg, snapshot, 0), **base_updates,
        )


async def run_due_watches(cfg: Config, context: BrowserContext, adapter: BookingAdapter, notifier: Notifier) -> int:
    now_iso = datetime.now(UTC).isoformat()
    due = db.due_watches(now_iso)
    for watch in due:
        try:
            await process_one_watch(cfg, context, adapter, notifier, watch)
        except Exception:
            log.exception("Unhandled error while processing watch %s", watch["id"])
            db.update_watch(
                watch["id"],
                consecutive_errors=watch["consecutive_errors"] + 1,
                next_check_at=compute_next_check_at(cfg, None, watch["consecutive_errors"] + 1),
            )
    return len(due)
