"""Adapter that drives the local mock_site instead of real Naver Booking.
Implements the exact same ``BookingAdapter`` interface as ``naver_adapter``,
so ``watcher/core.py`` can be fully exercised in tests without touching the
real site.
"""
from __future__ import annotations

import asyncio
import re
from datetime import UTC, datetime

from playwright.async_api import BrowserContext

from nrw import coupon_utils
from nrw.browser.base_adapter import BookingAdapter, DryRunResult, ReservationResult, ResolvedStore
from nrw.models import (
    AvailabilitySnapshot,
    Coupon,
    CouponContext,
    CouponUnavailableError,
    GateBlocked,
    GateReason,
    HumanVerificationRequired,
    NotOpenYetError,
    ReservationValidationError,
    SlotUnavailableError,
    SlotState,
    TimeSlot,
)

_PAGE_CLOSE_TIMEOUT_SEC = 5.0


async def _safe_close(page) -> None:
    """page.close() 가 가끔 (특히 client-side location.reload() 직후) 응답 없이
    멈추는 것이 실측됐다 - 그 경우 그대로 두면 watcher 프로세스 전체가 무기한
    멈춘다. 그래서 항상 시간제한을 두고, 넘기면 포기한다 (컨텍스트 자체가 나중에
    닫히면서 결국 정리된다)."""
    try:
        await asyncio.wait_for(page.close(), timeout=_PAGE_CLOSE_TIMEOUT_SEC)
    except Exception:
        pass


CONFIRM_SUMMARY_RE = re.compile(r"날짜:\s*(\S+)\s*/\s*시간:\s*(\S+)\s*/\s*인원:\s*(\d+)명")
SUCCESS_SUMMARY_RE = re.compile(
    r"(\S+)\s*/\s*(\S+)\s+(\S+)\s*/\s*(\d+)명\s*/\s*예약번호:\s*(\S+)"
    r"(?:\s*/\s*적용쿠폰:\s*(.+?)\s*/\s*할인액:\s*(\d+)원)?"
)


def _to_coupon(d: dict) -> Coupon:
    return Coupon(
        coupon_id=d["id"],
        name=d["name"],
        discount_type=d.get("discount_type", "AMOUNT"),
        discount_value=d.get("discount_value", 0),
        min_amount=d.get("min_amount"),
        max_discount=d.get("max_discount"),
        held=bool(d.get("held")),
        downloadable_free=bool(d.get("downloadable_free", True)),
        requires_gate=bool(d.get("requires_gate")),
        gate_reason=d.get("gate_reason"),
        expires_at=d.get("expires_at"),
        raw_text=d.get("name"),
    )


