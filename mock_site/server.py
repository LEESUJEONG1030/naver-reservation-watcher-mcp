"""Local mock 'Naver-style' booking site used purely for testing.

Lets tests (and the developer, manually) drive a full reservation lifecycle
- 오픈 전 -> 오픈 -> 마감 -> 취소자리 발생 -> 예약 성공 -
without ever touching the real Naver service. ``mock_adapter.py`` drives this
page with Playwright using the exact same ``BookingAdapter`` interface that
``naver_adapter.py`` implements for the real site.

Also simulates Naver-style coupons (별도 "쿠폰" 섹션): 무료 다운로드 가능한 쿠폰,
보유 중(이미 받음) 쿠폰, 확인이 필요한(게이트) 쿠폰, 기간만료 쿠폰을 모두 흉내낼 수
있다. 결제/예약 금액은 인원 수 * 1인당 가격으로 계산한다.

Run standalone for manual poking around:
    python -m mock_site.server
Then open http://127.0.0.1:8791/booking?date=2026-10-03&party=2

Admin API (JSON, used by tests to move the state machine):
    POST /admin/open     {}                                  -> opens booking
    POST /admin/slots    {"19:00": true, "18:00": false, ..} -> merge slot availability
    POST /admin/gate     {"require_payment": bool, "require_captcha": bool}
    POST /admin/coupons  {"coupons": [...]}                   -> replace the full coupon list
    POST /admin/reset    {}                                  -> back to BEFORE_OPEN, no coupons
    GET  /admin/state                                         -> current state JSON
"""
from __future__ import annotations

import html
import json
import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

DEFAULT_SLOTS = {"18:00": True, "18:30": True, "19:00": True, "19:30": True, "20:00": True}
STORE_NAME = "테스트식당"
PRICE_PER_PERSON = 30000


@dataclass
class MockState:
    is_open: bool = False
    open_at_text: str = "10월 3일 10시부터 예약 가능합니다"
    slots: dict[str, bool] = field(default_factory=lambda: dict(DEFAULT_SLOTS))
    require_payment: bool = False
    require_captcha: bool = False
    reservation_seq: int = 1000
    last_reservation: dict | None = None
    coupons: list[dict] = field(default_factory=list)

    def reset(self) -> None:
        self.is_open = False
        self.slots = dict(DEFAULT_SLOTS)
        self.require_payment = False
        self.require_captcha = False
        self.last_reservation = None
        self.coupons = []


STATE = MockState()
_LOCK = threading.Lock()


def _coupon_expired(coupon: dict) -> bool:
    expires_at = coupon.get("expires_at")
    if not expires_at:
        return False
    try:
        expires = datetime.fromisoformat(expires_at)
    except ValueError:
        return False
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=UTC)
    return datetime.now(UTC) >= expires


def _coupon_discount(coupon: dict, amount: int) -> int:
    if coupon.get("discount_type") == "PERCENT":
        raw = amount * (coupon.get("discount_value", 0) / 100.0)
        max_discount = coupon.get("max_discount")
        if max_discount is not None:
            raw = min(raw, max_discount)
        return int(min(raw, amount))
    return int(min(coupon.get("discount_value", 0), amount))


def _coupon_usable_now(coupon: dict, amount: int) -> bool:
    """게이트 없고, 만료 안 됐고, 최소금액 조건을 만족하는 쿠폰인지."""
    if coupon.get("requires_gate"):
        return False
    if _coupon_expired(coupon):
        return False
    min_amount = coupon.get("min_amount")
    if min_amount is not None and amount < min_amount:
        return False
    return True


