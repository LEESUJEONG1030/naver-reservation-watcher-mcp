"""dry_run_reserve(): exercises the exact same preparation code path as a real
reserve() (shared via _advance_to_confirm_screen), but must never click the
final confirm button. Verifies it stops at the right stage for each scenario
and reports coupon/gate/validation info correctly.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from playwright.async_api import async_playwright

from mock_site.server import STATE, run_server
from nrw.browser.base_adapter import ResolvedStore
from nrw.browser.mock_adapter import MockBookingAdapter

pytestmark = pytest.mark.asyncio

TARGET_DATE = "2026-10-03"
PORT = 8796


@pytest.fixture
def mock_server():
    STATE.reset()
    srv = run_server(port=PORT)
    yield f"http://127.0.0.1:{PORT}/booking"
    srv.shutdown()
    srv.server_close()
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


def make_coupon(coupon_id, name, **kwargs) -> dict:
    defaults = dict(
        id=coupon_id, name=name, discount_type="AMOUNT", discount_value=3000,
        min_amount=None, max_discount=None, held=False, downloadable_free=True,
        requires_gate=False, gate_reason=None, expires_at=None,
    )
    defaults.update(kwargs)
    return defaults


async def test_dry_run_reaches_confirm_screen_without_clicking(mock_server, context):
    STATE.is_open = True
    adapter = MockBookingAdapter()
    store = ResolvedStore(name="테스트식당", url=mock_server)

    result = await adapter.dry_run_reserve(context, store, TARGET_DATE, "19:00", 2)

    assert result.ok is True
    assert result.stage == "READY_TO_CONFIRM"
    assert result.final_button_text  # 실제로는 여기서 누르게 될 버튼 텍스트가 채워짐
    assert "19:00" in result.validation_summary
    assert "2명" in result.validation_summary

    # 절대 클릭하지 않았으므로 예약이 실제로 생기지 않아야 한다
    state = STATE
    assert state.slots["19:00"] is True  # 슬롯이 그대로 살아있음(소비 안 됨)
    assert state.last_reservation is None


async def test_dry_run_reports_free_coupon_without_downloading_it(mock_server, context):
    STATE.is_open = True
    STATE.coupons = [make_coupon("c1", "무료쿠폰", discount_value=5000)]
    adapter = MockBookingAdapter()
    store = ResolvedStore(name="테스트식당", url=mock_server)

    result = await adapter.dry_run_reserve(context, store, TARGET_DATE, "19:00", 2)

    assert result.ok is True
    assert result.coupon_name == "무료쿠폰"
    assert result.coupon_applicable is True
    assert result.coupon_already_held is False
    assert "받지 않았습니다" in result.coupon_note

    # 점검일 뿐이므로 실제로 쿠폰을 받지 않아야 한다 (계정 상태를 바꾸지 않음)
    assert STATE.coupons[0]["held"] is False


async def test_dry_run_stops_at_payment_gate(mock_server, context):
    STATE.is_open = True
    STATE.require_payment = True
    adapter = MockBookingAdapter()
    store = ResolvedStore(name="테스트식당", url=mock_server)

    result = await adapter.dry_run_reserve(context, store, TARGET_DATE, "19:00", 2)

    assert result.ok is False
    assert result.stage == "GATE_BLOCKED"
    assert "PAYMENT_REQUIRED" in result.gate_reason
    assert result.final_button_text is None


async def test_dry_run_stops_at_captcha_needs_human(mock_server, context):
    STATE.is_open = True
    STATE.require_captcha = True
    adapter = MockBookingAdapter()
    store = ResolvedStore(name="테스트식당", url=mock_server)

    result = await adapter.dry_run_reserve(context, store, TARGET_DATE, "19:00", 2)

    assert result.ok is False
    assert result.stage == "NEEDS_HUMAN"


async def test_dry_run_reports_slot_unavailable(mock_server, context):
    STATE.is_open = True
    STATE.slots["19:00"] = False  # 마감
    adapter = MockBookingAdapter()
    store = ResolvedStore(name="테스트식당", url=mock_server)

    result = await adapter.dry_run_reserve(context, store, TARGET_DATE, "19:00", 2)

    assert result.ok is False
    assert result.stage == "SLOT_UNAVAILABLE"


async def test_dry_run_reports_not_open_yet(mock_server, context):
    STATE.is_open = False
    adapter = MockBookingAdapter()
    store = ResolvedStore(name="테스트식당", url=mock_server)

    result = await adapter.dry_run_reserve(context, store, TARGET_DATE, "19:00", 2)

    assert result.ok is False
    assert result.stage == "NOT_OPEN"


async def test_dry_run_never_creates_a_reservation_across_all_scenarios(mock_server, context):
    """방어적 회귀 테스트: 어떤 dry-run 경로를 타든 실제 예약은 절대 생기지 않는다."""
    STATE.is_open = True
    STATE.coupons = [make_coupon("c1", "쿠폰", discount_value=1000)]
    adapter = MockBookingAdapter()
    store = ResolvedStore(name="테스트식당", url=mock_server)

    await adapter.dry_run_reserve(context, store, TARGET_DATE, "19:00", 2)
    assert STATE.last_reservation is None
