"""End-to-end coupon scenarios against the mock site: 쿠폰 없음 / 무료 쿠폰 다운로드 /
여러 쿠폰 중 최적 선택 / 사용 불가(게이트) 쿠폰 / 만료 쿠폰 - plus the two safety
mechanisms (never auto-touch gated coupons, never force a reservation through when a
selected coupon vanishes right before the click).
"""
from __future__ import annotations

import json
import urllib.request
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from playwright.async_api import async_playwright

from mock_site.server import PRICE_PER_PERSON, STATE, run_server
from nrw.browser.mock_adapter import MockBookingAdapter
from nrw.config import Config, PollingConfig
from nrw.models import CouponUnavailableError
from nrw.notifier.base import Notifier, NotifyMessage
from nrw.watcher.core import attempt_reservation, process_one_watch

pytestmark = pytest.mark.asyncio

TARGET_DATE = "2026-10-03"
PORT = 8792  # test_e2e_mock_flow.py 와 겹치지 않게 별도 포트 사용
AMOUNT = 2 * PRICE_PER_PERSON  # make_watch 의 party_size=2 기준 예상 결제금액


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


def make_coupon(
    coupon_id, name, *, discount_type="AMOUNT", discount_value=3000, min_amount=None,
    max_discount=None, held=False, downloadable_free=True, requires_gate=False,
    gate_reason=None, expires_at=None,
) -> dict:
    return {
        "id": coupon_id, "name": name, "discount_type": discount_type,
        "discount_value": discount_value, "min_amount": min_amount, "max_discount": max_discount,
        "held": held, "downloadable_free": downloadable_free, "requires_gate": requires_gate,
        "gate_reason": gate_reason, "expires_at": expires_at,
    }


def _admin_post(mock_server_url: str, path: str, payload: dict) -> dict:
    base = mock_server_url.rsplit("/booking", 1)[0]
    req = urllib.request.Request(
        base + path, data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST",
    )
    with urllib.request.urlopen(req, timeout=5) as resp:
        return json.loads(resp.read())


def _admin_state(mock_server_url: str) -> dict:
    base = mock_server_url.rsplit("/booking", 1)[0]
    with urllib.request.urlopen(base + "/admin/state", timeout=5) as resp:
        return json.loads(resp.read())


def set_coupons(mock_server_url: str, coupons: list[dict]) -> None:
    _admin_post(mock_server_url, "/admin/coupons", {"coupons": coupons})


async def test_no_coupon_reserves_without_discount(isolated_db, mock_server, context):
    db = isolated_db
    adapter = MockBookingAdapter()
    notifier = RecordingNotifier()
    cfg = make_cfg()

    watch_id = make_watch(db, mock_server, mode="AUTO", time_priority=["19:00"])
    STATE.is_open = True
    # 쿠폰 없음: set_coupons 호출 없이 기본 빈 목록

    watch = db.get_watch(watch_id)
    await process_one_watch(cfg, context, adapter, notifier, watch)

    assert db.get_watch(watch_id)["status"] == "RESERVED"
    reservation = db.list_reservations()[0]
    assert reservation["coupon_name"] is None
    assert reservation["coupon_discount"] is None


async def test_free_coupon_auto_downloaded_and_applied(isolated_db, mock_server, context):
    db = isolated_db
    adapter = MockBookingAdapter()
    notifier = RecordingNotifier()
    cfg = make_cfg()

    set_coupons(mock_server, [
        make_coupon("c1", "5천원 할인쿠폰", discount_type="AMOUNT", discount_value=5000),
    ])
    watch_id = make_watch(db, mock_server, mode="AUTO", time_priority=["19:00"])
    STATE.is_open = True

    watch = db.get_watch(watch_id)
    await process_one_watch(cfg, context, adapter, notifier, watch)

    assert db.get_watch(watch_id)["status"] == "RESERVED"
    reservation = db.list_reservations()[0]
    assert reservation["coupon_name"] == "5천원 할인쿠폰"
    assert reservation["coupon_discount"] == 5000

    # 중복 다운로드 방지: 서버 쪽 상태도 held=True 로 정확히 한 번만 반영됐는지 확인
    state = _admin_state(mock_server)
    assert state["coupons"][0]["held"] is True


async def test_multiple_coupons_best_discount_chosen(isolated_db, mock_server, context):
    db = isolated_db
    adapter = MockBookingAdapter()
    notifier = RecordingNotifier()
    cfg = make_cfg()

    # AMOUNT: 3,000원 고정 / PERCENT: 20% (60,000원 * 20% = 12,000원, cap 10,000 -> 10,000원)
    set_coupons(mock_server, [
        make_coupon("small", "3천원쿠폰", discount_type="AMOUNT", discount_value=3000),
        make_coupon("big", "20%쿠폰", discount_type="PERCENT", discount_value=20, max_discount=10000),
    ])
    watch_id = make_watch(db, mock_server, mode="AUTO", time_priority=["19:00"])
    STATE.is_open = True

    watch = db.get_watch(watch_id)
    await process_one_watch(cfg, context, adapter, notifier, watch)

    reservation = db.list_reservations()[0]
    assert reservation["coupon_name"] == "20%쿠폰"
    assert reservation["coupon_discount"] == 10000  # AMOUNT(3000) 보다 실제 할인액이 커서 선택됨


