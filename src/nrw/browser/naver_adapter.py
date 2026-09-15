"""Real Naver Booking adapter.

Primary path targets the "장소예약" (Naver Place booking) widget embedded at
``pcmap.place.naver.com/{category}/{id}/booking`` - reached from a
map.naver.com place page or a naver.me short link. This is what most
restaurant/cafe businesses on Naver actually use. Selectors for this widget
were captured from a real business page (see naver_selectors.py header) and
live in one place there for easy maintenance.

If a business's page doesn't match this widget's date-chip markers at all
(e.g. an older/custom booking.naver.com widget), we fall back to the
generic text/marker-based reading in ``_read_snapshot_generic``. Either way,
safety-gate detection (payment/consent/captcha/reauth) is marker-text based
and applies regardless of which widget shape matched.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import re
from datetime import UTC, datetime
from urllib.parse import quote, urlsplit, urlunsplit

from playwright.async_api import BrowserContext, Page, TimeoutError as PwTimeoutError

from nrw import coupon_utils
from nrw.browser import naver_selectors as sel
from nrw.browser.base_adapter import BookingAdapter, DryRunResult, ReservationResult, ResolvedStore
from nrw.models import (
    AvailabilitySnapshot,
    Coupon,
    CouponContext,
    CouponGateReason,
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

log = logging.getLogger("nrw.browser.naver_adapter")

_PAGE_CLOSE_TIMEOUT_SEC = 5.0


async def _safe_close(page: Page) -> None:
    """page.close() 가 가끔 (특히 클라이언트 측 navigation 직후) 응답 없이 멈추는
    것이 실측됐다 - 그대로 두면 watcher 프로세스 전체가 무기한 멈춘다. 그래서
    항상 시간제한을 두고, 넘기면 포기한다 (컨텍스트가 나중에 닫히며 정리된다)."""
    try:
        await asyncio.wait_for(page.close(), timeout=_PAGE_CLOSE_TIMEOUT_SEC)
    except Exception:
        pass


def _place_home_url(booking_url: str) -> str:
    """쿠폰은 /booking 이 아니라 /home 서브패스에 노출되는 것을 실제로 확인했다
    (2026-09-14, 실제 업체 B). /booking 으로 끝나면 /home 으로 바꿔주고, 그 형태가
    아니면 원래 URL을 그대로 쓴다 (best-effort - 못 찾으면 쿠폰 0개로 처리됨)."""
    parts = urlsplit(booking_url)
    path = parts.path
    if path.endswith("/booking"):
        path = path[: -len("/booking")] + "/home"
    return urlunsplit((parts.scheme, parts.netloc, path, "", ""))


class NaverBookingAdapter(BookingAdapter):
    async def resolve_store(
        self, context: BrowserContext, store_name: str | None, store_url: str | None
    ) -> ResolvedStore:
        if not store_url:
            return await self._resolve_by_search(context, store_name)

        page = await context.new_page()
        try:
            await page.goto(store_url, wait_until="domcontentloaded")
            await page.wait_for_timeout(1500)
            await self._raise_if_needs_human(page)

            current_url = page.url
            if "/booking" not in current_url and any(
                h in current_url for h in sel.BOOKING_HOST_MARKERS
            ):
                # frame 내부 콘텐츠 하이드레이션이 고정 대기(1500ms)보다 늦게 끝나는
                # 경우가 실측됨(같은 링크인데 가끔 booking 링크를 못 찾음) - 짧게
                # 몇 번 재시도한다.
                booking_href = None
                for attempt in range(4):
                    booking_href = await self._find_booking_link(page)
                    if booking_href:
                        break
                    await page.wait_for_timeout(750)
                if booking_href:
                    await page.goto(booking_href, wait_until="domcontentloaded")
                    await page.wait_for_timeout(1000)
                    current_url = page.url
                else:
                    log.warning(
                        "booking 링크를 찾지 못해 현재 URL을 그대로 사용합니다: %s", current_url
                    )

            name = store_name or await self._guess_store_name(page)
            return ResolvedStore(name=name, url=current_url)
        finally:
            await _safe_close(page)

    async def _resolve_by_search(self, context: BrowserContext, store_name: str | None) -> ResolvedStore:
        """업체명만으로 검색할 때는 일반 웹검색(search.naver.com)이 아니라 네이버
        지도 검색(map.naver.com/p/search)을 써야 한다 - 실제로 확인해보니 일반
        웹검색 결과에서는 실제 예약 위젯이 있는 place 페이지의 booking 링크를
        안정적으로 못 찾았다 (엉뚱한/불완전한 URL로 귀결됨). 지도 검색 결과 페이지는
        실제 place 프레임을 그대로 포함하므로 ``_find_booking_link`` 로 프레임까지
        뒤져서 찾는다 (resolve_store 가 직접 URL을 받았을 때와 동일한 방식)."""
        if not store_name:
            raise ValueError("store_name 또는 store_url 중 하나는 반드시 필요합니다")
        page = await context.new_page()
        try:
            await page.goto(
                f"https://map.naver.com/p/search/{quote(store_name)}", wait_until="domcontentloaded"
            )
            await page.wait_for_timeout(2000)
            await self._raise_if_needs_human(page)
            booking_href = await self._find_booking_link(page)
            if not booking_href:
                raise RuntimeError(
                    f"'{store_name}' 의 네이버 예약 링크를 지도 검색에서 찾지 못했습니다. "
                    "store_url을 직접 지정해주세요."
                )
            return await self.resolve_store(context, store_name, booking_href)
        finally:
            await _safe_close(page)

    async def _find_booking_link(self, page: Page) -> str | None:
        from urllib.parse import urljoin

        for frame in page.frames:
            try:
                link = frame.locator('a[href*="/booking"]').first
                if await link.count():
                    href = await link.get_attribute("href")
                    if href:
                        # href may be host-relative to the frame's own origin
                        # (e.g. pcmap.place.naver.com), not the top-level page's.
                        return urljoin(frame.url, href)
            except Exception:
                continue
        return None

    async def check_availability(
        self, context: BrowserContext, store: ResolvedStore, target_date: str, party_size: int
    ) -> AvailabilitySnapshot:
        page = await context.new_page()
        try:
            await page.goto(store.url, wait_until="domcontentloaded")
            await self._raise_if_needs_human(page)

            has_date_chips = await self._wait_for_date_chips(page)

            if has_date_chips:
                return await self._check_via_date_chips(page, target_date, party_size)

            # networkidle이 절대 조건이 아니라 best-effort 대기로 바뀜: 네이버 페이지는
            # 분석/추적 요청이 끊임없이 나가 networkidle이 영영 안 걸릴 수 있다 - 실제로
            # 운영 중 이 대기에서 타임아웃으로 체크 자체가 실패하는 것을 확인했다. 이제는
            # 타임아웃이 나도 지금까지 로드된 내용으로 계속 진행한다.
            try:
                await page.wait_for_load_state("networkidle", timeout=8000)
            except PwTimeoutError:
                pass
            await self._raise_if_needs_human(page)
            return await self._read_snapshot_generic(page, target_date)
        finally:
            await _safe_close(page)

    async def _wait_for_date_chips(self, page: Page, timeout_ms: int = 6000) -> bool:
        """날짜 칩 위젯이 하이드레이션될 때까지 짧게 폴링한다 (고정 sleep 1회보다 안정적)."""
        try:
            await page.wait_for_selector(sel.DATE_CHIP_LABEL, timeout=timeout_ms)
            return True
        except PwTimeoutError:
            return await page.locator(sel.DATE_CHIP_LABEL).count() > 0

    # -- 쿠폰 -----------------------------------------------------------------

    async def list_coupons(
        self, context: BrowserContext, store: ResolvedStore, target_date: str, party_size: int
    ) -> CouponContext:
        """쿠폰은 /booking 이 아니라 업체 홈(/home) 페이지에 노출되는 것을 실제
        확인했다 (실제 업체 B). 실제 결제금액은 이 페이지만으로는 알 수 없는 경우가
        많아 estimated_amount 는 best-effort 로 None 일 수 있다 - 그 경우
        min_amount 조건이 있는 쿠폰은 안전하게 "적용 불가"로 취급된다."""
        page = await context.new_page()
        try:
            await page.goto(_place_home_url(store.url), wait_until="domcontentloaded")
            await page.wait_for_timeout(1500)
            await self._raise_if_needs_human(page)
            coupons = await self._scan_coupons(page)
            return CouponContext(estimated_amount=None, coupons=coupons)
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
            await page.goto(_place_home_url(store.url), wait_until="domcontentloaded")
            await page.wait_for_timeout(1500)
            await self._raise_if_needs_human(page)

            links = page.locator(sel.COUPON_DOWNLOAD_LINK_SELECTOR)
            count = await links.count()
            for i in range(count):
                link = links.nth(i)
                coupon_id = await self._coupon_id_for(link)
                if coupon_id not in candidates:
                    continue
                aria_disabled = await link.get_attribute("aria-disabled")
                if aria_disabled == "true":
                    continue  # 이미 받음 - 중복 다운로드 방지
                try:
                    await link.click(timeout=3000)
                    await page.wait_for_timeout(800)
                except Exception:
                    log.warning("쿠폰(%s) 다운로드 클릭 실패 - 건너뜀", coupon_id)

            return await self._scan_coupons(page)
        finally:
            await _safe_close(page)

    async def _coupon_id_for(self, link) -> str:
        try:
            li = link.locator(sel.COUPON_LIST_ITEM_XPATH)
            text = (await li.inner_text()) if await li.count() else (await link.inner_text())
        except Exception:
            text = ""
        digest = hashlib.sha1(text.strip().encode("utf-8")).hexdigest()[:10]
        return f"naver-coupon-{digest}"

    async def _scan_coupons(self, page: Page) -> list[Coupon]:
        """네이버 플레이스 쿠폰 목록을 읽는다 (다운로드는 하지 않음).
        구조는 naver_selectors.py 상단 주석에 실제 확인한 내용을 기록해뒀다."""
        coupons: list[Coupon] = []
        links = page.locator(sel.COUPON_DOWNLOAD_LINK_SELECTOR)
        try:
            count = await links.count()
        except Exception:
            return coupons

        for i in range(count):
            link = links.nth(i)
            try:
                li = link.locator(sel.COUPON_LIST_ITEM_XPATH)
                raw_text = (await li.inner_text()) if await li.count() else (await link.inner_text())
            except Exception:
                continue
            raw_text = raw_text.strip()
            if not raw_text:
                continue

            aria_disabled = await link.get_attribute("aria-disabled")
            held = aria_disabled == "true"
            gated = any(m in raw_text for m in sel.COUPON_GATE_MARKERS)

            discount_type, discount_value = "AMOUNT", 0.0
            percent_m = sel.COUPON_PERCENT_RE.search(raw_text)
            amount_m = sel.COUPON_AMOUNT_RE.search(raw_text)
            if percent_m:
                discount_type, discount_value = "PERCENT", float(percent_m.group(1))
            elif amount_m:
                discount_type, discount_value = "AMOUNT", float(amount_m.group(1).replace(",", ""))
            # 금액을 파싱하지 못하면(예: 이 매장의 "음료수 증정"처럼 비금전적 혜택)
            # discount_value=0 으로 남겨두되 raw_text 로 사람이 직접 확인할 수 있게 한다.

            lines = [ln.strip() for ln in raw_text.splitlines() if ln.strip()]
            name = lines[0] if lines else f"쿠폰{i + 1}"

            coupons.append(Coupon(
                coupon_id=await self._coupon_id_for(link),
                name=name[:60],
                discount_type=discount_type,
                discount_value=discount_value,
                held=held,
                downloadable_free=not held,
                requires_gate=gated,
                gate_reason=CouponGateReason.MARKETING_CONSENT_REQUIRED if gated else None,
                raw_text=raw_text,
            ))
        return coupons

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
        self, page: Page, store: ResolvedStore, target_date: str, time_: str, party_size: int,
        coupon_id: str | None = None,
    ) -> ReservationResult:
        shown, applied_coupon, accepted_terms, _widget_coupons = await self._advance_to_confirm_screen(
            page, store, target_date, time_, party_size, coupon_id, dry_run=False
        )

        await self._click_confirm_button(page)
        await self._raise_if_needs_human(page)

        gate = await self._detect_gate(page)
        if gate:
            raise GateBlocked(gate, "확인 클릭 이후 결제/동의 화면으로 전환되었습니다")

        confirmation_text = await self._wait_for_success(page)
        m = sel.RESERVATION_NO_RE.search(confirmation_text)
        return ReservationResult(
            store_name=store.name,
            date=target_date,
            time=time_,
            party_size=party_size,
            naver_reservation_no=m.group(1) if m else None,
            raw_confirmation=confirmation_text,
            coupon_name=applied_coupon.name if applied_coupon else None,
            coupon_discount=None,  # 실제 결제금액 기준 정확한 할인액은 이 어댑터에서 산출하지 않음
            accepted_terms=accepted_terms,
        )

    async def _advance_to_confirm_screen(
        self, page: Page, store: ResolvedStore, target_date: str, time_: str, party_size: int,
        coupon_id: str | None, dry_run: bool,
    ) -> tuple[str | None, Coupon | None, list[str], list[Coupon]]:
        """네비게이션부터 최종 확인 버튼을 누르기 '직전'까지 - 날짜/시간 확인,
        쿠폰 확인/적용, 필수 동의 자동 체크(AUTO_ACCEPT_REQUIRED_TERMS), 안전
        게이트 확인, 클릭 직전 재검증 - 모든 단계를 수행한다. ``reserve()`` 와
        ``dry_run_reserve()`` 가 이 메서드를 그대로 공유해서, dry-run이 실제
        예약 경로와 절대 어긋나지 않도록 보장한다. ``dry_run=True`` 면 예약
        화면에 내장된 쿠폰 선택 UI를 조회만 하고 실제로 선택/적용하지 않는다
        (필수 동의 체크는 최종 제출 전까지 지속적 효과가 없으므로 dry-run에서도
        실제로 수행한다). 실패하면(마감/게이트/인증필요/재검증실패) 지금까지와
        동일한 예외를 던진다.

        반환값: (클릭 직전 화면 요약, 실제 적용한 쿠폰, 자동 체크한 필수 동의
        항목 이름 목록, 예약 화면에 내장된 쿠폰 UI에서 읽은 쿠폰 목록)."""
        await page.goto(store.url, wait_until="domcontentloaded")
        await self._raise_if_needs_human(page)

        has_date_chips = await self._wait_for_date_chips(page)

        if has_date_chips:
            snapshot = await self._check_via_date_chips(page, target_date, party_size, leave_modal_open=True)
        else:
            try:
                await page.wait_for_load_state("networkidle", timeout=8000)
            except PwTimeoutError:
                pass
            snapshot = await self._read_snapshot_generic(page, target_date)

        if snapshot.page_state == SlotState.BEFORE_OPEN:
            raise NotOpenYetError("아직 예약이 열리지 않았습니다")

        matching = [s for s in snapshot.slots if s.time == time_]
        if not matching or not matching[0].available:
            raise SlotUnavailableError(f"'{time_}' 시간은 예약할 수 없는 상태입니다")

        if has_date_chips:
            # 실제로 해당 시간 버튼을 클릭해 위젯을 다음 단계(요약/제출 화면)로
            # 넘어가게 한다. 이전에는 가용성만 읽고 아무 것도 클릭하지 않아 위젯이
            # 계속 시간 선택 모달에 머물렀고, 그 상태에서 클릭 직전 재검증이 화면의
            # 무관한 텍스트를 잘못 읽어들이는 원인이 됐다 (2026-09-15 실측, 실제 업체 A).
            await self._select_time_slot(page, time_)
            await self._advance_past_next_button(page)

        applied_coupon: Coupon | None = None
        widget_coupons: list[Coupon] = []
        if coupon_id:
            # 클릭 직전 쿠폰 재검증. 주의: 쿠폰은 /home 에 노출되고 예약 화면(/booking)
            # 자체에는 쿠폰 적용 UI가 없는 업체가 많다는 것을 실제로 확인했다 - 이
            # 경우 예약 화면에서 쿠폰을 재확인할 수 없으므로, 확신 없이 "할인 적용됨"을
            # 주장하지 않고 안전하게 CouponUnavailableError 로 멈춘다 (강행하지 않음).
            applied_coupon = await self._verify_coupon_still_valid(page, coupon_id)
        else:
            # 예약 화면 자체에 내장된 쿠폰 선택 UI도 확인한다 - /home 페이지 스캔이
            # 놓치거나 게이트 여부를 다르게(불일치하게) 보고한 적이 있었다
            # (2026-09-15 실측, 실제 업체 A). dry-run이면 조회만 하고 선택/적용하지
            # 않는다.
            widget_coupons, applied_coupon = await self._handle_widget_coupons(page, apply=not dry_run)

        # AUTO_ACCEPT_REQUIRED_TERMS: 예약 완료에 필수인 동의만 자동 체크한다.
        # 체크박스 상태는 최종 제출 전까지 아무 지속적 효과가 없으므로 dry-run
        # 에서도 실제로 수행해, dry-run이 실제 경로와 어긋나지 않게 한다.
        accepted_terms = await self._auto_accept_required_terms(page)
        for name in accepted_terms:
            log.info("AUTO_ACCEPT_REQUIRED_TERMS: 필수 동의 자동 체크 - '%s'", name)

        await self._raise_if_needs_human(page)
        gate = await self._detect_gate(page)
        if gate:
            raise self._with_partial_progress(
                GateBlocked(gate, "예약 진행 화면에서 결제/동의 관련 항목이 감지되었습니다"),
                accepted_terms, applied_coupon,
            )

        # 클릭 직전 재검증: 화면에 표시된 날짜/시간/인원을 다시 읽어 확인
        shown = await self._read_pending_summary(page)
        if shown and not self._summary_matches(shown, target_date, time_, party_size):
            raise self._with_partial_progress(
                ReservationValidationError(
                    f"클릭 직전 재검증 실패: 요청({target_date} {time_} {party_size}명) != 화면({shown!r})"
                ),
                accepted_terms, applied_coupon,
            )

        await self._raise_if_needs_human(page)
        gate = await self._detect_gate(page)
        if gate:
            raise self._with_partial_progress(
                GateBlocked(gate, "최종 확인 화면에서 결제/동의 관련 항목이 감지되었습니다"),
                accepted_terms, applied_coupon,
            )

        return shown, applied_coupon, accepted_terms, widget_coupons

    def _with_partial_progress(
        self, exc: Exception, accepted_terms: list[str], applied_coupon: Coupon | None,
    ) -> Exception:
        """실패로 멈추기 직전까지 이미 안전하게 처리한 진행 상황(자동 체크한 필수
        동의, 적용한 쿠폰)을 예외 자체에 실어 보낸다 - 호출자가 실패 원인과 함께
        "여기까지는 무엇을 했는지"도 이벤트 로그/알림에 남길 수 있게 한다. 기존
        예외 클래스를 수정하지 않고 인스턴스에 속성만 덧붙이므로 다른 곳(예:
        mock_adapter)의 기존 raise 는 전혀 영향받지 않는다."""
        exc.accepted_terms = accepted_terms
        exc.applied_coupon_name = applied_coupon.name if applied_coupon else None
        return exc

    async def _select_time_slot(self, page: Page, time_: str) -> None:
        """시간대 버튼 목록에서 ``time_`` 에 해당하는 버튼을 실제로 클릭한다.
        ``_read_modal_times`` 는 가용성만 읽고 아무 것도 클릭하지 않으므로, 여기서
        찾은 버튼이 비활성 상태면(레이스 컨디션으로 방금 마감된 경우) 강행하지 않고
        멈춘다."""
        groups = page.locator(sel.TIME_PERIOD_GROUP)
        gcount = await groups.count()
        for i in range(gcount):
            group = groups.nth(i)
            label_loc = group.locator(sel.TIME_PERIOD_LABEL).first
            period_label = (await label_loc.inner_text()).strip() if await label_loc.count() else ""
            buttons = group.locator("button")
            bcount = await buttons.count()
            for j in range(bcount):
                btn = buttons.nth(j)
                try:
                    text = (await btn.inner_text()).strip()
                except Exception:
                    continue
                if self._period_time_to_24h(period_label, text) != time_:
                    continue
                if await btn.is_disabled():
                    raise SlotUnavailableError(f"'{time_}' 시간 버튼이 방금 비활성 상태로 바뀌었습니다")
                await btn.click()
                return
        # 못 찾으면 조용히 넘어간다 - 위에서 이미 가용성 검사를 통과했으므로 이
        # 위젯에 시간대 버튼 자체가 없는(구조가 다른) 경우일 수 있다. 아래
        # _advance_past_next_button 도 마찬가지로 없으면 그냥 넘어가 기존
        # (구형/커스텀 위젯) 동작을 그대로 유지한다.
        log.info("'%s' 시간에 대응하는 시간대 버튼을 찾지 못해 클릭을 건너뜁니다.", time_)

    async def _advance_past_next_button(self, page: Page) -> None:
        """시간 선택 후 노출되는 '다음' 버튼을 눌러 요약/제출 화면으로 진입한다.
        이 버튼이 눌리기 전까지는 위젯이 시간 선택 모달에 머물러 있어, 이후
        ``_read_pending_summary`` 가 날짜/시간/인원과 무관한 페이지 텍스트를
        잘못 집어올 수 있다 (2026-09-15 실측, 실제 업체 A).
        이 버튼이 없는 위젯 구조(구형/커스텀)에서는 조용히 넘어가 기존 동작을
        그대로 유지한다."""
        next_btn = page.get_by_role("button", name=sel.NEXT_STEP_BUTTON_TEXT, exact=True).first
        if await next_btn.count() == 0:
            return
        await next_btn.click()
        try:
            await page.wait_for_selector(
                f"{sel.SUBMIT_BUTTON_SELECTOR}, {sel.BOOKING_SUMMARY_SELECTOR}", timeout=6000
            )
        except PwTimeoutError:
            pass  # best-effort - 이후 게이트/요약 탐색 단계가 그대로 처리한다

    async def _is_locator_disabled(self, loc) -> bool:
        """``Locator.is_disabled()`` 는 HTML ``disabled`` 속성만 확인한다. 실제
        네이버 예약 위젯의 제출 버튼은 그 대신 ``aria-disabled="true"`` 와
        ``SubmitButtonView__is_disabled__...`` 클래스만 붙는 것으로 실측 확인됐다
        (2026-09-15, 실제 업체 A) - 세 가지를 모두 확인해야 안전하다."""
        try:
            if await loc.is_disabled():
                return True
        except Exception:
            pass
        try:
            if (await loc.get_attribute("aria-disabled")) == "true":
                return True
        except Exception:
            pass
        try:
            cls = (await loc.get_attribute("class")) or ""
            if sel.SUBMIT_BUTTON_DISABLED_MARKER in cls:
                return True
        except Exception:
            pass
        return False

    async def _find_final_button_locator(self, page: Page):
        """실제 최종 제출 버튼을 찾는다. 네이버 예약 위젯 자체의 제출 버튼
        컴포넌트(``SubmitButtonView``)를 항상 먼저 시도한다 - 페이지 상단 헤더에도
        "예약하기"라는 동일 텍스트의 내비게이션 버튼(role=button)이 별도로 존재해서,
        텍스트만으로 첫 번째 버튼을 찾으면 그 헤더 버튼을 오인 클릭할 위험이
        실측으로 확인됐다 (2026-09-15, 실제 업체 A). 이 위젯 셀렉터가 없는
        (구형/커스텀) 위젯에서는 기존 텍스트 기반 스캔으로 폴백한다."""
        for widget_selector in sel.SUBMIT_BUTTON_SELECTORS:
            widget_btn = page.locator(widget_selector).first
            if await widget_btn.count():
                return widget_btn
        for text in sel.CONFIRM_BUTTON_TEXTS:
            loc = page.get_by_role("button", name=re.compile(re.escape(text))).first
            if await loc.count():
                return loc
        return None

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

        if result.coupon_note is None:
            # 예약 화면 자체에 내장된 쿠폰 UI에서 확인된 게 없으면(예: 이 위젯이
            # 아예 없는 업체 구조) /home 사전 조회 결과를 대신 보여준다 - 참고용,
            # 이 예약에 실제로 적용 가능한지에 대한 신뢰도는 더 낮다.
            result.coupon_name = coupon_name
            result.coupon_applicable = coupon_applicable
            result.coupon_already_held = coupon_already_held
            result.coupon_note = coupon_note
        return result

    async def _dry_run_inner(
        self, page: Page, store: ResolvedStore, target_date: str, time_: str, party_size: int,
    ) -> DryRunResult:
        base = dict(
            store_name=store.name, store_url=store.url, target_date=target_date,
            time=time_, party_size=party_size,
        )
        try:
            shown, _, accepted_terms, widget_coupons = await self._advance_to_confirm_screen(
                page, store, target_date, time_, party_size, coupon_id=None, dry_run=True
            )
        except NotOpenYetError as e:
            return DryRunResult(ok=False, stage="NOT_OPEN", message=str(e), **base)
        except SlotUnavailableError as e:
            return DryRunResult(ok=False, stage="SLOT_UNAVAILABLE", message=str(e), **base)
        except HumanVerificationRequired as e:
            # 예: AUTO_ACCEPT_REQUIRED_TERMS 가 필수/선택 여부를 판별할 수 없는
            # (새로운/다른 형태의) 동의 항목을 만나 멈춘 경우. 여기까지 이미
            # 안전하게 처리한 동의/쿠폰 정보가 있다면 참고용으로 함께 보여준다.
            shown_d, button_text_d, accepted_terms_d, widget_coupons_d = await self._dry_run_diagnostics(page)
            result = DryRunResult(
                ok=False, stage="NEEDS_HUMAN", message=str(e),
                validation_summary=shown_d, final_button_text=button_text_d,
                accepted_terms=accepted_terms_d, **base,
            )
            self._apply_widget_coupon_report(result, widget_coupons_d)
            return result
        except GateBlocked as e:
            # 게이트에 막혀도(절대 클릭/선택하지 않음) 화면에 이미 있는 요약/버튼
            # 문구와 지금까지 자동 체크된 필수 동의/쿠폰 정보는 참고용으로 읽어서
            # 남긴다 - 전부 순수 조회라 안전하고, 점검 결과의 가치가 커진다.
            shown_d, button_text_d, accepted_terms_d, widget_coupons_d = await self._dry_run_diagnostics(page)
            result = DryRunResult(
                ok=False, stage="GATE_BLOCKED", message=str(e), gate_reason=str(e.reason),
                validation_summary=shown_d, final_button_text=button_text_d,
                accepted_terms=accepted_terms_d, **base,
            )
            self._apply_widget_coupon_report(result, widget_coupons_d)
            return result
        except ReservationValidationError as e:
            shown_d, _, accepted_terms_d, widget_coupons_d = await self._dry_run_diagnostics(page)
            result = DryRunResult(
                ok=False, stage="VALIDATION_FAILED", message=str(e),
                accepted_terms=accepted_terms_d, **base,
            )
            self._apply_widget_coupon_report(result, widget_coupons_d)
            return result
        except Exception as e:
            return DryRunResult(ok=False, stage="ERROR", message=f"예상치 못한 오류: {e}", **base)

        button_text = await self._find_confirm_button_text(page)
        result = DryRunResult(
            ok=True, stage="READY_TO_CONFIRM",
            message="실제 예약과 동일하게 여기까지 진행했고, 최종 확인 버튼을 누르기 직전에 멈췄습니다.",
            validation_summary=shown, final_button_text=button_text,
            accepted_terms=accepted_terms, **base,
        )
        self._apply_widget_coupon_report(result, widget_coupons)
        return result

    async def _dry_run_diagnostics(self, page: Page) -> tuple[str | None, str | None, list[str], list[Coupon]]:
        """예외로 멈춘 뒤에도(절대 클릭/선택하지 않음) 화면에 이미 있는 참고
        정보를 최대한 읽어서 dry-run 결과에 포함시킨다 - 전부 순수 조회라 안전하다."""
        shown = None
        try:
            shown = await self._read_pending_summary(page)
        except Exception:
            pass
        button_text = None
        try:
            button_text = await self._find_confirm_button_text(page)
        except Exception:
            pass
        accepted_terms: list[str] = []
        try:
            accepted_terms = await self._checked_required_consent_names(page)
        except Exception:
            pass
        widget_coupons: list[Coupon] = []
        try:
            widget_coupons, _ = await self._handle_widget_coupons(page, apply=False)
        except Exception:
            pass
        return shown, button_text, accepted_terms, widget_coupons

    async def _checked_required_consent_names(self, page: Page) -> list[str]:
        """지금 이미 체크되어 있는 체크박스 중 '필수'로 분류되는 항목의 이름을
        돌려준다 - (auto_accept 가 이미 체크했을 수 있는) 상태를 예외로 멈춘
        뒤에도 참고 정보로 보여주기 위한 순수 조회."""
        names: list[str] = []
        checkboxes = page.locator('input[type="checkbox"]')
        count = await checkboxes.count()
        for i in range(count):
            cb = checkboxes.nth(i)
            try:
                if not await cb.is_checked():
                    continue
            except Exception:
                continue
            label_text = await self._consent_label_text(page, cb)
            if label_text and self._classify_consent_text(label_text) == "required_safe":
                names.append(label_text.strip())
        return names

    def _apply_widget_coupon_report(self, result: DryRunResult, widget_coupons: list[Coupon]) -> None:
        """예약 화면에 내장된 쿠폰 UI에서 읽은 목록을 dry-run 결과에 반영한다.
        아무 것도 찾지 못했으면 결과를 건드리지 않는다(호출자가 /home 사전
        조회 결과로 대신 채울 수 있게 한다)."""
        if not widget_coupons:
            return
        best_coupon = self._pick_best_unconditional_widget_coupon(widget_coupons)
        if best_coupon:
            result.coupon_name = best_coupon.name
            result.coupon_applicable = True
            result.coupon_already_held = True  # 별도 다운로드 없이 바로 선택 가능한 상태
            result.coupon_note = (
                "예약 화면에 내장된 쿠폰 UI에서 확인됨 - 실제 실행 시 자동으로 선택/적용됩니다 "
                "(이번 점검에서는 실제로 선택하지 않았습니다)"
            )
        else:
            gated_names = ", ".join(c.name for c in widget_coupons if c.requires_gate)
            result.coupon_applicable = False
            result.coupon_note = (
                f"예약 화면에 쿠폰이 있지만 모두 확인이 필요한(게이트) 쿠폰이라 자동 적용 대상이 아닙니다: {gated_names}"
                if gated_names else "예약 화면의 쿠폰 UI에서 적용 가능한 쿠폰을 찾지 못했습니다"
            )

    async def _find_confirm_button_text(self, page: Page) -> str | None:
        """``_click_confirm_button`` 과 동일한 탐색 순서로 버튼을 찾되 클릭하지 않는다.
        버튼이 비활성 상태면 문구 뒤에 그 사실을 덧붙인다."""
        loc = await self._find_final_button_locator(page)
        if loc is None:
            return None
        try:
            text = (await loc.inner_text()).strip()
        except Exception:
            return None
        if await self._is_locator_disabled(loc):
            return f"{text} (현재 비활성 상태 - 추가 입력/동의가 필요할 수 있습니다)"
        return text

    async def _verify_coupon_still_valid(self, page: Page, coupon_id: str) -> Coupon:
        """예약 화면 자체에서 쿠폰을 재확인할 수 있으면 재확인하고, 그럴 수 없는
        업체 구조라면(쿠폰이 /home 전용인 경우 등) 확신할 수 없으므로 할인 없이
        강행하지 않고 CouponUnavailableError 로 멈춘다."""
        coupons_here = await self._scan_coupons(page)
        coupon = next((c for c in coupons_here if c.coupon_id == coupon_id), None)
        if not coupon:
            raise CouponUnavailableError(
                coupon_id,
                "예약 화면에서 쿠폰을 재확인할 수 없습니다 (이 업체는 쿠폰이 예약 화면과 "
                "분리되어 있을 수 있음) - 확신 없이 할인 적용을 진행하지 않습니다",
            )
        if coupon.requires_gate or not coupon.held:
            raise CouponUnavailableError(coupon.name, "더 이상 자동 적용할 수 없는 상태로 바뀌었습니다")
        return coupon

    # -- date-chip widget (pcmap.place.naver.com booking) --------------------

    async def _check_via_date_chips(
        self, page: Page, target_date: str, party_size: int, leave_modal_open: bool = False
    ) -> AvailabilitySnapshot:
        checked_at = datetime.now(UTC).isoformat()
        target_dt = datetime.strptime(target_date, "%Y-%m-%d")
        label_re = re.compile(rf"^{target_dt.month}\.\s*{target_dt.day}\.\s")
        chip_label = page.locator(sel.DATE_CHIP_LABEL).filter(has_text=label_re)

        if await chip_label.count():
            label_el = chip_label.first
            cls = (await label_el.get_attribute("class")) or ""
            if sel.DATE_CHIP_LABEL_CLOSED_MARKER in cls:
                return AvailabilitySnapshot(page_state=SlotState.SOLD_OUT, slots=[], checked_at=checked_at)

            ancestor_btn = label_el.locator(sel.DATE_CHIP_CLICKABLE_ANCESTOR)
            if await ancestor_btn.count():
                await ancestor_btn.click()
            else:
                # "예약가능" 마커가 없는 알 수 없는 상태 - 안전하게 마감으로 취급
                return AvailabilitySnapshot(page_state=SlotState.SOLD_OUT, slots=[], checked_at=checked_at)
        else:
            # ~30일치 날짜 칩 목록에 없는 먼 미래 날짜 - 달력 모달에서 이동
            await self._open_calendar_modal(page)
            is_closed = await self._navigate_calendar_to(page, target_dt)
            if is_closed:
                return AvailabilitySnapshot(page_state=SlotState.SOLD_OUT, slots=[], checked_at=checked_at)

        return await self._read_modal_times(page, target_date, party_size, checked_at)

    async def _open_calendar_modal(self, page: Page) -> None:
        search_entry = page.get_by_text(sel.SEARCH_ENTRY_TEXT, exact=False).first
        if await search_entry.count():
            await search_entry.click()
            return
        any_chip_btn = page.locator('button[class*="_timeChipButton_"]').first
        if await any_chip_btn.count():
            await any_chip_btn.click()
            return
        raise RuntimeError("예약 모달을 열 수 있는 진입점을 찾지 못했습니다")

    async def _navigate_calendar_to(self, page: Page, target_dt: datetime) -> bool:
        """달력을 target_dt의 월로 이동시키고, 해당 날짜 셀을 클릭한다.
        마감/선택불가 상태면 클릭하지 않고 True를 반환한다."""
        expected_label = f"{target_dt.year}. {target_dt.month}"
        for _ in range(24):  # 최대 2년치 탐색 (안전장치)
            month_label_el = page.locator(sel.CALENDAR_MONTH_LABEL).first
            if await month_label_el.count() == 0:
                raise RuntimeError("달력 모달을 찾지 못했습니다")
            current_label = (await month_label_el.inner_text()).strip()
            if current_label == expected_label:
                break
            nav_buttons = page.locator(sel.CALENDAR_NAV_BUTTON)
            if await nav_buttons.count() < 2:
                raise RuntimeError("달력 이동(다음달) 버튼을 찾지 못했습니다")
            await nav_buttons.nth(1).click()
            await page.wait_for_timeout(300)
        else:
            raise RuntimeError(f"'{expected_label}' 월로 달력을 이동하지 못했습니다")

        day_spans = page.locator(sel.CALENDAR_DAY_SPAN).filter(has_text=re.compile(rf"^{target_dt.day}$"))
        if await day_spans.count() == 0:
            raise RuntimeError(f"{target_dt.date()} 날짜 셀을 달력에서 찾지 못했습니다")
        day_span = day_spans.first

        disabled_ancestor = day_span.locator(sel.CALENDAR_DISABLED_ANCESTOR)
        if await disabled_ancestor.count():
            aria_disabled = await disabled_ancestor.first.get_attribute("aria-disabled")
            if aria_disabled == "true":
                return True

        await day_span.click()
        await page.wait_for_timeout(300)
        return False

    async def _read_modal_times(
        self, page: Page, target_date: str, party_size: int, checked_at: str | None = None
    ) -> AvailabilitySnapshot:
        checked_at = checked_at or datetime.now(UTC).isoformat()
        await self._raise_if_needs_human(page)
        # 날짜 칩/달력 클릭 직후 모달(시간대 그룹)이 렌더링될 때까지 명시적으로 대기한다 -
        # 고정된 sleep 대신 실제 요소가 나타나는 것을 기다려 타이밍 경쟁을 피한다.
        try:
            await page.wait_for_selector(sel.TIME_PERIOD_GROUP, timeout=5000)
        except PwTimeoutError:
            pass  # 이 위젯에 시간대 그룹이 없을 수 있음 - 아래에서 빈 슬롯/UNKNOWN으로 처리됨
        await self._select_party_size(page, party_size)

        slots: list[TimeSlot] = []
        groups = page.locator(sel.TIME_PERIOD_GROUP)
        gcount = await groups.count()
        for i in range(gcount):
            group = groups.nth(i)
            label_loc = group.locator(sel.TIME_PERIOD_LABEL).first
            period_label = (await label_loc.inner_text()).strip() if await label_loc.count() else ""
            buttons = group.locator("button")
            bcount = await buttons.count()
            for j in range(bcount):
                btn = buttons.nth(j)
                try:
                    text = (await btn.inner_text()).strip()
                except Exception:
                    continue
                hhmm = self._period_time_to_24h(period_label, text)
                if not hhmm:
                    continue
                disabled = await btn.is_disabled()
                slots.append(TimeSlot(time=hhmm, available=not disabled))

        if slots:
            page_state = SlotState.OPEN if any(s.available for s in slots) else SlotState.SOLD_OUT
        else:
            page_state = SlotState.UNKNOWN
        return AvailabilitySnapshot(page_state=page_state, slots=slots, checked_at=checked_at)

    def _period_time_to_24h(self, period_label: str, text: str) -> str | None:
        m = re.match(r"^(\d{1,2}):(\d{2})$", text)
        if not m:
            return None
        h, minute = int(m.group(1)), int(m.group(2))
        if "오후" in period_label:
            if h != 12:
                h += 12
        elif "오전" in period_label:
            if h == 12:
                h = 0
        return f"{h:02d}:{minute:02d}"

    async def _select_party_size(self, page: Page, party_size: int) -> None:
        text = sel.PARTY_SIZE_BUTTON_TEXT.format(n=party_size)
        btn = page.get_by_role("button", name=text, exact=True)
        if await btn.count() == 0:
            btn = page.locator("button", has_text=text)
        if await btn.count() == 0:
            log.info("인원 선택 버튼('%s')을 찾지 못했습니다 - 이미 선택되어 있을 수 있습니다.", text)
            return
        cls = (await btn.first.get_attribute("class")) or ""
        if sel.PARTY_SIZE_SELECTED_MARKER in cls:
            return  # 이미 선택됨
        await btn.first.click()
        await page.wait_for_timeout(200)

    # -- generic text/marker-based fallback (widget 구조가 다른 업체용) ------

    async def _guess_store_name(self, page: Page) -> str:
        try:
            title = await page.title()
            return title.split("|")[0].split("-")[0].strip() or "알수없음"
        except Exception:
            return "알수없음"

    async def _raise_if_needs_human(self, page: Page) -> None:
        try:
            text = await page.inner_text("body")
        except Exception:
            return
        for marker in (*sel.CAPTCHA_MARKERS, *sel.REAUTH_MARKERS):
            if marker.lower() in text.lower():
                raise HumanVerificationRequired(f"사용자 확인이 필요한 화면 감지: '{marker}'")

    async def _detect_gate(self, page: Page) -> GateReason | None:
        try:
            text = await page.inner_text("body")
        except Exception:
            return None
        if any(m in text for m in sel.PAYMENT_MARKERS):
            return GateReason.PAYMENT_REQUIRED
        if any(m in text for m in sel.CANCEL_FEE_MARKERS):
            return GateReason.CANCEL_FEE_REQUIRED
        if await self._has_unchecked_required_consent(page, text):
            return GateReason.EXTRA_CONSENT_REQUIRED
        return None

    async def _has_unchecked_required_consent(self, page: Page, body_text: str) -> bool:
        """동의 관련 문구가 있어도 이미 (자동 체크로건 원래건) 체크되어 있으면
        게이트로 취급하지 않는다 - 순수 텍스트 매칭은 체크 상태를 반영하지 못한다.
        ``_auto_accept_required_terms`` 가 안전하게 체크할 수 있는 항목은 이미
        체크했을 것이므로, 여기서 남아있는 미체크 필수 항목이 있다면(체크박스가
        전혀 없는 구조 포함) AUTO_ACCEPT_REQUIRED_TERMS 가 적용되지 않는 경로
        (구형/커스텀 위젯 등)에 대한 안전망으로 그대로 게이트 처리한다."""
        if not any(m in body_text for m in sel.EXTRA_CONSENT_MARKERS):
            return False
        checkboxes = page.locator('input[type="checkbox"]')
        try:
            count = await checkboxes.count()
        except Exception:
            return True
        if count == 0:
            return True
        for i in range(count):
            cb = checkboxes.nth(i)
            try:
                if await cb.is_checked():
                    continue
            except Exception:
                return True
            label_text = await self._consent_label_text(page, cb)
            classification = self._classify_consent_text(label_text) if label_text else "ambiguous"
            if classification in ("required_safe", "ambiguous"):
                return True
        return False

    # -- AUTO_ACCEPT_REQUIRED_TERMS: 필수 동의만 자동 체크 ----------------------

    def _classify_consent_text(self, text: str) -> str:
        """반환값: 'required_safe' | 'optional_safe' | 'marketing' | 'ambiguous'."""
        is_marketing = any(m.lower() in text.lower() for m in sel.MARKETING_CONSENT_MARKERS)
        has_required = any(m in text for m in sel.REQUIRED_CONSENT_TEXT_MARKERS)
        has_optional = any(m in text for m in sel.OPTIONAL_CONSENT_TEXT_MARKERS)
        if is_marketing:
            return "marketing"
        if has_required and not has_optional:
            return "required_safe"
        if has_optional and not has_required:
            return "optional_safe"
        return "ambiguous"

    async def _consent_label_text(self, page: Page, cb) -> str | None:
        label = cb.locator("xpath=ancestor::label[1]")
        if await label.count():
            try:
                return (await label.first.inner_text()).strip()
            except Exception:
                return None
        try:
            cb_id = await cb.get_attribute("id")
        except Exception:
            cb_id = None
        if cb_id:
            for_label = page.locator(f'label[for="{cb_id}"]')
            if await for_label.count():
                try:
                    return (await for_label.first.inner_text()).strip()
                except Exception:
                    return None
        return None

    async def _auto_accept_required_terms(self, page: Page) -> list[str]:
        """AUTO_ACCEPT_REQUIRED_TERMS 정책: 예약 완료에 필수인(화면에 "필수"로
        명확히 표시된) 이용약관/개인정보 동의 체크박스만 자동으로 체크한다.
        - 마케팅/알림/광고성/멤버십 관련 문구가 있으면 "필수"로 표시돼 있어도
          절대 자동 체크하지 않는다 - 사용자가 사전 승인한 범위를 벗어난다.
          그런 항목이 필수로 표시된 비정상적인 경우는 추측하지 않고 멈춘다.
        - 필수/선택 여부를 텍스트로 확실히 판별할 수 없는(새로운/다른 형태의)
          항목이 있으면 추측하지 않고 즉시 멈춘다(NEEDS_HUMAN).
        체크한 항목의 이름 목록을 반환한다(이벤트 로그용)."""
        checked_names: list[str] = []
        checkboxes = page.locator('input[type="checkbox"]')
        count = await checkboxes.count()
        for i in range(count):
            cb = checkboxes.nth(i)
            try:
                # 화면에 보이지 않는(다른 단계/시나리오용으로 DOM에는 남아있지만
                # 지금은 display:none 등으로 숨겨진) 체크박스는 이 예약과 무관한
                # 항목이므로 정책 판단 대상에서 제외한다.
                if not await cb.is_visible():
                    continue
                if await cb.is_checked():
                    continue
            except Exception:
                pass

            label_text = await self._consent_label_text(page, cb)
            if label_text is None:
                raise HumanVerificationRequired(
                    "동의 체크박스의 안내 문구를 읽지 못해 필수/선택 여부를 판별할 수 없습니다"
                )

            classification = self._classify_consent_text(label_text)

            if classification == "marketing":
                if any(m in label_text for m in sel.REQUIRED_CONSENT_TEXT_MARKERS):
                    raise HumanVerificationRequired(
                        f"마케팅/알림성 항목이 필수로 표시되어 있어 자동으로 진행할 수 없습니다: '{label_text}'"
                    )
                continue  # 마케팅성 + 선택(또는 표시 없음) - 체크하지 않고 넘어간다

            if classification == "optional_safe":
                continue  # 선택 항목 - 체크하지 않는다

            if classification == "required_safe":
                label = cb.locator("xpath=ancestor::label[1]")
                click_target = label if await label.count() else cb
                await click_target.click()
                checked_names.append(label_text.strip())
                continue

            raise HumanVerificationRequired(
                f"필수/선택 여부를 명확히 판별할 수 없는 동의 항목이 있어 자동으로 진행할 수 없습니다: '{label_text}'"
            )
        return checked_names

    # -- 예약 화면 자체에 내장된 쿠폰 선택 UI ------------------------------------

    async def _open_widget_coupon_modal(self, page: Page) -> bool:
        btn = page.locator(sel.COUPON_SELECT_BUTTON_SELECTOR).first
        if await btn.count() == 0:
            return False
        try:
            if not await btn.is_visible():
                return False
        except Exception:
            return False
        await btn.click(timeout=5000)
        try:
            await page.wait_for_selector(
                f"{sel.COUPON_MODAL_ITEM_SELECTOR}, {sel.COUPON_MODAL_CLOSE_BUTTON_SELECTOR}", timeout=4000
            )
        except PwTimeoutError:
            pass  # 쿠폰이 0개일 수도 있음 - 모달 틀 자체는 열렸을 것
        return True

    async def _close_widget_coupon_modal(self, page: Page) -> None:
        cancel_btn = page.get_by_role("button", name=sel.COUPON_MODAL_CANCEL_BUTTON_TEXT, exact=True).first
        if await cancel_btn.count():
            try:
                await cancel_btn.click(timeout=2000)
                return
            except Exception:
                pass
        close_btn = page.locator(sel.COUPON_MODAL_CLOSE_BUTTON_SELECTOR).first
        if await close_btn.count():
            try:
                await close_btn.click(timeout=2000)
            except Exception:
                pass

    async def _parse_widget_coupon_item(self, item, index: int) -> Coupon | None:
        try:
            raw_text = (await item.inner_text()).strip()
        except Exception:
            return None
        if not raw_text:
            return None

        title_loc = item.locator(sel.COUPON_MODAL_ITEM_TITLE_SELECTOR).first
        lines = [ln.strip() for ln in raw_text.splitlines() if ln.strip()]
        if await title_loc.count():
            name = (await title_loc.inner_text()).strip()
        else:
            name = lines[0] if lines else f"쿠폰{index + 1}"

        gated = any(m in raw_text for m in sel.COUPON_GATE_MARKERS)

        discount_type, discount_value = "AMOUNT", 0.0
        percent_m = sel.COUPON_PERCENT_RE.search(raw_text)
        amount_m = sel.COUPON_AMOUNT_RE.search(raw_text)
        if percent_m:
            discount_type, discount_value = "PERCENT", float(percent_m.group(1))
        elif amount_m:
            discount_type, discount_value = "AMOUNT", float(amount_m.group(1).replace(",", ""))
        # 파싱 실패하면(예: "커피 1잔 증정"처럼 비금전적 혜택) discount_value=0 으로
        # 두고 raw_text 로 사람이 직접 확인할 수 있게 한다.

        date_loc = item.locator(sel.COUPON_MODAL_ITEM_DATE_SELECTOR).first
        date_text = (await date_loc.inner_text()).strip() if await date_loc.count() else ""
        expires_at = self._parse_widget_coupon_expiry(date_text)

        return Coupon(
            coupon_id="widget-" + hashlib.sha1(name.encode("utf-8")).hexdigest()[:10],
            name=name[:60],
            discount_type=discount_type,
            discount_value=discount_value,
            held=True,  # 이 모달에 뜬 쿠폰은 별도 "받기"(다운로드) 단계 없이 바로 선택 가능
            downloadable_free=False,
            requires_gate=gated,
            gate_reason=CouponGateReason.MARKETING_CONSENT_REQUIRED if gated else None,
            expires_at=expires_at,
            raw_text=raw_text,
        )

    def _parse_widget_coupon_expiry(self, date_text: str) -> str | None:
        """'2026. 10. 07. 까지' -> ISO datetime. 파싱 실패하면 None(만료 여부
        판단은 보수적으로 "만료 아님"으로 취급된다 - is_expired()가
        expires_at=None 을 그렇게 처리한다)."""
        m = sel.COUPON_MODAL_EXPIRY_RE.search(date_text)
        if not m:
            return None
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        try:
            return datetime(y, mo, d, 23, 59, 59, tzinfo=UTC).isoformat()
        except ValueError:
            return None

    def _pick_best_unconditional_widget_coupon(self, coupons: list[Coupon]) -> Coupon | None:
        """게이트가 없고 만료되지 않은(무조건) 쿠폰 중 혜택이 가장 큰 것을 고른다.
        이 모달에서 읽은 쿠폰은 최소금액 조건을 파싱하지 않으므로(min_amount 는
        항상 None) 전부 "무조건" 쿠폰으로 취급해도 안전하다."""
        candidates = [
            c for c in coupons
            if not c.requires_gate and c.min_amount is None and not coupon_utils.is_expired(c)
        ]
        if not candidates:
            return None
        return max(candidates, key=lambda c: c.discount_value)

    async def _handle_widget_coupons(self, page: Page, apply: bool) -> tuple[list[Coupon], Coupon | None]:
        """예약 화면에 내장된 쿠폰 선택 모달을 열어 쿠폰 목록을 읽는다.
        ``apply=True`` 면 무료/무조건이고 게이트가 없는 최적 쿠폰을 실제로
        선택("적용")한다 - 아직 최종 제출 전이라 언제든 되돌릴 수 있다.
        ``apply=False``(dry-run) 면 조회만 하고 아무 것도 선택하지 않은 채
        모달을 닫는다. 마케팅 동의 등 게이트가 있는 쿠폰은 apply=True 여도
        절대 선택하지 않는다(coupon_utils.pick_best 가 이미 제외한다)."""
        opened = await self._open_widget_coupon_modal(page)
        if not opened:
            return [], None

        items = page.locator(sel.COUPON_MODAL_ITEM_SELECTOR)
        count = await items.count()
        coupons: list[Coupon] = []
        for i in range(count):
            c = await self._parse_widget_coupon_item(items.nth(i), index=i)
            if c:
                coupons.append(c)

        applied: Coupon | None = None
        if apply and coupons:
            # coupon_utils.pick_best()는 결제금액을 모르면(amount=None) 아무것도
            # 고르지 않는다(퍼센트/최소금액 조건이 있는 쿠폰은 안전하게 평가할 수
            # 없기 때문) - 하지만 이 모달에서 읽은 쿠폰은 최소금액 조건을 파싱하지
            # 않으므로(min_amount=None) 전부 "무조건" 쿠폰이다. 게이트가 없고
            # 만료되지 않은 것 중 혜택이 가장 큰 것을 직접 고른다.
            best_coupon = self._pick_best_unconditional_widget_coupon(coupons)
            if best_coupon:
                idx = next(i for i, c in enumerate(coupons) if c.coupon_id == best_coupon.coupon_id)
                checkbox = items.nth(idx).locator(sel.COUPON_MODAL_ITEM_CHECKBOX_SELECTOR).first
                if await checkbox.count():
                    is_checked = (await checkbox.get_attribute("aria-checked")) == "true"
                    if not is_checked:
                        await checkbox.click()
                    applied = best_coupon

        if applied is not None:
            apply_btn = page.get_by_role("button", name=sel.COUPON_MODAL_APPLY_BUTTON_TEXT, exact=True).first
            if await apply_btn.count():
                await apply_btn.click()
            else:
                await self._close_widget_coupon_modal(page)
        else:
            await self._close_widget_coupon_modal(page)

        return coupons, applied

    async def _read_snapshot_generic(self, page: Page, target_date: str) -> AvailabilitySnapshot:
        checked_at = datetime.now(UTC).isoformat()
        text = await page.inner_text("body")

        if any(m in text for m in sel.BEFORE_OPEN_MARKERS):
            open_at_iso = None
            m = sel.OPEN_AT_TEXT_RE.search(text)
            if m:
                month, day, hour = (int(g) for g in m.groups())
                try:
                    year = datetime.now(UTC).year
                    open_at_iso = datetime(year, month, day, hour).isoformat()
                except ValueError:
                    open_at_iso = None
            return AvailabilitySnapshot(
                page_state=SlotState.BEFORE_OPEN,
                slots=[],
                open_at_text=text[:200],
                open_at_iso=open_at_iso,
                checked_at=checked_at,
            )

        slots: list[TimeSlot] = []
        candidates = page.locator("button, [role=button], li, span, a")
        count = min(await candidates.count(), 500)
        seen: set[str] = set()
        for i in range(count):
            el = candidates.nth(i)
            try:
                t = (await el.inner_text()).strip()
            except Exception:
                continue
            if not sel.TIME_TEXT_RE.match(t) or t in seen:
                continue
            seen.add(t)
            disabled = False
            try:
                disabled = bool(await el.get_attribute("disabled"))
                if not disabled:
                    cls = (await el.get_attribute("class")) or ""
                    disabled = any(m in cls.lower() for m in sel.DISABLED_CLASS_MARKERS)
                if not disabled:
                    aria_disabled = await el.get_attribute("aria-disabled")
                    disabled = aria_disabled == "true"
            except Exception:
                pass
            slots.append(TimeSlot(time=t, available=not disabled))

        if not slots and any(m in text for m in sel.SOLD_OUT_MARKERS):
            page_state = SlotState.SOLD_OUT
        elif any(s.available for s in slots):
            page_state = SlotState.OPEN
        elif slots:
            page_state = SlotState.SOLD_OUT
        else:
            page_state = SlotState.UNKNOWN

        return AvailabilitySnapshot(page_state=page_state, slots=slots, checked_at=checked_at)

    async def _read_pending_summary(self, page: Page) -> str | None:
        for selector in (
            sel.BOOKING_SUMMARY_SELECTOR,  # 네이버 예약 위젯 공통 요약 컴포넌트 - 가장 신뢰도 높음
            "[class*=summary]", "[class*=confirm]", "[class*=Summary]", "[role=dialog]",
        ):
            loc = page.locator(selector).first
            if await loc.count():
                try:
                    return (await loc.inner_text()).strip()
                except Exception:
                    continue
        return None

    def _time_to_period_12h(self, time_: str) -> str | None:
        """'17:00' -> '오후 5:00' 같은 네이버 예약 위젯 표기로 변환한다. 요약
        패널이 24시간제가 아니라 이 형식으로 시간을 보여주는 것이 실측으로
        확인됐다 (2026-09-15, 실제 업체 A) - 그대로면 클릭 직전 재검증이 항상
        시간 불일치로 실패한다."""
        m = re.match(r"^(\d{1,2}):(\d{2})$", time_)
        if not m:
            return None
        h, mm = int(m.group(1)), m.group(2)
        period = "오후" if h >= 12 else "오전"
        h12 = h - 12 if h > 12 else (12 if h == 0 else h)
        return f"{period} {h12}:{mm}"

    def _summary_matches(self, shown: str, target_date: str, time_: str, party_size: int) -> bool:
        dt = datetime.strptime(target_date, "%Y-%m-%d")
        date_ok = (
            target_date in shown
            or f"{dt.month}월 {dt.day}일" in shown
            or f"{dt.month}/{dt.day}" in shown
            or f"{dt.month}. {dt.day}." in shown
        )
        period_12h = self._time_to_period_12h(time_)
        time_ok = time_ in shown or (period_12h is not None and period_12h in shown)
        party_ok = f"{party_size}명" in shown
        return date_ok and time_ok and party_ok

    async def _click_confirm_button(self, page: Page) -> None:
        loc = await self._find_final_button_locator(page)
        if loc is None:
            raise RuntimeError(
                "최종 예약 확인 버튼을 찾지 못했습니다. naver_selectors.CONFIRM_BUTTON_TEXTS 를 "
                "실제 화면 문구에 맞게 보완해주세요."
            )
        if await self._is_locator_disabled(loc):
            raise ReservationValidationError(
                "최종 확인 버튼이 비활성 상태입니다 - 자동으로 진행하지 않습니다"
            )
        await loc.click(timeout=5000)

    async def _wait_for_success(self, page: Page) -> str:
        try:
            await page.wait_for_function(
                """(markers) => markers.some(m => document.body.innerText.includes(m))""",
                arg=list(sel.SUCCESS_MARKERS),
                timeout=15000,
            )
        except PwTimeoutError as e:
            raise RuntimeError(
                "예약 완료 화면을 확인하지 못했습니다. 실제로 예약이 완료됐는지 "
                "브라우저에서 직접 확인해주세요."
            ) from e
        return await page.inner_text("body")
