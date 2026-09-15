"""End-to-end test driving the full mock reservation lifecycle:
오픈 전 -> 오픈 -> 마감 -> 취소자리 발생 -> 예약 성공,
through the exact same watcher core logic used against the real Naver adapter.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from playwright.async_api import async_playwright

from mock_site.server import STATE, run_server
from nrw.browser.mock_adapter import MockBookingAdapter
from nrw.config import Config, PollingConfig
from nrw.models import JobType
from nrw.notifier.base import Notifier, NotifyMessage
from nrw.watcher.core import process_one_watch
from nrw.watcher.jobs import handle_job

pytestmark = pytest.mark.asyncio

TARGET_DATE = "2026-10-03"
PORT = 8791


class RecordingNotifier(Notifier):
    def __init__(self) -> None:
        self.messages: list[NotifyMessage] = []

    def notify(self, message: NotifyMessage) -> None:
        self.messages.append(message)


@pytest.fixture
def mock_server():
    STATE.reset()
    srv = run_server(port=PORT)
    yield f"http://127.0.0.1:{PORT}/booking"
    srv.shutdown()
    srv.server_close()  # 소켓을 실제로 해제한다 - 안 하면 다음 테스트가 같은 포트에
    # bind() 할 때 이전 리스너와 공존하며 요청을 임의로 나눠 받는 경합이 생길 수 있다.
    STATE.reset()


@pytest_asyncio.fixture
async def context():
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        ctx = await browser.new_context()
        try:
            yield ctx
        finally:
            await ctx.close()
            await browser.close()


def make_cfg() -> Config:
    return Config(polling=PollingConfig(human_pause_sec=0.05, job_poll_interval_sec=0.1))


def make_watch(db, mock_server_url, *, mode: str, time_priority=None) -> int:
    now = datetime.now(UTC)
    return db.create_watch(
        store_name="테스트식당",
        store_url=mock_server_url,
        target_date=TARGET_DATE,
        time_min="18:00",
        time_max="20:00",
        time_priority=time_priority,
        party_size=2,
        watch_start_at=(now - timedelta(minutes=1)).isoformat(),
        watch_end_at=(now + timedelta(days=1)).isoformat(),
        mode=mode,
        status="PENDING",
    )


async def test_auto_mode_full_lifecycle_reserves_on_priority_match(isolated_db, mock_server, context):
    db = isolated_db
    adapter = MockBookingAdapter()
    notifier = RecordingNotifier()
    cfg = make_cfg()

    watch_id = make_watch(db, mock_server, mode="AUTO", time_priority=["19:00", "18:30"])

    # 1) 오픈 전
    watch = db.get_watch(watch_id)
    await process_one_watch(cfg, context, adapter, notifier, watch)
    assert db.get_watch(watch_id)["status"] == "WAITING_OPEN"
    assert db.list_reservations() == []

    # 2) 오픈 -> 19:00이 우선순위 1위이고 열려 있으므로 AUTO가 즉시 예약해야 함
    STATE.is_open = True
    watch = db.get_watch(watch_id)
    await process_one_watch(cfg, context, adapter, notifier, watch)

    final = db.get_watch(watch_id)
    assert final["status"] == "RESERVED"
    reservations = db.list_reservations()
    assert len(reservations) == 1
    assert reservations[0]["time"] == "19:00"
    assert reservations[0]["party_size"] == 2
    assert reservations[0]["naver_reservation_no"].startswith("MOCK-")
    assert STATE.slots["19:00"] is False  # 슬롯이 실제로 소비됨
    assert any("예약이 완료" in m.body for m in notifier.messages)

    # 예약 성공 후에는 due_watches 에서 제외되어 더 이상 조회되지 않아야 함
    due = db.due_watches(datetime.now(UTC).isoformat())
    assert watch_id not in {w["id"] for w in due}


async def test_sold_out_then_cancellation_reopens_slot_ask_mode(isolated_db, mock_server, context):
    db = isolated_db
    adapter = MockBookingAdapter()
    notifier = RecordingNotifier()
    cfg = make_cfg()

    watch_id = make_watch(db, mock_server, mode="ASK", time_priority=None)

    # 오픈 + 전부 마감
    STATE.is_open = True
    STATE.slots = {t: False for t in STATE.slots}
    watch = db.get_watch(watch_id)
    await process_one_watch(cfg, context, adapter, notifier, watch)
    assert db.get_watch(watch_id)["status"] == "WATCHING"
    assert db.list_reservations() == []

    # 취소자리 발생: 19:30 하나만 다시 열림
    STATE.slots["19:30"] = True
    watch = db.get_watch(watch_id)
    await process_one_watch(cfg, context, adapter, notifier, watch)

    pending = db.get_watch(watch_id)
    assert pending["status"] == "AWAITING_APPROVAL"
    assert pending["pending_candidate"]["time"] == "19:30"
    assert any("예약할까요" in m.body for m in notifier.messages)
    assert any(a.action_id == "approve" for m in notifier.messages for a in m.actions)

    # 이벤트 로그에 "새로 열린 시간 감지" 가 남아야 함
    events = db.list_events(watch_id=watch_id)
    assert any("새로 열린 시간 감지" in e["message"] for e in events)

    # 사용자가 승인 -> 실제 예약 완료
    job_id = db.create_job(type_=JobType.APPROVE, watch_id=watch_id, payload=None)
    job = db.claim_pending_jobs()[0]
    await handle_job(cfg, context, adapter, notifier, job)

    done_job = db.get_job(job_id)
    assert done_job["status"] == "DONE"
    assert done_job["result"]["ok"] is True

    final = db.get_watch(watch_id)
    assert final["status"] == "RESERVED"
    assert db.list_reservations()[0]["time"] == "19:30"


async def test_decline_does_not_immediately_re_ask_for_same_open_slot(isolated_db, mock_server, context):
    """회귀 테스트: 실사용 중 발견된 버그 - decline 처리 후 같은 슬롯이 계속 열려
    있으면, 바로 다음 확인 주기에서 다시 AWAITING_APPROVAL로 튕겨 돌아오면 안 된다."""
    db = isolated_db
    adapter = MockBookingAdapter()
    notifier = RecordingNotifier()
    cfg = make_cfg()

    watch_id = make_watch(db, mock_server, mode="ASK", time_priority=["19:00"])
    STATE.is_open = True

    watch = db.get_watch(watch_id)
    await process_one_watch(cfg, context, adapter, notifier, watch)
    assert db.get_watch(watch_id)["status"] == "AWAITING_APPROVAL"
    assert len(notifier.messages) == 1

    # 사용자가 "이번 자리 넘기기" 클릭 -> DECLINE job
    job_id = db.create_job(type_=JobType.DECLINE, watch_id=watch_id, payload=None)
    job = db.claim_pending_jobs()[0]
    await handle_job(cfg, context, adapter, notifier, job)
    assert db.get_job(job_id)["status"] == "DONE"
    declined = db.get_watch(watch_id)
    assert declined["status"] == "WATCHING"
    assert declined["pending_candidate"] is None
    assert declined["last_snapshot"]["declined_candidate"] == "19:00"

    # 같은 시간(19:00)이 여전히 열려 있는 상태로 다음 확인 주기가 돌아도
    # 다시 AWAITING_APPROVAL 로 되돌아가거나 재알림을 보내면 안 된다.
    watch = db.get_watch(watch_id)
    await process_one_watch(cfg, context, adapter, notifier, watch)
    still = db.get_watch(watch_id)
    assert still["status"] == "WATCHING"
    assert still["pending_candidate"] is None
    assert len(notifier.messages) == 1  # 재알림 없음

    # 마감 -> 재오픈(취소자리 발생)이면 거절 기록도 초기화되어 다시 승인 요청이 가능해야 함
    STATE.slots["19:00"] = False
    watch = db.get_watch(watch_id)
    await process_one_watch(cfg, context, adapter, notifier, watch)
    assert db.get_watch(watch_id)["last_snapshot"]["declined_candidate"] is None

    STATE.slots["19:00"] = True
    watch = db.get_watch(watch_id)
    await process_one_watch(cfg, context, adapter, notifier, watch)
    reopened = db.get_watch(watch_id)
    assert reopened["status"] == "AWAITING_APPROVAL"
    assert len(notifier.messages) == 2  # 재오픈으로 새로 알림


async def test_notify_mode_never_reserves_and_dedupes(isolated_db, mock_server, context):
    db = isolated_db
    adapter = MockBookingAdapter()
    notifier = RecordingNotifier()
    cfg = make_cfg()

    watch_id = make_watch(db, mock_server, mode="NOTIFY")
    STATE.is_open = True

    watch = db.get_watch(watch_id)
    await process_one_watch(cfg, context, adapter, notifier, watch)
    assert db.get_watch(watch_id)["status"] == "WATCHING"
    assert db.list_reservations() == []
    assert len(notifier.messages) == 1

    # 같은 후보로 다시 체크해도 중복 알림을 보내지 않아야 함
    watch = db.get_watch(watch_id)
    await process_one_watch(cfg, context, adapter, notifier, watch)
    assert len(notifier.messages) == 1

    # 마감됐다가 다시 열리면 (취소자리 재발생) 다시 알림이 가능해야 함
    STATE.slots = {t: False for t in STATE.slots}
    watch = db.get_watch(watch_id)
    await process_one_watch(cfg, context, adapter, notifier, watch)
    assert db.get_watch(watch_id)["last_snapshot"]["notified_candidate"] is None
    assert len(notifier.messages) == 1  # 마감 상태 자체는 알릴 후보가 없으므로 그대로

    STATE.slots["18:00"] = True
    watch = db.get_watch(watch_id)
    await process_one_watch(cfg, context, adapter, notifier, watch)
    assert len(notifier.messages) == 2  # 재오픈으로 다시 알림 발송됨


async def test_payment_gate_blocks_auto_reservation(isolated_db, mock_server, context):
    db = isolated_db
    adapter = MockBookingAdapter()
    notifier = RecordingNotifier()
    cfg = make_cfg()

    watch_id = make_watch(db, mock_server, mode="AUTO", time_priority=["19:00"])
    STATE.is_open = True
    STATE.require_payment = True

    watch = db.get_watch(watch_id)
    await process_one_watch(cfg, context, adapter, notifier, watch)

    final = db.get_watch(watch_id)
    assert final["status"] == "AWAITING_APPROVAL"
    assert db.list_reservations() == []  # 절대 결제를 자동으로 진행하지 않음
    assert any("결제" in m.body for m in notifier.messages)


async def test_captcha_gate_sets_needs_human(isolated_db, mock_server, context):
    db = isolated_db
    adapter = MockBookingAdapter()
    notifier = RecordingNotifier()
    cfg = make_cfg()

    watch_id = make_watch(db, mock_server, mode="AUTO", time_priority=["19:00"])
    STATE.is_open = True
    STATE.require_captcha = True

    watch = db.get_watch(watch_id)
    await process_one_watch(cfg, context, adapter, notifier, watch)

    final = db.get_watch(watch_id)
    assert final["status"] == "NEEDS_HUMAN"
    assert db.list_reservations() == []


async def test_needs_human_pauses_until_resumed_via_check_now(isolated_db, mock_server, context):
    db = isolated_db
    adapter = MockBookingAdapter()
    notifier = RecordingNotifier()
    cfg = make_cfg()

    watch_id = make_watch(db, mock_server, mode="AUTO", time_priority=["19:00"])
    STATE.is_open = True
    STATE.require_captcha = True

    watch = db.get_watch(watch_id)
    await process_one_watch(cfg, context, adapter, notifier, watch)
    assert db.get_watch(watch_id)["status"] == "NEEDS_HUMAN"

    # NEEDS_HUMAN 상태에서는 due_watches에 다시 나타나지 않아야 한다 (자동 재시도 없음)
    due = db.due_watches(datetime.now(UTC).isoformat())
    assert watch_id not in {w["id"] for w in due}

    # 사용자가 브라우저에서 직접 인증을 완료했다고 가정 (캡차 해제)
    STATE.require_captcha = False

    # 사용자가 Claude에게 "다시 확인해줘" -> check_availability(watch_id=...) 호출 시나리오
    job_id = db.create_job(type_=JobType.CHECK_NOW, watch_id=watch_id, payload={})
    job = db.claim_pending_jobs()[0]
    await handle_job(cfg, context, adapter, notifier, job)

    done_job = db.get_job(job_id)
    assert done_job["status"] == "DONE"
    assert done_job["result"]["ok"] is True

    resumed = db.get_watch(watch_id)
    assert resumed["status"] in ("WATCHING", "WAITING_OPEN")
    assert resumed["next_check_at"] is not None

    events = [e["message"] for e in db.list_events(watch_id=watch_id)]
    assert any("감시를 재개" in m for m in events)


async def test_approve_rechecks_slot_before_reserving(isolated_db, mock_server, context):
    db = isolated_db
    adapter = MockBookingAdapter()
    notifier = RecordingNotifier()
    cfg = make_cfg()

    watch_id = make_watch(db, mock_server, mode="ASK", time_priority=["19:00"])
    STATE.is_open = True

    watch = db.get_watch(watch_id)
    await process_one_watch(cfg, context, adapter, notifier, watch)
    pending = db.get_watch(watch_id)
    assert pending["status"] == "AWAITING_APPROVAL"
    assert pending["pending_candidate"]["time"] == "19:00"

    # 알림이 전송된 후, 사용자가 승인하기 전에 다른 누군가가 그 자리를 먼저 예약해버림
    STATE.slots["19:00"] = False

    job_id = db.create_job(type_=JobType.APPROVE, watch_id=watch_id, payload=None)
    job = db.claim_pending_jobs()[0]
    await handle_job(cfg, context, adapter, notifier, job)

    done_job = db.get_job(job_id)
    assert done_job["status"] == "DONE"
    assert done_job["result"]["ok"] is False
    assert done_job["result"]["status"] == "UNAVAILABLE"

    final = db.get_watch(watch_id)
    assert final["status"] == "WATCHING"
    assert final["pending_candidate"] is None
    assert db.list_reservations() == []  # 재조회에서 막혔으므로 실제 예약 시도(클릭)는 없어야 함
    assert any("이미 사라졌습니다" in m.body for m in notifier.messages)


async def test_pre_click_revalidation_blocks_mismatched_time(isolated_db, mock_server, context):
    """reserve_now 스타일로 실제 열려있지 않은/다른 시간에 예약을 강제로 시도하면
    클릭 직전 재검증에서 막혀야 한다."""
    from nrw.browser.base_adapter import ResolvedStore
    from nrw.models import ReservationValidationError, SlotUnavailableError

    adapter = MockBookingAdapter()
    STATE.is_open = True
    store = await adapter.resolve_store(context, "테스트식당", mock_server)

    with pytest.raises(SlotUnavailableError):
        await adapter.reserve(context, store, TARGET_DATE, "23:59", 2)