class MockBookingAdapter(BookingAdapter):
    async def resolve_store(
        self, context: BrowserContext, store_name: str | None, store_url: str | None
    ) -> ResolvedStore:
        if not store_url:
            raise ValueError("mock adapter requires an explicit store_url (mock_site booking URL)")
        return ResolvedStore(name=store_name or "테스트식당", url=store_url)

    async def check_availability(
        self, context: BrowserContext, store: ResolvedStore, target_date: str, party_size: int
    ) -> AvailabilitySnapshot:
        page = await context.new_page()
        try:
            await page.goto(f"{store.url}?date={target_date}&party={party_size}")
            snapshot = await self._read_snapshot(page)
            return snapshot
        finally:
            await _safe_close(page)

    async def list_coupons(
        self, context: BrowserContext, store: ResolvedStore, target_date: str, party_size: int
    ) -> CouponContext:
        page = await context.new_page()
        try:
            await page.goto(f"{store.url}?date={target_date}&party={party_size}")
            raw = await self._fetch_coupons_raw(page)
            amount = await page.evaluate("() => window.__amount")
            return CouponContext(estimated_amount=amount, coupons=[_to_coupon(d) for d in raw])
        finally:
            await _safe_close(page)

    async def download_free_coupons(
        self, context: BrowserContext, store: ResolvedStore, coupons: list[Coupon]
    ) -> list[Coupon]:
        candidates = {
            c.coupon_id for c in coupons
            if not c.held and c.downloadable_free and not c.requires_gate
        }
        if not candidates:
            return coupons

        page = await context.new_page()
        try:
            await page.goto(store.url)
            for cid in candidates:
                btn = page.locator(f'.coupon-download-btn[data-coupon-id="{cid}"]')
                if await btn.count():
                    await btn.click()
                    await page.wait_for_load_state("domcontentloaded")
            raw = await self._fetch_coupons_raw(page)
            return [_to_coupon(d) for d in raw]
        finally:
            await _safe_close(page)

    async def _fetch_coupons_raw(self, page) -> list[dict]:
        # 드물게 "Execution context was destroyed" (직전 reload/navigation과 경합)가
        # 발생하는 것이 실측됨 - 한 번은 안전하게 재시도한다.
        for attempt in range(2):
            try:
                data = await page.evaluate("() => fetch('/admin/state').then(r => r.json())")
                return data.get("coupons", [])
            except Exception:
                if attempt == 1:
                    raise
                await page.wait_for_load_state("domcontentloaded")

    async def reserve(
        self,
        context: BrowserContext,
        store: ResolvedStore,
        target_date: str,
        time_: str,
        party_size: int,
        coupon_id: str | None = None,
    ) -> ReservationResult:
        page = await context.new_page()
        try:
            result = await self._reserve_inner(page, store, target_date, time_, party_size, coupon_id)
        except (GateBlocked, HumanVerificationRequired):
            # 결제/동의/인증 화면은 사용자가 직접 볼 수 있도록 페이지를 닫지 않고 남겨둔다.
            raise
        except Exception:
            await _safe_close(page)
            raise
        else:
            await _safe_close(page)
            return result

    async def _reserve_inner(
        self, page, store: ResolvedStore, target_date: str, time_: str, party_size: int,
        coupon_id: str | None,
    ) -> ReservationResult:
        await self._advance_to_confirm_screen(page, store, target_date, time_, party_size, coupon_id)

        await page.click("#confirm-btn")
        await page.wait_for_selector("#success-panel", timeout=5000)
        success_text = await page.locator("#success-summary").inner_text()
        m = SUCCESS_SUMMARY_RE.search(success_text)
        if not m:
            return ReservationResult(
                store_name=store.name, date=target_date, time=time_,
                party_size=party_size, naver_reservation_no=None,
                raw_confirmation=success_text,
            )
        return ReservationResult(
            store_name=m.group(1), date=m.group(2), time=m.group(3),
            party_size=int(m.group(4)), naver_reservation_no=m.group(5),
            raw_confirmation=success_text,
            coupon_name=m.group(6),
            coupon_discount=int(m.group(7)) if m.group(7) else None,
        )

    async def _advance_to_confirm_screen(
        self, page, store: ResolvedStore, target_date: str, time_: str, party_size: int,
        coupon_id: str | None,
    ) -> str:
        """네비게이션부터 최종 확인 버튼을 누르기 '직전'까지. ``reserve()`` 와
        ``dry_run_reserve()`` 가 이 메서드를 그대로 공유해서 dry-run이 실제
        예약 경로와 절대 어긋나지 않도록 보장한다."""
        await page.goto(f"{store.url}?date={target_date}&party={party_size}")

        before_open = await page.locator("#before-open-panel").count()
        if before_open:
            raise NotOpenYetError("아직 예약이 열리지 않았습니다")

        await page.select_option("#partySize", str(party_size))

        slot = page.locator(f'.slot[data-time="{time_}"]')
        if await slot.count() == 0:
            raise SlotUnavailableError(f"'{time_}' 시간 슬롯을 찾을 수 없습니다")
        if await slot.is_disabled():
            raise SlotUnavailableError(f"'{time_}' 시간은 이미 마감되었습니다")
        await slot.click()

        if coupon_id:
            await self._select_coupon_or_raise(page, coupon_id, party_size)

        await page.click("#reserve-btn")
        await page.wait_for_selector(
            "#payment-panel, #captcha-panel, #confirm-panel", timeout=5000
        )

        if await page.locator("#payment-panel").count():
            raise GateBlocked(GateReason.PAYMENT_REQUIRED, "선결제(예약금)가 필요합니다")
        if await page.locator("#captcha-panel").count():
            raise HumanVerificationRequired("보안 인증(캡차/추가 인증)이 필요합니다")

        summary_text = await page.locator("#confirm-summary").inner_text()
        match = CONFIRM_SUMMARY_RE.search(summary_text)
        if not match:
            raise ReservationValidationError(f"예약 확인 화면을 해석할 수 없습니다: {summary_text!r}")
        shown_date, shown_time, shown_party = match.group(1), match.group(2), int(match.group(3))
        if shown_date != target_date or shown_time != time_ or shown_party != party_size:
            raise ReservationValidationError(
                f"클릭 직전 재검증 실패: 요청({target_date} {time_} {party_size}명) "
                f"!= 화면({shown_date} {shown_time} {shown_party}명)"
            )
        if coupon_id and "쿠폰:" not in summary_text:
            raise ReservationValidationError(
                f"클릭 직전 재검증 실패: 쿠폰이 선택되었지만 확인 화면에 반영되지 않았습니다: {summary_text!r}"
            )
        return summary_text

    # -- dry run (최종 확인 버튼 직전까지만) ------------------------------------

    async def dry_run_reserve(
        self, context: BrowserContext, store: ResolvedStore, target_date: str, time_: str, party_size: int,
    ) -> DryRunResult:
        coupon_name = None
        coupon_applicable = False
        coupon_already_held = False
        coupon_note = None
        try:
            coupon_ctx = await self.list_coupons(context, store, target_date, party_size)
            pick = coupon_utils.pick_best(coupon_ctx.coupons, coupon_ctx.estimated_amount)
            if pick.coupon:
                coupon_name = pick.coupon.name
                coupon_already_held = pick.coupon.held
                coupon_applicable = True
                coupon_note = (
                    "이미 보유 중 - 적용 가능합니다" if pick.coupon.held else
                    "무료로 받을 수 있는 쿠폰입니다 - 실제 실행 시 자동으로 받아서 적용됩니다 "
                    "(이번 점검에서는 실제로 받지 않았습니다)"
                )
            elif coupon_ctx.coupons:
                coupon_note = "쿠폰이 있지만 모두 확인이 필요한(게이트) 쿠폰이라 자동 적용 대상이 아닙니다"
            else:
                coupon_note = "받을 수 있는 쿠폰이 없습니다"
        except Exception as e:
            coupon_note = f"쿠폰 조회 중 오류: {e}"

        page = await context.new_page()
        try:
            result = await self._dry_run_inner(page, store, target_date, time_, party_size)
        finally:
            await _safe_close(page)

        result.coupon_name = coupon_name
        result.coupon_applicable = coupon_applicable
        result.coupon_already_held = coupon_already_held
        result.coupon_note = coupon_note
        return result

    async def _dry_run_inner(
        self, page, store: ResolvedStore, target_date: str, time_: str, party_size: int,
    ) -> DryRunResult:
        base = dict(
            store_name=store.name, store_url=store.url, target_date=target_date,
            time=time_, party_size=party_size,
        )
        try:
            shown = await self._advance_to_confirm_screen(page, store, target_date, time_, party_size, None)
        except NotOpenYetError as e:
            return DryRunResult(ok=False, stage="NOT_OPEN", message=str(e), **base)
        except SlotUnavailableError as e:
            return DryRunResult(ok=False, stage="SLOT_UNAVAILABLE", message=str(e), **base)
        except HumanVerificationRequired as e:
            return DryRunResult(ok=False, stage="NEEDS_HUMAN", message=str(e), **base)
        except GateBlocked as e:
            return DryRunResult(ok=False, stage="GATE_BLOCKED", message=str(e), gate_reason=str(e.reason), **base)
        except ReservationValidationError as e:
            return DryRunResult(ok=False, stage="VALIDATION_FAILED", message=str(e), **base)
        except Exception as e:
            return DryRunResult(ok=False, stage="ERROR", message=f"예상치 못한 오류: {e}", **base)

        button_text = None
        confirm_btn = page.locator("#confirm-btn")
        if await confirm_btn.count():
            button_text = (await confirm_btn.inner_text()).strip()
        return DryRunResult(
            ok=True, stage="READY_TO_CONFIRM",
            message="실제 예약과 동일하게 여기까지 진행했고, 최종 확인 버튼을 누르기 직전에 멈췄습니다.",
            validation_summary=shown, final_button_text=button_text, **base,
        )

    async def _select_coupon_or_raise(self, page, coupon_id: str, party_size: int) -> None:
        """예약 클릭 직전 쿠폰을 다시 조회해 여전히 유효한지 확인하고, 유효하면
        '적용' 버튼을 클릭한다. 더 이상 유효하지 않으면 절대 클릭하지 않고
        CouponUnavailableError 를 발생시킨다 (쿠폰 없이 예약을 강행하지 않음)."""
        raw = await self._fetch_coupons_raw(page)
        coupon = next((c for c in raw if c["id"] == coupon_id), None)
        amount = await page.evaluate("() => window.__amount")

        if not coupon:
            raise CouponUnavailableError(coupon_id, "쿠폰을 더 이상 찾을 수 없습니다")
        if not coupon.get("held"):
            raise CouponUnavailableError(coupon["name"], "보유하지 않은 쿠폰입니다")
        if coupon.get("requires_gate"):
            raise CouponUnavailableError(coupon["name"], "추가 확인이 필요한 쿠폰으로 바뀌었습니다")
        expires_at = coupon.get("expires_at")
        if expires_at:
            expires = datetime.fromisoformat(expires_at)
            if expires.tzinfo is None:
                expires = expires.replace(tzinfo=UTC)
            if datetime.now(UTC) >= expires:
                raise CouponUnavailableError(coupon["name"], "쿠폰 기간이 만료되었습니다")
        min_amount = coupon.get("min_amount")
        if min_amount is not None and amount < min_amount:
            raise CouponUnavailableError(coupon["name"], f"최소 결제금액({min_amount:,}원) 조건을 만족하지 않습니다")

        apply_btn = page.locator(f'.coupon-apply-btn[data-coupon-id="{coupon_id}"]')
        if await apply_btn.count() == 0:
            raise CouponUnavailableError(coupon["name"], "적용 버튼을 화면에서 찾을 수 없습니다")
        await apply_btn.click()

    async def _read_snapshot(self, page) -> AvailabilitySnapshot:
        checked_at = datetime.now(UTC).isoformat()
        before_open = await page.locator("#before-open-panel").count()
        if before_open:
            banner = await page.locator("#before-open-panel .banner").inner_text()
            return AvailabilitySnapshot(
                page_state=SlotState.BEFORE_OPEN,
                slots=[],
                open_at_text=banner,
                checked_at=checked_at,
            )

        slot_locators = page.locator(".slot")
        count = await slot_locators.count()
        slots: list[TimeSlot] = []
        for i in range(count):
            el = slot_locators.nth(i)
            time_str = await el.get_attribute("data-time")
            disabled = await el.is_disabled()
            slots.append(TimeSlot(time=time_str, available=not disabled))

        page_state = SlotState.OPEN if any(s.available for s in slots) else SlotState.SOLD_OUT
        return AvailabilitySnapshot(page_state=page_state, slots=slots, checked_at=checked_at)
