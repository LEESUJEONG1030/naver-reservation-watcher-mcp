"""Shared enums and small data structures used across watcher/mcp_server/browser."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class ReservationMode(StrEnum):
    AUTO = "AUTO"
    ASK = "ASK"
    NOTIFY = "NOTIFY"


class WatchStatus(StrEnum):
    PENDING = "PENDING"              # 등록됐지만 아직 감시 시작 시각 전
    WAITING_OPEN = "WAITING_OPEN"    # 감시 중, 아직 예약 오픈 전으로 보임
    WATCHING = "WATCHING"            # 감시 중, 오픈은 됐지만 조건에 맞는 자리 없음
    AWAITING_APPROVAL = "AWAITING_APPROVAL"  # 후보 발견, 사용자 승인 대기(ASK) 또는 결제/동의 게이트
    NEEDS_HUMAN = "NEEDS_HUMAN"       # CAPTCHA/추가인증/비밀번호 재입력 등 사용자 개입 필요
    RESERVED = "RESERVED"            # 예약 성공, 감시 종료
    EXPIRED = "EXPIRED"              # watch_end_at 경과, 감시 종료
    CANCELLED = "CANCELLED"          # 사용자가 감시 삭제
    ERROR = "ERROR"                  # 반복 에러


class JobType(StrEnum):
    CHECK_NOW = "CHECK_NOW"
    RESERVE_NOW = "RESERVE_NOW"
    APPROVE = "APPROVE"
    DECLINE = "DECLINE"


class JobStatus(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    DONE = "DONE"
    ERROR = "ERROR"


class SlotState(StrEnum):
    """네이버(또는 mock) 예약 페이지에서 읽어낸 전체 페이지 상태."""

    BEFORE_OPEN = "BEFORE_OPEN"
    OPEN = "OPEN"
    SOLD_OUT = "SOLD_OUT"
    UNKNOWN = "UNKNOWN"


class GateReason(StrEnum):
    """AUTO 모드 자동 진행을 막는 이유 (안전 게이트)."""

    PAYMENT_REQUIRED = "PAYMENT_REQUIRED"
    PREPAYMENT_REQUIRED = "PREPAYMENT_REQUIRED"
    DEPOSIT_REQUIRED = "DEPOSIT_REQUIRED"
    CANCEL_FEE_REQUIRED = "CANCEL_FEE_REQUIRED"
    EXTRA_CONSENT_REQUIRED = "EXTRA_CONSENT_REQUIRED"


class CouponGateReason(StrEnum):
    """쿠폰을 자동으로 받거나 적용하지 않고 사용자 확인이 필요한 이유."""

    MEMBERSHIP_REQUIRED = "MEMBERSHIP_REQUIRED"        # 유료 멤버십 가입 필요
    SUBSCRIPTION_REQUIRED = "SUBSCRIPTION_REQUIRED"    # 구독 필요
    EXTRA_INFO_REQUIRED = "EXTRA_INFO_REQUIRED"        # 개인정보 추가 제공 필요
    MARKETING_CONSENT_REQUIRED = "MARKETING_CONSENT_REQUIRED"  # 마케팅 동의 필요
    PAYMENT_REQUIRED = "PAYMENT_REQUIRED"              # 별도 결제 필요


@dataclass
class TimeSlot:
    time: str  # "HH:MM"
    available: bool = True
    remaining: int | None = None  # 남은 자리 수 (알 수 있는 경우)


@dataclass
class AvailabilitySnapshot:
    """한 번의 체크에서 얻은 결과."""

    page_state: SlotState
    slots: list[TimeSlot] = field(default_factory=list)
    open_at_text: str | None = None  # "10월 3일 10시부터 예약 가능" 같은 원문
    open_at_iso: str | None = None   # 파싱에 성공하면 ISO datetime
    checked_at: str = ""  # ISO timestamp, 채워짐

    def available_times(self) -> list[str]:
        return [s.time for s in self.slots if s.available]


@dataclass
class ReservationCandidate:
    time: str
    party_size: int
    priority_rank: int | None = None  # None이면 range 매칭(우선순위 미지정)


@dataclass
class Coupon:
    """네이버(또는 mock) 예약 페이지에서 읽어낸 쿠폰 한 장."""

    coupon_id: str
    name: str
    discount_type: str  # "PERCENT" | "AMOUNT"
    discount_value: float  # PERCENT면 %, AMOUNT면 원
    min_amount: int | None = None       # 최소 결제금액 조건 (없으면 무조건)
    max_discount: int | None = None     # PERCENT 할인의 최대 할인액 캡
    held: bool = False                  # 이미 받아서 보유 중인지 (재다운로드 방지)
    downloadable_free: bool = True      # 무료로 즉시 다운로드 가능한지
    requires_gate: bool = False         # 자동으로 받거나 적용하면 안 되는 쿠폰인지
    gate_reason: CouponGateReason | None = None
    expires_at: str | None = None       # ISO datetime, 알 수 있는 경우
    raw_text: str | None = None         # 원문 (디버깅/알림용)


@dataclass
class CouponContext:
    """한 번의 쿠폰 조회 결과: 예상 결제금액 + 그 시점에 보이는 쿠폰 전체."""

    estimated_amount: int | None
    coupons: list[Coupon] = field(default_factory=list)


@dataclass
class CouponPick:
    """예약 조건(금액)에 대해 고른 최적 쿠폰 - 없으면 coupon=None."""

    coupon: Coupon | None
    estimated_discount: int
    applicable_candidates: list[Coupon] = field(default_factory=list)  # 참고용(로그/알림)
    gated_candidates: list[Coupon] = field(default_factory=list)       # 확인 필요해서 제외된 것들


class CouponUnavailableError(Exception):
    """예약 직전 재검증에서, 앞서 선택했던 쿠폰이 더 이상 유효/적용 불가능해진 경우."""

    def __init__(self, coupon_name: str, detail: str = ""):
        self.coupon_name = coupon_name
        self.detail = detail
        super().__init__(f"쿠폰 '{coupon_name}' 재검증 실패: {detail}")


class HumanVerificationRequired(Exception):
    """CAPTCHA, 2FA, 비밀번호 재입력 등 사용자 개입이 필요한 상황."""


class GateBlocked(Exception):
    """결제/선결제/보증금/취소수수료/추가동의 등으로 자동 진행을 막아야 하는 상황."""

    def __init__(self, reason: GateReason, detail: str = ""):
        self.reason = reason
        self.detail = detail
        super().__init__(f"{reason}: {detail}")


class ReservationValidationError(Exception):
    """예약 버튼 클릭 직전 재검증 실패."""


class NotOpenYetError(Exception):
    """아직 예약이 열리지 않은 상태에서 reserve()가 호출된 경우."""


class SlotUnavailableError(Exception):
    """요청한 시간이 이미 마감되어 선택할 수 없는 경우."""
