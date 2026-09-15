"""Regression tests for NaverBookingAdapter's real click-through flow
(date chip -> party -> time slot -> "다음" -> confirm/summary screen),
AUTO_ACCEPT_REQUIRED_TERMS (필수 동의 자동 체크), and the booking-widget's own
coupon selection UI.

These exercise the exact production code (_advance_to_confirm_screen,
_select_time_slot, _advance_past_next_button, _read_pending_summary,
_find_final_button_locator, _click_confirm_button, _auto_accept_required_terms,
_handle_widget_coupons) against a local static HTML fixture
(tests/fixtures/naver_widget_confirm.html) that reproduces the real Naver
Place booking widget's structure as captured live on 2026-09-15 against
실제 업체 A - including the specific bugs/gaps that run uncovered:

  1. Selecting a time slot alone does not advance the widget; a separate
     "다음" button must be clicked too, or the confirm screen is never
     reached at all.
  2. The confirm-screen summary shows time in 12-hour "오후 5:00" form, not
     "17:00" - the pre-click revalidation must accept both.
  3. The page has a decoy header button whose visible text ("예약하기") is
     identical to the *first* entry in CONFIRM_BUTTON_TEXTS and carries
     role="button" - a naive text-based scan can click it by mistake instead
     of the real submit button.
  4. The real submit button is disabled via aria-disabled="true" plus an
     "is_disabled" CSS class, not the plain HTML `disabled` attribute.
  5. The confirm screen's single "모두 동의합니다 *필수" checkbox blocks AUTO
     mode forever unless it is safely auto-checked - but only when it is
     unambiguously required and not marketing/알림성.
  6. The booking widget has its own "쿠폰 선택" coupon UI, separate from and
     more authoritative than the /home page's coupon list.

No real network access or login session is used - this is a pure local
fixture, so it is safe to actually click things in it.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import pytest_asyncio
from playwright.async_api import async_playwright

from nrw.browser.base_adapter import ResolvedStore
from nrw.browser.naver_adapter import NaverBookingAdapter
from nrw.models import GateBlocked, GateReason, HumanVerificationRequired, ReservationValidationError

pytestmark = pytest.mark.asyncio

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "naver_widget_confirm.html"
FIXTURE_URL = FIXTURE_PATH.resolve().as_uri()

TARGET_DATE = "2026-09-18"
TIME_ = "17:00"  # 픽스처는 이걸 "오후 5:00"으로 표시한다
PARTY = 2


@pytest_asyncio.fixture
async def page():
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        ctx = await browser.new_context()
        pg = await ctx.new_page()
        try:
            yield pg
        finally:
            await ctx.close()
            await browser.close()


def make_store(consent: str = "none", coupon: str = "none") -> ResolvedStore:
    url = f"{FIXTURE_URL}?consent={consent}&coupon={coupon}"
    return ResolvedStore(name="테스트업체", url=url)


async def test_advance_to_confirm_screen_reaches_real_summary_and_matches(page):
    """다음 버튼 클릭까지 실제로 수행하고, 12시간제(오후 5:00) 요약도 올바르게
    24시간제(17:00) 요청과 일치한다고 판단해야 한다."""
    adapter = NaverBookingAdapter()
    store = make_store()

    shown, applied_coupon, accepted_terms, widget_coupons = await adapter._advance_to_confirm_screen(
        page, store, TARGET_DATE, TIME_, PARTY, coupon_id=None, dry_run=False
    )

    assert applied_coupon is None
    assert accepted_terms == []
    assert widget_coupons == []
    assert shown is not None
    assert "오후 5:00" in shown
    assert "2명" in shown
    assert "9. 18." in shown


async def test_find_final_button_never_picks_a_decoy_button(page):
    """헤더의 동일 텍스트('예약하기') 내비게이션 버튼도, 같은 컴포넌트 계열을
    공유하는 '이전'(뒤로가기) 버튼도 아니라 실제 제출 버튼
    (data-click-code="submitbutton.submit", '예약 신청하기')을 찾아야 한다."""
    adapter = NaverBookingAdapter()
    store = make_store()

    await adapter._advance_to_confirm_screen(
        page, store, TARGET_DATE, TIME_, PARTY, coupon_id=None, dry_run=False
    )

    loc = await adapter._find_final_button_locator(page)
    assert loc is not None
    text = (await loc.inner_text()).strip()
    assert text == "예약 신청하기"
    assert text not in ("예약하기", "이전")

    button_text = await adapter._find_confirm_button_text(page)
    assert button_text == "예약 신청하기"


async def test_required_consent_is_auto_accepted_and_reaches_confirm_screen(page):
    """예약 완료에 필수인 것으로 명확히 표시된(*필수) 동의는 자동으로 체크하고
    끝까지 진행해야 한다 - 더 이상 GATE_BLOCKED 로 멈추지 않는다. 체크한 항목
    이름이 accepted_terms 로 보고돼야 한다(이벤트 로그용)."""
    adapter = NaverBookingAdapter()
    store = make_store(consent="required")

    shown, applied_coupon, accepted_terms, _ = await adapter._advance_to_confirm_screen(
        page, store, TARGET_DATE, TIME_, PARTY, coupon_id=None, dry_run=False
    )

    assert shown is not None
    assert len(accepted_terms) == 1
    assert "필수" in accepted_terms[0]
    assert "아래 내용에 모두 동의합니다" in accepted_terms[0]

    checkbox = page.locator("#consent-checkbox")
    assert await checkbox.is_checked() is True
    submit = page.locator("#submit-btn")
    assert (await submit.get_attribute("aria-disabled")) == "false"


async def test_marketing_consent_marked_required_stops_for_human(page):
    """마케팅/알림성 동의 항목이 (비정상적으로) *필수로 표시돼 있으면, 사전
    승인 범위를 벗어나므로 절대 자동 체크하지 않고 NEEDS_HUMAN 으로 멈춘다."""
    adapter = NaverBookingAdapter()
    store = make_store(consent="marketing")

    with pytest.raises(HumanVerificationRequired) as excinfo:
        await adapter._advance_to_confirm_screen(
            page, store, TARGET_DATE, TIME_, PARTY, coupon_id=None, dry_run=False
        )

    assert "마케팅" in str(excinfo.value) or "알림" in str(excinfo.value)
    # 실제로 체크하지 않았어야 한다
    assert await page.locator("#consent-checkbox").is_checked() is False


async def test_ambiguous_consent_stops_for_human_instead_of_guessing(page):
    """필수/선택 표시가 전혀 없는(새로운/다른 형태의) 동의 항목은 추측하지 않고
    NEEDS_HUMAN 으로 멈춘다."""
    adapter = NaverBookingAdapter()
    store = make_store(consent="ambiguous")

    with pytest.raises(HumanVerificationRequired):
        await adapter._advance_to_confirm_screen(
            page, store, TARGET_DATE, TIME_, PARTY, coupon_id=None, dry_run=False
        )

    assert await page.locator("#consent-checkbox").is_checked() is False


async def test_dry_run_also_auto_accepts_required_terms_but_reports_it(page):
    """dry-run 이어도 필수 동의 체크는 실제로 수행한다(최종 제출 전까지 지속적
    효과가 없어 안전) - 최종 확인 버튼 직전까지 정상 도달하는지 확인하는 게
    이 기능의 핵심 목적이기 때문이다."""
    adapter = NaverBookingAdapter()
    store = make_store(consent="required")

    shown, _, accepted_terms, _ = await adapter._advance_to_confirm_screen(
        page, store, TARGET_DATE, TIME_, PARTY, coupon_id=None, dry_run=True
    )

    assert shown is not None
    assert len(accepted_terms) == 1
    assert await page.locator("#consent-checkbox").is_checked() is True


async def test_widget_coupon_free_is_auto_selected_and_applied_for_real_reserve(page):
    """예약 화면 자체에 내장된 쿠폰 UI에 게이트 없는 무료 쿠폰이 있으면, 실제
    예약 경로(apply=True)에서는 자동으로 선택("적용")해야 한다."""
    adapter = NaverBookingAdapter()
    store = make_store(coupon="free")

    _, applied_coupon, _, widget_coupons = await adapter._advance_to_confirm_screen(
        page, store, TARGET_DATE, TIME_, PARTY, coupon_id=None, dry_run=False
    )

    assert len(widget_coupons) == 1
    assert widget_coupons[0].name == "테스트 무료 쿠폰"
    assert widget_coupons[0].requires_gate is False
    assert applied_coupon is not None
    assert applied_coupon.name == "테스트 무료 쿠폰"

    # 모달이 "적용"으로 닫혔어야 한다
    assert not await page.locator("#coupon-modal").is_visible()


async def test_widget_coupon_gated_is_never_auto_applied(page):
    """멤버십 가입이 필요한(게이트) 쿠폰은 실제 예약 경로에서도 절대 자동으로
    선택/적용하지 않는다."""
    adapter = NaverBookingAdapter()
    store = make_store(coupon="gated")

    _, applied_coupon, _, widget_coupons = await adapter._advance_to_confirm_screen(
        page, store, TARGET_DATE, TIME_, PARTY, coupon_id=None, dry_run=False
    )

    assert len(widget_coupons) == 1
    assert widget_coupons[0].requires_gate is True
    assert applied_coupon is None

    checkbox = page.locator("#coupon-item-checkbox")
    assert (await checkbox.get_attribute("aria-checked")) == "false"


async def test_dry_run_reports_widget_coupon_without_selecting_it(page):
    """dry-run 에서는 쿠폰 UI를 조회만 하고 절대 선택/적용하지 않는다 - 계정/진행
    중인 예약 폼에 실제 변화를 주는 행위이기 때문이다."""
    adapter = NaverBookingAdapter()
    store = make_store(coupon="free")

    _, applied_coupon, _, widget_coupons = await adapter._advance_to_confirm_screen(
        page, store, TARGET_DATE, TIME_, PARTY, coupon_id=None, dry_run=True
    )

    assert len(widget_coupons) == 1
    assert applied_coupon is None  # dry-run은 절대 선택하지 않는다

    checkbox = page.locator("#coupon-item-checkbox")
    assert (await checkbox.get_attribute("aria-checked")) == "false"


async def test_click_confirm_button_refuses_when_widget_reports_disabled(page):
    """제출 버튼이 aria-disabled="true" + is_disabled 클래스로 비활성 상태임을
    보고하면(HTML disabled 속성이 없어도) 클릭을 거부해야 한다."""
    adapter = NaverBookingAdapter()
    store = make_store(consent="required")

    # AUTO_ACCEPT_REQUIRED_TERMS 를 거치지 않고, 이 테스트는 위젯 자체의 비활성
    # 버튼 처리만 별도로 확인하기 위해 화면까지만 직접 진행시킨다.
    await page.goto(store.url, wait_until="domcontentloaded")
    await page.locator('button[class*="_timeChipButton_"]').first.click()
    await page.wait_for_selector('[class*="_timePeriodGroup_"]')
    await adapter._select_party_size(page, PARTY)
    await adapter._select_time_slot(page, TIME_)
    await adapter._advance_past_next_button(page)

    loc = await adapter._find_final_button_locator(page)
    assert await adapter._is_locator_disabled(loc) is True

    with pytest.raises(ReservationValidationError):
        await adapter._click_confirm_button(page)

    # 체크박스를 체크하면(사람이 직접 하는 동작을 흉내) 버튼이 다시 활성화된다 -
    # _is_locator_disabled 가 실시간 상태를 정확히 반영하는지 확인.
    await page.locator("#consent-checkbox").check()
    assert await adapter._is_locator_disabled(loc) is False


async def test_payment_gate_still_blocks_regardless_of_consent_policy(page):
    """결제/보증금/취소수수료 안전 게이트는 AUTO_ACCEPT_REQUIRED_TERMS 와 무관하게
    그대로 유지되어야 한다 - 이 정책은 동의 체크박스에만 적용된다."""
    adapter = NaverBookingAdapter()
    store = make_store(consent="none")

    await page.goto(store.url, wait_until="domcontentloaded")
    await page.locator('button[class*="_timeChipButton_"]').first.click()
    await page.wait_for_selector('[class*="_timePeriodGroup_"]')
    await adapter._select_party_size(page, PARTY)
    await adapter._select_time_slot(page, TIME_)
    await adapter._advance_past_next_button(page)

    # 결제 마커 텍스트를 화면에 주입해 결제 게이트를 흉내낸다.
    await page.evaluate(
        "document.querySelector('main').insertAdjacentHTML('beforeend', '<div>선결제 안내</div>')"
    )

    gate = await adapter._detect_gate(page)
    assert gate == GateReason.PAYMENT_REQUIRED
