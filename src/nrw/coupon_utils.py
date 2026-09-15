"""Coupon selection/estimation logic shared by watcher core and both adapters.

Pure functions only (no Playwright/DB) so they're trivially unit-testable and
identical regardless of which adapter produced the ``Coupon`` list.
"""
from __future__ import annotations

from datetime import UTC, datetime

from nrw.models import Coupon, CouponPick


def estimate_discount(coupon: Coupon, amount: int) -> int:
    """이 쿠폰을 이 결제금액에 적용했을 때 예상 할인액 (원)."""
    if coupon.discount_type == "PERCENT":
        raw = amount * (coupon.discount_value / 100.0)
        if coupon.max_discount is not None:
            raw = min(raw, coupon.max_discount)
        return int(min(raw, amount))
    # AMOUNT
    return int(min(coupon.discount_value, amount))


def is_expired(coupon: Coupon, as_of: datetime | None = None) -> bool:
    if not coupon.expires_at:
        return False
    as_of = as_of or datetime.now(UTC)
    try:
        expires = datetime.fromisoformat(coupon.expires_at)
    except ValueError:
        return False
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=UTC)
    return as_of >= expires


def is_applicable(coupon: Coupon, amount: int | None, as_of: datetime | None = None) -> bool:
    """자동으로 선택 대상이 될 수 있는 쿠폰인지 - 게이트가 걸려있거나, 만료됐거나,
    최소 결제금액 조건을 못 채우면 후보에서 제외한다."""
    if coupon.requires_gate:
        return False
    if is_expired(coupon, as_of):
        return False
    if coupon.min_amount is not None:
        if amount is None or amount < coupon.min_amount:
            return False
    return True


def pick_best(coupons: list[Coupon], amount: int | None, as_of: datetime | None = None) -> CouponPick:
    """예약 조건(예상 결제금액)에 실제로 쓸 수 있는 쿠폰 중 할인액이 가장 큰 것을 고른다.

    - requires_gate=True (유료 멤버십/구독/개인정보/마케팅동의/별도결제 필요)인 쿠폰은
      자동 선택 대상에서 항상 제외한다 (gated_candidates 로 따로 보고해 사용자가
      직접 확인할 수 있게 한다).
    - 만료됐거나 최소 결제금액 조건을 못 채우는 쿠폰도 제외한다.
    - 같은 할인액이면 먼저 나온 쿠폰(원래 목록 순서)을 우선한다.
    """
    applicable = [c for c in coupons if is_applicable(c, amount, as_of)]
    gated = [c for c in coupons if c.requires_gate and not is_expired(c, as_of)]

    if not applicable or amount is None:
        return CouponPick(coupon=None, estimated_discount=0, applicable_candidates=applicable, gated_candidates=gated)

    best = max(applicable, key=lambda c: estimate_discount(c, amount))
    return CouponPick(
        coupon=best,
        estimated_discount=estimate_discount(best, amount),
        applicable_candidates=applicable,
        gated_candidates=gated,
    )


def format_coupon_summary(pick: CouponPick) -> str | None:
    """알림 문구에 붙일 짧은 요약. 후보가 하나도 없으면 None."""
    parts = []
    if pick.coupon:
        parts.append(f"사용 가능한 쿠폰: {pick.coupon.name} (예상 할인 {pick.estimated_discount:,}원)")
    if pick.gated_candidates:
        names = ", ".join(c.name for c in pick.gated_candidates)
        parts.append(f"확인 필요(자동 적용 안 함): {names}")
    if not parts:
        return None
    return " / ".join(parts)