def _coupon_row_html(coupon: dict, amount: int) -> str:
    cid = html.escape(coupon["id"])
    name = html.escape(coupon["name"])
    if coupon.get("discount_type") == "PERCENT":
        desc = f"{coupon['discount_value']:.0f}% 할인"
        if coupon.get("max_discount"):
            desc += f" (최대 {coupon['max_discount']:,}원)"
    else:
        desc = f"{coupon['discount_value']:,.0f}원 할인"
    if coupon.get("min_amount"):
        desc += f" / {coupon['min_amount']:,}원 이상"

    expired = _coupon_expired(coupon)
    gated = bool(coupon.get("requires_gate"))
    held = bool(coupon.get("held"))

    if expired:
        control = '<span class="coupon-status">기간만료</span>'
    elif gated:
        reason = html.escape(coupon.get("gate_reason") or "확인 필요")
        control = f'<span class="coupon-status" data-gate-reason="{reason}">확인 필요 ({reason})</span>'
    elif held:
        control = (
            '<span class="coupon-status">보유중</span> '
            f'<button class="coupon-apply-btn" data-coupon-id="{cid}">적용</button>'
        )
    elif coupon.get("downloadable_free", True):
        control = f'<button class="coupon-download-btn" data-coupon-id="{cid}">받기</button>'
    else:
        control = '<span class="coupon-status">다운로드 불가</span>'

    return (
        f'<div class="coupon-row" data-coupon-id="{cid}" '
        f'data-usable="{"true" if _coupon_usable_now(coupon, amount) and held else "false"}">'
        f'<span class="coupon-name">{name}</span> '
        f'<span class="coupon-desc">{desc}</span> {control}'
        f"</div>"
    )


