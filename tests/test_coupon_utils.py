from datetime import UTC, datetime, timedelta

from nrw.coupon_utils import estimate_discount, is_applicable, is_expired, pick_best
from nrw.models import Coupon, CouponGateReason


def make_coupon(**kwargs) -> Coupon:
    defaults = dict(
        coupon_id="c1", name="테스트쿠폰", discount_type="AMOUNT", discount_value=3000,
    )
    defaults.update(kwargs)
    return Coupon(**defaults)


def test_estimate_discount_amount_type_capped_by_total():
    c = make_coupon(discount_type="AMOUNT", discount_value=5000)
    assert estimate_discount(c, 10000) == 5000
    assert estimate_discount(c, 3000) == 3000  # 총액보다 큰 할인은 총액까지만


def test_estimate_discount_percent_type_with_cap():
    c = make_coupon(discount_type="PERCENT", discount_value=10, max_discount=2000)
    assert estimate_discount(c, 10000) == 1000
    assert estimate_discount(c, 100000) == 2000  # 캡 적용


def test_is_expired():
    future = (datetime.now(UTC) + timedelta(days=1)).isoformat()
    past = (datetime.now(UTC) - timedelta(days=1)).isoformat()
    assert not is_expired(make_coupon(expires_at=future))
    assert is_expired(make_coupon(expires_at=past))
    assert not is_expired(make_coupon(expires_at=None))


def test_is_applicable_excludes_gated_and_min_amount():
    gated = make_coupon(requires_gate=True, gate_reason=CouponGateReason.MEMBERSHIP_REQUIRED)
    assert not is_applicable(gated, 100000)

    min_amount_coupon = make_coupon(min_amount=50000)
    assert not is_applicable(min_amount_coupon, 30000)
    assert is_applicable(min_amount_coupon, 50000)


def test_pick_best_chooses_largest_discount_among_applicable():
    coupons = [
        make_coupon(coupon_id="a", name="A", discount_type="AMOUNT", discount_value=3000),
        make_coupon(coupon_id="b", name="B", discount_type="PERCENT", discount_value=20, max_discount=10000),
        make_coupon(coupon_id="c", name="C", discount_type="AMOUNT", discount_value=1000),
    ]
    pick = pick_best(coupons, amount=40000)
    assert pick.coupon.coupon_id == "b"  # 20% of 40000 = 8000, biggest
    assert pick.estimated_discount == 8000


def test_pick_best_excludes_gated_but_reports_them():
    coupons = [
        make_coupon(coupon_id="free", name="무료쿠폰", discount_type="AMOUNT", discount_value=1000),
        make_coupon(
            coupon_id="paid", name="멤버십쿠폰", discount_type="AMOUNT", discount_value=50000,
            requires_gate=True, gate_reason=CouponGateReason.MEMBERSHIP_REQUIRED,
        ),
    ]
    pick = pick_best(coupons, amount=100000)
    assert pick.coupon.coupon_id == "free"  # 훨씬 큰 할인이라도 게이트 쿠폰은 자동 선택 안 됨
    assert len(pick.gated_candidates) == 1
    assert pick.gated_candidates[0].coupon_id == "paid"


def test_pick_best_returns_none_when_nothing_applicable():
    coupons = [make_coupon(min_amount=1_000_000)]
    pick = pick_best(coupons, amount=10000)
    assert pick.coupon is None
    assert pick.estimated_discount == 0


def test_pick_best_handles_no_amount():
    coupons = [make_coupon()]
    pick = pick_best(coupons, amount=None)
    assert pick.coupon is None
