"""FastMCP server exposing reservation-watch tools to Claude.

This process never touches Playwright directly - it only reads/writes the
shared SQLite DB and, for on-demand actions (check_availability/reserve_now/
approve_reservation), enqueues a job for the always-on watcher service and
waits for the result. If the watcher isn't running, that's reported clearly
instead of hanging.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastmcp import FastMCP

from nrw import db
from nrw.config import CONFIG
from nrw.models import JobStatus, JobType, ReservationMode

mcp = FastMCP(
    name="naver-reservation-watcher-mcp",
    instructions=(
        "네이버 예약(식당/카페/호텔 등)의 빈자리를 백그라운드에서 감시하고, "
        "조건에 맞으면 자동/승인후 예약하거나 알림을 주는 도구입니다. "
        "감시가 실제로 동작하려면 watcher 서비스(nrw-watcher)가 별도로 실행 중이어야 합니다."
    ),
)

VALID_MODES = {m.value for m in ReservationMode}


def _watcher_alive() -> bool:
    return db.is_watcher_alive(CONFIG.watcher.heartbeat_stale_after_sec)


def _watcher_not_running_message() -> str:
    return (
        "watcher 서비스가 실행 중이지 않은 것 같습니다 (하트비트 없음/오래됨). "
        "scripts/run_watcher.ps1 을 실행하거나 Windows 작업 스케줄러에 등록된 "
        "'NaverReservationWatcher' 작업이 켜져 있는지 확인해주세요."
    )


def _validate_date(value: str, field: str) -> None:
    try:
        datetime.strptime(value, "%Y-%m-%d")
    except ValueError as e:
        raise ValueError(f"{field}는 YYYY-MM-DD 형식이어야 합니다: {value!r}") from e


def _validate_time(value: str, field: str) -> None:
    try:
        datetime.strptime(value, "%H:%M")
    except ValueError as e:
        raise ValueError(f"{field}는 HH:MM 형식이어야 합니다: {value!r}") from e


def _format_watch(w: dict) -> dict:
    return {
        "id": w["id"],
        "store_name": w["store_name"],
        "store_url": w["store_url"],
        "target_date": w["target_date"],
        "time_min": w["time_min"],
        "time_max": w["time_max"],
        "time_priority": w.get("time_priority"),
        "party_size": w["party_size"],
        "watch_start_at": w["watch_start_at"],
        "watch_end_at": w["watch_end_at"],
        "mode": w["mode"],
        "status": w["status"],
        "last_checked_at": w.get("last_checked_at"),
        "last_message": w.get("last_message"),
        "pending_candidate": w.get("pending_candidate"),
        "reservation_id": w.get("reservation_id"),
    }


@mcp.tool
def add_reservation_watch(
    store_name: str,
    target_date: str,
    time_min: str,
    time_max: str,
    party_size: int,
    mode: str = "ASK",
    store_url: str | None = None,
    time_priority: list[str] | None = None,
    watch_start_at: str | None = None,
    watch_end_at: str | None = None,
) -> dict:
    """새 예약 감시를 등록합니다.

    Args:
        store_name: 업체명 (예: "OO식당"). store_url이 없으면 이 이름으로 네이버에서 검색합니다.
        store_url: 네이버 예약 URL (있으면 store_name 검색 없이 바로 사용, 더 안정적).
        target_date: 예약하고 싶은 날짜, YYYY-MM-DD.
        time_min: 원하는 시간대의 시작, HH:MM.
        time_max: 원하는 시간대의 끝, HH:MM.
        party_size: 인원 수.
        mode: "AUTO"(조건 일치 시 자동 예약) | "ASK"(발견 시 승인 요청) | "NOTIFY"(알림만).
        time_priority: 우선순위 시간 목록, 예: ["19:00", "18:30", "19:30"]. 없으면 time_min~time_max 중 가장 빠른 시간을 사용.
        watch_start_at: 감시 시작 시각(ISO). 생략하면 지금부터.
        watch_end_at: 감시 종료 시각(ISO). 생략하면 target_date 다음날 00:00.
    """
    mode = mode.upper()
    if mode not in VALID_MODES:
        raise ValueError(f"mode는 {sorted(VALID_MODES)} 중 하나여야 합니다: {mode!r}")
    if not store_url and not store_name:
        raise ValueError("store_name 또는 store_url 중 하나는 필요합니다")

    _validate_date(target_date, "target_date")
    _validate_time(time_min, "time_min")
    _validate_time(time_max, "time_max")
    if time_priority:
        for t in time_priority:
            _validate_time(t, "time_priority")

    start_at = watch_start_at or datetime.now(UTC).isoformat()
    if watch_end_at:
        end_at = watch_end_at
    else:
        target_dt = datetime.strptime(target_date, "%Y-%m-%d")
        end_at = (target_dt + timedelta(days=1)).replace(tzinfo=UTC).isoformat()

    watch_id = db.create_watch(
        store_name=store_name or "(이름 미지정)",
        store_url=store_url,
        target_date=target_date,
        time_min=time_min,
        time_max=time_max,
        time_priority=time_priority,
        party_size=party_size,
        watch_start_at=start_at,
        watch_end_at=end_at,
        mode=mode,
        status="PENDING",
    )
    db.add_event(watch_id=watch_id, level="INFO", message="감시 등록됨")

    warning = None if _watcher_alive() else _watcher_not_running_message()
    return {"ok": True, "watch_id": watch_id, "watch": _format_watch(db.get_watch(watch_id)), "warning": warning}


@mcp.tool
def list_reservation_watches(include_inactive: bool = True) -> dict:
    """현재 등록된 예약 감시 목록을 반환합니다.

    Args:
        include_inactive: True면 RESERVED/EXPIRED/CANCELLED 상태도 포함.
    """
    watches = [_format_watch(w) for w in db.list_watches(include_inactive=include_inactive)]
    return {"ok": True, "count": len(watches), "watches": watches, "watcher_alive": _watcher_alive()}


@mcp.tool
def remove_reservation_watch(watch_id: int) -> dict:
    """등록된 예약 감시를 삭제합니다.

    Args:
        watch_id: 삭제할 감시의 id (add_reservation_watch 또는 list_reservation_watches로 확인).
    """
    watch = db.get_watch(watch_id)
    if not watch:
        return {"ok": False, "error": f"watch_id {watch_id} 를 찾을 수 없습니다"}
    db.delete_watch(watch_id)
    return {"ok": True, "message": f"{watch['store_name']} (id={watch_id}) 감시를 삭제했습니다"}


@mcp.tool
def check_availability(
    watch_id: int | None = None,
    store_name: str | None = None,
    store_url: str | None = None,
    target_date: str | None = None,
    party_size: int | None = None,
    timeout_sec: float = 45,
) -> dict:
    """지금 이 순간 예약 가능한 시간을 확인합니다 (watcher 서비스를 통해 실제 브라우저로 조회).

    watch_id를 주면 등록된 감시의 조건을 그대로 사용하고, 아니면 store_name/store_url +
    target_date + party_size 를 직접 지정해 즉석으로 조회할 수 있습니다.
    """
    if not _watcher_alive():
        return {"ok": False, "error": _watcher_not_running_message()}

    payload = {}
    if watch_id is None:
        if not (store_name or store_url) or not target_date or not party_size:
            raise ValueError("watch_id가 없으면 store_name/store_url, target_date, party_size가 모두 필요합니다")
        _validate_date(target_date, "target_date")
        payload = {
            "store_name": store_name, "store_url": store_url,
            "target_date": target_date, "party_size": party_size,
        }

    job_id = db.create_job(type_=JobType.CHECK_NOW, watch_id=watch_id, payload=payload)
    job = db.wait_for_job(job_id, timeout_sec=timeout_sec)
    if not job or job["status"] == JobStatus.PENDING or job["status"] == JobStatus.RUNNING:
        return {"ok": False, "error": "watcher 응답 시간이 초과되었습니다. 잠시 후 다시 시도해주세요."}
    if job["status"] == JobStatus.ERROR:
        return {"ok": False, "error": (job.get("result") or {}).get("error", "알 수 없는 오류")}
    return job["result"]


@mcp.tool
def reserve_now(
    time: str,
    watch_id: int | None = None,
    store_name: str | None = None,
    store_url: str | None = None,
    target_date: str | None = None,
    party_size: int | None = None,
    dry_run: bool = False,
    timeout_sec: float = 90,
) -> dict:
    """지정된 조건으로 지금 즉시 실제 예약을 시도합니다.

    결제/선결제/보증금/취소수수료/추가 동의가 필요하거나 캡차/추가 인증이 나타나면
    자동으로 진행하지 않고 AWAITING_APPROVAL/NEEDS_HUMAN 상태로 멈춥니다. 클릭 직전에
    화면의 날짜/시간/인원을 다시 확인해 요청과 다르면 진행하지 않습니다.

    watch_id를 주면 등록된 감시의 업체/날짜/인원을 사용하고, time만 지정하면 됩니다.

    dry_run=True 로 호출하면 실제 예약을 절대 완료하지 않습니다 - 업체 확인, 날짜/
    시간/인원 선택, 쿠폰 탐지(다운로드는 안 함), 결제/보증금/취소수수료/마케팅동의/
    본인인증 안전 게이트 확인, 클릭 직전 재검증까지 실제 사이트에서 그대로 수행하되
    최종 확인 버튼을 누르기 직전에 멈추고 무엇을 관찰했는지 보고합니다 (DB에 예약
    기록도 남기지 않고 "예약 완료" 알림도 보내지 않습니다). 사이트가 바뀐 뒤 실제
    예약 흐름이 여전히 안전하게 동작하는지 점검할 때 사용하세요.
    """
    if not _watcher_alive():
        return {"ok": False, "error": _watcher_not_running_message()}

    _validate_time(time, "time")
    payload: dict = {"time": time, "dry_run": dry_run}
    if watch_id is None:
        if not (store_name or store_url) or not target_date or not party_size:
            raise ValueError("watch_id가 없으면 store_name/store_url, target_date, party_size가 모두 필요합니다")
        _validate_date(target_date, "target_date")
        payload.update({
            "store_name": store_name, "store_url": store_url,
            "target_date": target_date, "party_size": party_size,
        })

    job_id = db.create_job(type_=JobType.RESERVE_NOW, watch_id=watch_id, payload=payload)
    job = db.wait_for_job(job_id, timeout_sec=timeout_sec)
    if not job or job["status"] in (JobStatus.PENDING, JobStatus.RUNNING):
        return {"ok": False, "error": "watcher 응답 시간이 초과되었습니다. reservation_status로 나중에 확인해주세요."}
    if job["status"] == JobStatus.ERROR:
        return {"ok": False, "error": (job.get("result") or {}).get("error", "알 수 없는 오류")}
    return job["result"]


@mcp.tool
def approve_reservation(watch_id: int, approve: bool = True) -> dict:
    """ASK 모드에서 발견된 예약 제안을 승인(예약 진행)하거나 거절합니다.

    Args:
        watch_id: 대상 감시 id (상태가 AWAITING_APPROVAL 이어야 함).
        approve: True면 예약 진행, False면 이번 제안을 무시하고 계속 감시.
    """
    if not _watcher_alive():
        return {"ok": False, "error": _watcher_not_running_message()}

    job_type = JobType.APPROVE if approve else JobType.DECLINE
    job_id = db.create_job(type_=job_type, watch_id=watch_id, payload=None)
    job = db.wait_for_job(job_id, timeout_sec=90)
    if not job or job["status"] in (JobStatus.PENDING, JobStatus.RUNNING):
        return {"ok": False, "error": "watcher 응답 시간이 초과되었습니다."}
    if job["status"] == JobStatus.ERROR:
        return {"ok": False, "error": (job.get("result") or {}).get("error", "알 수 없는 오류")}
    return job["result"]


@mcp.tool
def reservation_status(watch_id: int | None = None) -> dict:
    """예약 성공/실패/대기 상태를 확인합니다.

    Args:
        watch_id: 지정하면 해당 감시의 상세 상태 + 최근 이벤트 로그를 반환.
                  생략하면 전체 감시 목록 요약 + 성공한 예약 목록을 반환.
    """
    if watch_id is not None:
        watch = db.get_watch(watch_id)
        if not watch:
            return {"ok": False, "error": f"watch_id {watch_id} 를 찾을 수 없습니다"}
        events = db.list_events(watch_id=watch_id, limit=20)
        reservation = db.get_reservation(watch["reservation_id"]) if watch.get("reservation_id") else None
        return {
            "ok": True, "watch": _format_watch(watch), "reservation": reservation,
            "recent_events": events, "watcher_alive": _watcher_alive(),
        }

    watches = [_format_watch(w) for w in db.list_watches(include_inactive=True)]
    reservations = db.list_reservations()
    return {
        "ok": True, "watches": watches, "reservations": reservations,
        "watcher_alive": _watcher_alive(),
    }


def main() -> None:
    db.init_db()
    mcp.run()


if __name__ == "__main__":
    main()