def _page(date: str, party: int) -> str:
    store = html.escape(STORE_NAME)
    date_esc = html.escape(date)
    amount = party * PRICE_PER_PERSON

    if not STATE.is_open:
        body = f"""
        <div id="before-open-panel" class="panel">
          <p class="banner">{html.escape(STATE.open_at_text)}</p>
          <p>아직 예약이 열리지 않았습니다.</p>
        </div>
        """
    else:
        slot_buttons = "\n".join(
            f'<button class="slot" data-time="{t}" {"disabled" if not avail else ""}>'
            f'{t} {"마감" if not avail else "예약가능"}</button>'
            for t, avail in sorted(STATE.slots.items())
        )
        coupon_rows = "\n".join(_coupon_row_html(c, amount) for c in STATE.coupons)
        coupon_section = (
            f'<div id="coupons"><p class="banner">쿠폰 (예상 결제금액 {amount:,}원)</p>{coupon_rows}</div>'
            if STATE.coupons
            else ""
        )
        body = f"""
        <div id="open-panel" class="panel">
          <p class="banner">예약 가능한 시간을 선택하세요</p>
          <div id="slots">{slot_buttons}</div>
          <label>인원
            <select id="partySize">
              {''.join(f'<option value="{n}">{n}명</option>' for n in range(1, 9))}
            </select>
          </label>
          {coupon_section}
          <button id="reserve-btn">예약하기</button>
          <div id="result-area"></div>
        </div>
        """

    require_payment_js = "true" if STATE.require_payment else "false"
    require_captcha_js = "true" if STATE.require_captcha else "false"

    return f"""<!doctype html>
<html lang="ko">
<head>
<meta charset="utf-8">
<title>{store} - 예약 (mock)</title>
<style>
body {{ font-family: sans-serif; max-width: 480px; margin: 40px auto; }}
.panel {{ border: 1px solid #ccc; padding: 16px; border-radius: 8px; }}
.slot {{ margin: 4px; padding: 8px 12px; }}
.slot.selected {{ background: #1a73e8; color: white; }}
.slot:disabled {{ color: #999; }}
.coupon-row {{ padding: 6px 0; border-bottom: 1px solid #eee; }}
.coupon-row.selected {{ background: #e8f0fe; }}
#payment-panel, #captcha-panel, #confirm-panel, #success-panel {{ border: 1px solid #999; margin-top: 12px; padding: 12px; }}
</style>
</head>
<body>
<h1>{store}</h1>
<p>날짜: {date_esc} / 인원: {party}명</p>
{body}
<script>
window.__requirePayment = {require_payment_js};
window.__requireCaptcha = {require_captcha_js};
window.__date = {json.dumps(date)};
window.__amount = {amount};

(function() {{
  var selected = null;
  var selectedCouponId = null;
  var selectedCouponLabel = null;

  document.querySelectorAll('.slot').forEach(function(btn) {{
    btn.addEventListener('click', function() {{
      if (btn.disabled) return;
      document.querySelectorAll('.slot').forEach(function(b) {{ b.classList.remove('selected'); }});
      btn.classList.add('selected');
      selected = btn.getAttribute('data-time');
    }});
  }});

  document.querySelectorAll('.coupon-download-btn').forEach(function(btn) {{
    btn.addEventListener('click', function() {{
      var couponId = btn.getAttribute('data-coupon-id');
      fetch('/api/coupon/download', {{
        method: 'POST',
        headers: {{'Content-Type': 'application/json'}},
        body: JSON.stringify({{coupon_id: couponId}})
      }}).then(function(r) {{ return r.json(); }}).then(function(data) {{
        if (data.ok) {{
          location.reload();
        }} else {{
          alert(data.error || '다운로드 실패');
        }}
      }});
    }});
  }});

  document.querySelectorAll('.coupon-apply-btn').forEach(function(btn) {{
    btn.addEventListener('click', function() {{
      document.querySelectorAll('.coupon-row').forEach(function(r) {{ r.classList.remove('selected'); }});
      var row = btn.closest('.coupon-row');
      row.classList.add('selected');
      selectedCouponId = btn.getAttribute('data-coupon-id');
      selectedCouponLabel = row.querySelector('.coupon-name').textContent;
    }});
  }});

  var reserveBtn = document.getElementById('reserve-btn');
  if (reserveBtn) {{
    reserveBtn.addEventListener('click', function() {{
      var resultArea = document.getElementById('result-area');
      resultArea.innerHTML = '';
      if (!selected) {{
        resultArea.innerHTML = '<p style="color:red">시간을 선택하세요</p>';
        return;
      }}
      var party = document.getElementById('partySize').value;

      if (window.__requirePayment) {{
        resultArea.innerHTML =
          '<div id="payment-panel"><p>선결제(예약금)가 필요합니다.</p>' +
          '<label><input type="checkbox" id="pay-agree"> 결제 및 취소수수료 규정에 동의합니다</label>' +
          '<br><button id="pay-btn">결제하기</button></div>';
        return;
      }}
      if (window.__requireCaptcha) {{
        resultArea.innerHTML =
          '<div id="captcha-panel"><p>보안 인증(캡차/추가 인증)이 필요합니다. 브라우저에서 직접 확인해주세요.</p></div>';
        return;
      }}

      var couponLine = '';
      if (selectedCouponId) {{
        couponLine = ' / 쿠폰: ' + selectedCouponLabel;
      }}
      resultArea.innerHTML =
        '<div id="confirm-panel"><p>아래 내용으로 예약하시겠습니까?</p>' +
        '<p id="confirm-summary">날짜: ' + window.__date + ' / 시간: ' + selected + ' / 인원: ' + party + '명' +
        couponLine + '</p>' +
        '<button id="confirm-btn">확인</button></div>';

      document.getElementById('confirm-btn').addEventListener('click', function() {{
        fetch('/api/confirm', {{
          method: 'POST',
          headers: {{'Content-Type': 'application/json'}},
          body: JSON.stringify({{
            date: window.__date, time: selected, party_size: parseInt(party, 10),
            coupon_id: selectedCouponId
          }})
        }}).then(function(r) {{ return r.json(); }}).then(function(data) {{
          if (data.ok) {{
            var couponSuccessLine = '';
            if (data.applied_coupon) {{
              couponSuccessLine = ' / 적용쿠폰: ' + data.applied_coupon.name +
                ' / 할인액: ' + data.applied_coupon.discount + '원';
            }}
            resultArea.innerHTML =
              '<div id="success-panel"><p>예약이 완료되었습니다.</p>' +
              '<p id="success-summary">' + data.store_name + ' / ' + data.date + ' ' + data.time +
              ' / ' + data.party_size + '명 / 예약번호: ' + data.reservation_no +
              couponSuccessLine + '</p></div>';
          }} else {{
            resultArea.innerHTML = '<p style="color:red">' + data.error + '</p>';
          }}
        }});
      }});
    }});
  }}
}})();
</script>
</body>
</html>"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):  # noqa: A002 - silence default logging
        pass

    def _send_json(self, obj, status=200):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, text, status=200):
        body = text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        if not length:
            return {}
        raw = self.rfile.read(length)
        return json.loads(raw) if raw else {}

    def do_GET(self):  # noqa: N802
        parsed = urlparse(self.path)
        qs = parse_qs(parsed.query)
        if parsed.path == "/booking":
            date = qs.get("date", ["2026-01-01"])[0]
            party = int(qs.get("party", ["1"])[0])
            with _LOCK:
                self._send_html(_page(date, party))
        elif parsed.path == "/admin/state":
            with _LOCK:
                self._send_json({
                    "is_open": STATE.is_open,
                    "open_at_text": STATE.open_at_text,
                    "slots": STATE.slots,
                    "require_payment": STATE.require_payment,
                    "require_captcha": STATE.require_captcha,
                    "last_reservation": STATE.last_reservation,
                    "coupons": STATE.coupons,
                })
        else:
            self._send_json({"error": "not found"}, status=404)

    def do_POST(self):  # noqa: N802
        parsed = urlparse(self.path)
        payload = self._read_json()
        with _LOCK:
            if parsed.path == "/admin/open":
                STATE.is_open = True
                self._send_json({"ok": True})
            elif parsed.path == "/admin/slots":
                STATE.slots.update({k: bool(v) for k, v in payload.items()})
                self._send_json({"ok": True, "slots": STATE.slots})
            elif parsed.path == "/admin/gate":
                if "require_payment" in payload:
                    STATE.require_payment = bool(payload["require_payment"])
                if "require_captcha" in payload:
                    STATE.require_captcha = bool(payload["require_captcha"])
                self._send_json({"ok": True})
            elif parsed.path == "/admin/coupons":
                STATE.coupons = list(payload.get("coupons", []))
                self._send_json({"ok": True, "coupons": STATE.coupons})
            elif parsed.path == "/admin/reset":
                STATE.reset()
                self._send_json({"ok": True})
            elif parsed.path == "/api/confirm":
                self._handle_confirm(payload)
            elif parsed.path == "/api/coupon/download":
                self._handle_coupon_download(payload)
            else:
                self._send_json({"error": "not found"}, status=404)

    def _handle_coupon_download(self, payload: dict) -> None:
        coupon_id = payload.get("coupon_id")
        coupon = next((c for c in STATE.coupons if c["id"] == coupon_id), None)
        if not coupon:
            self._send_json({"ok": False, "error": "쿠폰을 찾을 수 없습니다."})
            return
        if coupon.get("requires_gate"):
            self._send_json({"ok": False, "error": "이 쿠폰은 추가 확인이 필요합니다."})
            return
        if _coupon_expired(coupon):
            self._send_json({"ok": False, "error": "기간이 만료된 쿠폰입니다."})
            return
        if not coupon.get("downloadable_free", True):
            self._send_json({"ok": False, "error": "지금은 다운로드할 수 없는 쿠폰입니다."})
            return
        coupon["held"] = True
        self._send_json({"ok": True, "coupon": coupon})

    def _handle_confirm(self, payload: dict) -> None:
        time_ = payload.get("time")
        party_size = payload.get("party_size")
        date = payload.get("date")
        coupon_id = payload.get("coupon_id")

        if not STATE.is_open or not STATE.slots.get(time_, False):
            self._send_json({"ok": False, "error": "선택한 시간은 더 이상 예약할 수 없습니다."})
            return

        amount = (party_size or 0) * PRICE_PER_PERSON
        applied_coupon = None
        if coupon_id:
            coupon = next((c for c in STATE.coupons if c["id"] == coupon_id), None)
            # 서버 측 최종 방어선: 어댑터가 클릭 전 재검증을 놓쳤더라도 여기서
            # 한 번 더 막는다 - 게이트/만료/미보유 쿠폰은 절대 적용하지 않는다.
            if coupon and coupon.get("held") and _coupon_usable_now(coupon, amount):
                discount = _coupon_discount(coupon, amount)
                applied_coupon = {"id": coupon["id"], "name": coupon["name"], "discount": discount}

        STATE.slots[time_] = False
        STATE.reservation_seq += 1
        reservation_no = f"MOCK-{STATE.reservation_seq}"
        result = {
            "ok": True,
            "store_name": STORE_NAME,
            "date": date,
            "time": time_,
            "party_size": party_size,
            "reservation_no": reservation_no,
            "amount": amount,
            "applied_coupon": applied_coupon,
            "final_amount": amount - (applied_coupon["discount"] if applied_coupon else 0),
        }
        STATE.last_reservation = result
        self._send_json(result)


def run_server(host: str = "127.0.0.1", port: int = 8791) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((host, port), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True, name="mock-site")
    thread.start()
    return server


if __name__ == "__main__":
    srv = run_server()
    print("Mock booking site running at http://127.0.0.1:8791/booking?date=2026-10-03&party=2")
    print("Ctrl+C to stop")
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        srv.shutdown()
