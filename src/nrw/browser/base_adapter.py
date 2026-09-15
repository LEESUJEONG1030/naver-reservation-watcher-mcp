"""Common interface implemented by both ``naver_adapter`` (real Naver Booking)
and ``mock_adapter`` (local test site). ``watcher/core.py`` only talks to this
interface, so the exact same scheduling/decision logic can be exercised
end-to-end against the mock site without ever touching the real Naver
service.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from playwright.async_api import BrowserContext

from nrw.models import AvailabilitySnapshot, Coupon, CouponContext


@dataclass
class ResolvedStore:
    name: str
    url: str


@dataclass
class ReservationResult:
    store_name: str
    date: str
    time: str
    party_size: int
    naver_reservation_no: str | None
    raw_confirmation: str | None
    coupon_name: str | None = None
    coupon_discount: int | None = None
    # AUTO_ACCEPT_REQUIRED_TERMS 정책으로 자동 체크한 필수 동의 항목 이름들
    accepted_terms: list[str] = field(default_factory=list)


@dataclass
class DryRunResult:
    """실제로 최종 확인 버튼을 누르지 않고, 그 직전까지 진짜 예약과 동일한 절차
    (날짜/시간/인원 선택, 안전 게이트 확인, 클릭 직전 재검증)를 수행한 뒤 무엇을
    관찰했는지 보고하는 결과. ``ok=True`` 면 실제로 예약을 진행했어도 문제 없이
    확인 버튼까지 도달했을 것이라는 뜻이다."""

    ok: bool
    # READY_TO_CONFIRM | NOT_OPEN | SLOT_UNAVAILABLE | NEEDS_HUMAN | GATE_BLOCKED
    # | VALIDATION_FAILED | ERROR
    stage: str
    message: str
    store_name: str
    store_url: str
    target_date: str
    time: str
    party_size: int
    gate_reason: str | None = None
    validation_summary: str | None = None       # 클릭 직전 화면에서 읽은 요약 텍스트
    final_button_text: str | None = None        # 실제라면 여기서 누르게 될 버튼 텍스트
    coupon_name: str | None = None
    coupon_applicable: bool = False
    coupon_already_held: bool = False
    coupon_note: str | None = None
    # AUTO_ACCEPT_REQUIRED_TERMS 정책으로 자동 체크(했을) 필수 동의 항목 이름들
    accepted_terms: list[str] = field(default_factory=list)


class BookingAdapter(ABC):
    """One adapter instance is used for a single browser context/session."""

    @abstractmethod
    async def resolve_store(
        self, context: BrowserContext, store_name: str | None, store_url: str | None
    ) -> ResolvedStore:
        """업체명 또는 URL을 실제 예약 페이지 URL로 변환."""

    @abstractmethod
    async def check_availability(
        self, context: BrowserContext, store: ResolvedStore, target_date: str, party_size: int
    ) -> AvailabilitySnapshot:
        """지정 날짜의 예약 가능 시간을 조회. CAPTCHA/추가인증이 보이면
        ``HumanVerificationRequired`` 를 발생시켜야 한다."""

    @abstractmethod
    async def reserve(
        self,
        context: BrowserContext,
        store: ResolvedStore,
        target_date: str,
        time_: str,
        party_size: int,
        coupon_id: str | None = None,
    ) -> ReservationResult:
        """실제 예약을 시도한다.

        구현체는 반드시:
        1. 결제/선결제/보증금/취소수수료/추가동의가 필요하면 진행하지 않고
           ``GateBlocked`` 를 발생시킨다.
        2. 최종 확인(예약) 버튼을 누르기 직전, 화면에 표시된 날짜/시간/인원을
           다시 읽어 요청값과 다르면 ``ReservationValidationError`` 를 발생시키고
           절대 클릭하지 않는다.
        3. CAPTCHA/추가인증/비밀번호 재입력이 보이면 ``HumanVerificationRequired``
           를 발생시킨다.
        4. ``coupon_id`` 가 주어지면, 최종 클릭 직전에 그 쿠폰이 여전히
           유효/적용 가능한지 다시 확인한다. 더 이상 유효하지 않으면
           ``CouponUnavailableError`` 를 발생시키고(할인 없이 예약을 강행하지
           않음) 절대 클릭하지 않는다. 유효하면 그 쿠폰을 적용해 예약하고
           결과의 coupon_name/coupon_discount 를 채운다. ``coupon_id`` 가 없으면
           예약 화면 자체에 내장된 쿠폰 선택 UI(있는 경우)도 확인해, 무료/무조건
           이고 게이트가 없는 쿠폰이 있으면 자동으로 선택/적용한다.
        5. AUTO_ACCEPT_REQUIRED_TERMS: 예약 완료에 필수인(화면에 "필수"로 명확히
           표시된) 이용약관/개인정보 동의 체크박스만 자동으로 체크한다. 마케팅/
           알림/광고성/멤버십 관련 동의는 필수로 표시돼 있어도 절대 자동 체크하지
           않는다. 필수/선택 여부를 확실히 판별할 수 없는(새로운/다른 형태의)
           동의 항목이 있으면 추측하지 않고 ``HumanVerificationRequired`` 를
           발생시킨다. 자동 체크한 항목 이름은 결과의 ``accepted_terms`` 에 담는다.
        """

    @abstractmethod
    async def list_coupons(
        self, context: BrowserContext, store: ResolvedStore, target_date: str, party_size: int
    ) -> CouponContext:
        """이 업체에서 지금 받을 수 있는/보유 중인 쿠폰 목록과 예상 결제금액을 조회한다.
        다운로드는 하지 않는다 (``download_free_coupons`` 가 담당)."""

    @abstractmethod
    async def download_free_coupons(
        self, context: BrowserContext, store: ResolvedStore, coupons: list[Coupon]
    ) -> list[Coupon]:
        """무료로 즉시 받을 수 있고(``downloadable_free``), 아직 보유하지 않았고
        (``held`` False), 게이트가 없는(``requires_gate`` False) 쿠폰만 실제로
        다운로드(받기 클릭)한다. 이미 보유 중인 쿠폰은 다시 클릭하지 않는다
        (중복 다운로드 방지). 게이트가 있는 쿠폰은 절대 클릭하지 않는다.
        업데이트된 쿠폰 목록(다운로드한 것들은 held=True)을 반환한다."""

    @abstractmethod
    async def dry_run_reserve(
        self, context: BrowserContext, store: ResolvedStore, target_date: str, time_: str, party_size: int
    ) -> DryRunResult:
        """실제 예약과 완전히 동일한 절차(업체 확인은 호출자가 resolve_store 로
        이미 했다고 가정 - 날짜 선택, 인원 선택, 시간 선택, 쿠폰 탐지, 안전
        게이트 확인, 클릭 직전 재검증)를 실제 사이트에서 그대로 수행하되,
        최종 확인 버튼은 **절대 클릭하지 않는다**. 구현체는 ``reserve()`` 와
        동일한 내부 준비 로직을 공유해서(코드 중복으로 인한 드리프트 없이)
        신뢰할 수 있는 점검 도구가 되도록 해야 한다.

        쿠폰은 조회만 하고 실제로 다운로드/선택/적용 클릭은 하지 않는다(예약
        화면 자체에 내장된 쿠폰 선택 UI 포함) - "받을 수 있는지/적용 가능한지"만
        보고한다. 쿠폰을 실제로 받거나 선택하는 것도 사용자 계정/진행 중인
        예약 폼에 실제 변화를 주는 행위이기 때문이다.

        AUTO_ACCEPT_REQUIRED_TERMS(필수 동의 자동 체크)는 예외다 - 체크박스를
        체크하는 것 자체는 최종 제출 전까지 아무 지속적 효과가 없으므로, 실제
        예약과 동일하게 여기서도 수행하고 그 결과(어떤 항목을 체크했는지)를
        보고한다.

        게이트/인증 화면이 감지되면 예외를 던지지 않고 ``DryRunResult(ok=False,
        stage=...)`` 로 어디서 멈췄는지 보고한다."""
