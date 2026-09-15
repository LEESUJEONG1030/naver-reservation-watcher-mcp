"""attempt_reservation() must leave a clear, comprehensive trail for every AUTO
reservation attempt - success or failure - in both the events table and via
the notifier (Windows Toast in production): outcome, chosen slot, applied
coupon, auto-accepted required consent terms, and reservation number.
Recoverable failures (validation mismatch, slot lost to a race, unexpected
error) must let the watch return to WATCHING rather than getting stuck.

Uses a minimal FakeAdapter (not the mock HTTP site) so these tests exercise
attempt_reservation()'s own exception-handling/logging contract directly,
independent of any particular adapter's browser automation.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from nrw.browser.base_adapter import BookingAdapter, ReservationResult, ResolvedStore
from nrw.config import Config, PollingConfig
from nrw.models import (
    CouponContext,
    GateBlocked,
    GateReason,
    HumanVerificationRequired,
    ReservationValidationError,
    SlotUnavailableError,
)
from nrw.notifier.base import Notifier, NotifyMessage
from nrw.watcher.core import attempt_reservation

pytestmark = pytest.mark.asyncio

TARGET_DATE = "2026-10-03"


class RecordingNotifier(Notifier):
    def __init__(self) -> None:
        self.messages: list[NotifyMessage] = []

    def notify(self, message: NotifyMessage) -> None:
        self.messages.append(message)


def make_cfg() -> Config:
    return Config(polling=PollingConfig(human_pause_sec=0.01, job_poll_interval_sec=0.1))


class FakeAdapter(BookingAdapter):
    """실제 브라우저 없이 attempt_reservation()의 예외 처리/로깅만 검증하기
    위한 최소 구현. reserve()만 시나리오에 맞게 예외를 던지거나 성공 결과를
    반환하고, 나머지는 빈 결과로 응답한다."""

    def __init__(self, raise_exc: Exception | None = None, result: ReservationResult | None = None):
        self.raise_exc = raise_exc
        self.result = result

    async def resolve_store(self, context, store_name, store_url) -> ResolvedStore:
        return ResolvedStore(name=store_name or "테스트업체", url=store_url or "http://example.invalid")

    async def check_availability(self, context, store, target_date, party_size):
        raise NotImplementedError

    async def reserve(self, context, store, target_date, time_, party_size, coupon_id=None) -> ReservationResult:
        if self.raise_exc is not None:
            raise self.raise_exc
        return self.result

    async def list_coupons(self, context, store, target_date, party_size) -> CouponContext:
        return CouponContext(estimated_amount=None, coupons=[])

    async def download_free_coupons(self, context, store, coupons):
        return coupons

    async def dry_run_reserve(self, context, store, target_date, time_, party_size):
        raise NotImplementedError


async def _run(exc: Exception | None = None, result: ReservationResult | None = None):
    adapter = FakeAdapter(raise_exc=exc, result=result)
    notifier = RecordingNotifier()
    cfg = make_cfg()
    store = ResolvedStore(name="테스트업체", url="http://example.invalid")
    outcome = await attempt_reservation(
        cfg, None, adapter, notifier,
        store=store, target_date=TARGET_DATE, time_="19:00", party_size=2, watch_id=1,
    )
    return outcome, notifier


async def test_success_notification_includes_slot_coupon_terms_and_reservation_no(isolated_db):
    result = ReservationResult(
        store_name="테스트업체", date=TARGET_DATE, time="19:00", party_size=2,
        naver_reservation_no="R-12345", raw_confirmation="예약 완료",
        coupon_name="무료 쿠폰", coupon_discount=3000,
        accepted_terms=["아래 내용에 모두 동의합니다*필수"],
    )
    outcome, notifier = await _run(result=result)

    assert outcome.status == "RESERVED"
    assert len(notifier.messages) == 1
    body = notifier.messages[0].body
    assert "19:00" in body
    assert "2명" in body
    assert "R-12345" in body
    assert "무료 쿠폰" in body
    assert "아래 내용에 모두 동의합니다*필수" in body

    events = isolated_db.list_events(watch_id=1)
    success_event = next(e for e in events if e["message"].startswith("예약 성공"))
    assert success_event["detail"]["naver_reservation_no"] == "R-12345"
    assert success_event["detail"]["coupon_name"] == "무료 쿠폰"
    assert success_event["detail"]["accepted_terms"] == ["아래 내용에 모두 동의합니다*필수"]
    assert success_event["detail"]["slot"] == "19:00"


async def test_validation_failure_reports_partial_progress_and_is_recoverable(isolated_db):
    exc = ReservationValidationError("클릭 직전 재검증 실패: 요청 != 화면")
    exc.accepted_terms = ["아래 내용에 모두 동의합니다*필수"]
    exc.applied_coupon_name = "무료 쿠폰"

    outcome, notifier = await _run(exc=exc)

    assert outcome.status == "ERROR"  # 재시도 가능한 실패 - process_one_watch가 WATCHING으로 되돌린다
    assert len(notifier.messages) == 1
    body = notifier.messages[0].body
    assert "19:00" in body
    assert "재검증" in body

    events = isolated_db.list_events(watch_id=1)
    error_event = next(e for e in events if e["message"].startswith("테스트업체"))
    assert error_event["level"] == "ERROR"
    assert error_event["detail"]["accepted_terms"] == ["아래 내용에 모두 동의합니다*필수"]
    assert error_event["detail"]["coupon_name"] == "무료 쿠폰"


async def test_slot_unavailable_is_reported_and_recoverable(isolated_db):
    outcome, notifier = await _run(exc=SlotUnavailableError("'19:00' 시간은 예약할 수 없는 상태입니다"))

    assert outcome.status == "UNAVAILABLE"
    assert len(notifier.messages) == 1
    assert "19:00" in notifier.messages[0].body

    events = isolated_db.list_events(watch_id=1)
    assert any("선점" in e["message"] for e in events)


async def test_gate_blocked_reports_reason_and_partial_progress(isolated_db):
    exc = GateBlocked(GateReason.PAYMENT_REQUIRED, "결제 안내 감지")
    exc.accepted_terms = []
    exc.applied_coupon_name = None

    outcome, notifier = await _run(exc=exc)

    assert outcome.status == "AWAITING_APPROVAL"
    assert any("결제" in m.body for m in notifier.messages)

    events = isolated_db.list_events(watch_id=1)
    gate_event = next(e for e in events if e["level"] == "WARN" and "결제" in e["message"])
    assert gate_event["detail"]["reason"] == str(GateReason.PAYMENT_REQUIRED)


async def test_human_verification_required_reports_and_pauses(isolated_db):
    outcome, notifier = await _run(exc=HumanVerificationRequired("캡차 감지"))

    assert outcome.status == "NEEDS_HUMAN"
    assert any("캡차" in m.body for m in notifier.messages)


async def test_unexpected_error_is_reported_and_recoverable(isolated_db):
    outcome, notifier = await _run(exc=RuntimeError("예상치 못한 네트워크 오류"))

    assert outcome.status == "ERROR"
    assert len(notifier.messages) == 1
    assert "예상치 못한 네트워크 오류" in notifier.messages[0].body

    events = isolated_db.list_events(watch_id=1)
    assert any(e["level"] == "ERROR" and "네트워크 오류" in e["message"] for e in events)