async def test_gated_coupon_never_auto_applied_even_if_bigger(isolated_db, mock_server, context):
    db = isolated_db
    adapter = MockBookingAdapter()
    notifier = RecordingNotifier()
    cfg = make_cfg()

    set_coupons(mock_server, [
        make_coupon("free", "무료쿠폰", discount_type="AMOUNT", discount_value=2000),
        make_coupon(
            "membership", "멤버십전용쿠폰", discount_type="AMOUNT", discount_value=50000,
            requires_gate=True, gate_reason="MEMBERSHIP_REQUIRED",
        ),
    ])
    watch_id = make_watch(db, mock_server, mode="AUTO", time_priority=["19:00"])
    STATE.is_open = True

    watch = db.get_watch(watch_id)
    await process_one_watch(cfg, context, adapter, notifier, watch)

    reservation = db.list_reservations()[0]
    assert reservation["coupon_name"] == "무료쿠폰"  # 훨씬 큰 게이트 쿠폰은 절대 자동 선택 안 됨
    assert reservation["coupon_discount"] == 2000

    # 게이트 쿠폰은 다운로드(받기)조차 시도하지 않아 held 가 그대로 False 여야 한다
    state = _admin_state(mock_server)
    membership = next(c for c in state["coupons"] if c["id"] == "membership")
    assert membership["held"] is False

    events = [e["message"] for e in db.list_events(watch_id=watch_id)]
    assert any("확인 필요" in m and "멤버십전용쿠폰" in m for m in events)


async def test_expired_coupon_is_skipped(isolated_db, mock_server, context):
    db = isolated_db
    adapter = MockBookingAdapter()
    notifier = RecordingNotifier()
    cfg = make_cfg()

    past = (datetime.now(UTC) - timedelta(days=1)).isoformat()
    set_coupons(mock_server, [
        make_coupon("expired", "만료쿠폰", discount_type="AMOUNT", discount_value=8000, expires_at=past),
    ])
    watch_id = make_watch(db, mock_server, mode="AUTO", time_priority=["19:00"])
    STATE.is_open = True

    watch = db.get_watch(watch_id)
    await process_one_watch(cfg, context, adapter, notifier, watch)

    assert db.get_watch(watch_id)["status"] == "RESERVED"
    reservation = db.list_reservations()[0]
    assert reservation["coupon_name"] is None  # 만료 쿠폰은 선택되지 않고 할인 없이 예약

    # 만료된 쿠폰은 (설령 downloadable_free 여도) 다운로드도 거부되어야 한다
    state = _admin_state(mock_server)
    assert state["coupons"][0]["held"] is False


async def test_ask_mode_notification_includes_coupon_summary(isolated_db, mock_server, context):
    db = isolated_db
    adapter = MockBookingAdapter()
    notifier = RecordingNotifier()
    cfg = make_cfg()

    set_coupons(mock_server, [
        make_coupon("c1", "쿠폰1000", discount_type="AMOUNT", discount_value=1000),
    ])
    watch_id = make_watch(db, mock_server, mode="ASK", time_priority=["19:00"])
    STATE.is_open = True

    watch = db.get_watch(watch_id)
    await process_one_watch(cfg, context, adapter, notifier, watch)

    assert db.get_watch(watch_id)["status"] == "AWAITING_APPROVAL"
    assert len(notifier.messages) == 1
    body = notifier.messages[0].body
    assert "쿠폰1000" in body
    assert "1,000원" in body

    # ASK 모드에서도 무료 쿠폰은 미리 받아둔다
    state = _admin_state(mock_server)
    assert state["coupons"][0]["held"] is True


async def test_coupon_unavailable_at_final_check_pauses_instead_of_forcing(
    isolated_db, mock_server, context
):
    """예약 직전 재검증에서 쿠폰이 사라지면(레이스), 할인 없이 강행하지 않고
    AWAITING_APPROVAL 로 멈추고 사용자에게 알려야 한다."""
    db = isolated_db
    notifier = RecordingNotifier()
    cfg = make_cfg()

    class CouponLosingAdapter(MockBookingAdapter):
        async def reserve(self, *args, **kwargs):
            raise CouponUnavailableError("사라진쿠폰", "테스트: 예약 직전 재검증 실패로 가정")

    from nrw.browser.base_adapter import ResolvedStore

    outcome = await attempt_reservation(
        cfg, context, CouponLosingAdapter(), notifier,
        store=ResolvedStore(name="테스트식당", url=mock_server),
        target_date=TARGET_DATE, time_="19:00", party_size=2, watch_id=None,
    )

    assert outcome.status == "AWAITING_APPROVAL"
    assert db.list_reservations() == []  # 할인 없이 강행하지 않음
    assert any("쿠폰" in m.body for m in notifier.messages)


async def test_adapter_rejects_stale_coupon_id_before_clicking(isolated_db, mock_server, context):
    """어댑터 자체가 (attempt_reservation 을 거치지 않고 직접 호출해도) 더 이상
    유효하지 않은 coupon_id 로는 절대 클릭하지 않고 예외를 던지는지 확인."""
    from nrw.browser.base_adapter import ResolvedStore

    adapter = MockBookingAdapter()
    STATE.is_open = True
    store = ResolvedStore(name="테스트식당", url=mock_server)

    with pytest.raises(CouponUnavailableError):
        await adapter.reserve(context, store, TARGET_DATE, "19:00", 2, coupon_id="never-downloaded")
