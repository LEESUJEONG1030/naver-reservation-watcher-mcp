"""All Naver-Booking-specific text markers / selectors live here, in one file,
so they're easy to find and adjust when Naver changes their UI.

The date-chip / calendar-modal selectors below were captured on 2026-09-14 by
inspecting a real Naver Place ("장소예약") booking widget
(pcmap.place.naver.com/restaurant/{id}/booking). This is the reservation
widget embedded in most restaurant/cafe Naver Place pages - reached from a
map.naver.com place page or a naver.me short link via its "/booking"
sub-path. It is NOT the standalone booking.naver.com widget; some businesses
may still use that older widget, or a custom one, so keep the class-substring
matchers below loose (``[class*="..."]``) and treat the whole file as
best-effort - if a specific business's DOM differs, adjust the values here.

CSS module class names carry a build-specific hash suffix (e.g.
``_timeChipButton_1qlq4_410``) that can change on redeploy. We match only the
stable human-readable prefix via attribute-contains selectors
(``[class*="_timeChipButton_"]``) so a hash rotation alone doesn't break
these.
"""
from __future__ import annotations

import re

BOOKING_HOST_MARKERS = ("booking.naver.com", "pcmap.place.naver.com", "map.naver.com")

# 시간 슬롯처럼 보이는 텍스트 (예: "19:00", "9:30") - 구형/커스텀 위젯 폴백용
TIME_TEXT_RE = re.compile(r"^\s*\d{1,2}:\d{2}\s*$")

# --- 날짜 칩 목록 (예약 페이지 최상단, 약 30일치를 가로로 나열) ---------------
# <button class="_timeChipButton_..."><div class="_timeChip_..."><span
#   class="_timeChipLabel_... _timeChipLabelAvailable_...">9. 14. (오늘)</span>
#   <span class="_blind_...">예약가능</span></div></button>
# 마감된 날짜는 button이 아니라 <div aria-disabled="true"> 로 렌더링되고
# label 클래스도 ..LabelClosed.. 로 바뀐다.
DATE_CHIP_LABEL = '[class*="_timeChipLabel_"]'
DATE_CHIP_LABEL_AVAILABLE_MARKER = "LabelAvailable"
DATE_CHIP_LABEL_CLOSED_MARKER = "LabelClosed"
DATE_CHIP_CLICKABLE_ANCESTOR = 'xpath=ancestor::button[contains(@class,"_timeChipButton_")]'

# --- 인원 선택 버튼 (모달 안, "N명" 텍스트를 가진 pill 버튼) -------------------
PARTY_SIZE_BUTTON_TEXT = "{n}명"
PARTY_SIZE_SELECTED_MARKER = "_selected_"

# --- 월 달력 (모달 안, 날짜 칩 목록에 없는 먼 미래 날짜를 고를 때 사용) --------
CALENDAR_MONTH_LABEL = '[class*="_monthLabel_"]'
CALENDAR_NAV_BUTTON = 'button[class*="_navButton_"]'  # 순서상 0=이전달, 1=다음달
CALENDAR_DAY_SPAN = '[class*="_day_"]'
CALENDAR_DISABLED_ANCESTOR = 'xpath=ancestor::div[@aria-disabled][1]'
CALENDAR_CELL_CLOSED_MARKER = "_closed_"

# --- 시간대 버튼 (모달 안, "오전"/"오후" 그룹 + "5:00" 형식 버튼) ------------
TIME_PERIOD_GROUP = '[class*="_timePeriodGroup_"]'
TIME_PERIOD_LABEL = '[class*="_timePeriodLabel_"]'  # "오전" | "오후"
TIME_PERIOD_BUTTON = '[class*="_timePeriodGrid_"] button'
TIME_BUTTON_LABEL = '[class*="_label_"]'

# 검색 모달을 여는 진입점 (날짜 칩 목록에 없는 먼 미래 날짜를 고를 때)
SEARCH_ENTRY_TEXT = "인원, 날짜, 시간으로 검색"

# --- 시간 선택 이후 "다음"을 눌러야 진입하는 요약/제출 단계 -----------------------
# 2026-09-15 실제 업체 A 페이지에서 직접 확인: 시간대 버튼을 클릭하는
# 것만으로는 위젯이 요약 화면으로 넘어가지 않고, 별도의 "다음" 버튼을 한 번 더
# 눌러야 한다. 이 버튼을 누르지 않으면 클릭 직전 재검증 단계에서 날짜/시간/인원
# 요약을 전혀 읽을 수 없다 (엉뚱한 페이지 텍스트를 대신 주워온다).
NEXT_STEP_BUTTON_TEXT = "다음"

# "다음" 이후 나타나는 요약 패널과 최종 제출 버튼 - 이 클래스 접두어들은 업체별
# 커스터마이징이 아니라 네이버 예약 위젯 자체의 React 컴포넌트 이름이라, 업체가
# 달라도 동일하게 유지될 가능성이 높다 (해시 접미사만 바뀜).
#   예: <div class="BookingSummaryView__root__qk8ZD"><ul class="...__list__...">
#         일정\n9. 18. (금) 오후 5:00\n인원\n2명</ul></div>
#       <button class="SubmitButtonView__btn_cta__xYPjJ ...">예약 신청하기</button>
# 주의: 페이지 상단 헤더에도 "예약하기"라는 별도의 내비게이션 버튼이 role="button"
# 으로 존재해서(실측으로 확인), CONFIRM_BUTTON_TEXTS 텍스트만으로 첫 번째 버튼을
# 찾으면 이 헤더 버튼을 오인 클릭할 위험이 있다 - 그래서 이 위젯 전용 셀렉터를
# 항상 먼저 시도하고, 텍스트 기반 스캔은 이 셀렉터가 없는(구형/커스텀) 위젯에만
# 폴백으로 사용한다.
BOOKING_SUMMARY_SELECTOR = '[class*="BookingSummaryView"]'

# 실측(2026-09-15, 실제 업체 A) 결과 "이전"(뒤로가기) 버튼도 같은 스텝-네비게이션
# 컴포넌트라서 "SubmitButtonView" 클래스 접두어를 함께 쓴다 - 클래스만으로는 실제
# 제출 버튼과 구분이 안 된다. data-click-code 속성("submitbutton.submit")이
# 훨씬 더 구체적이라 최우선으로 쓰고, 그 다음 "_btn_cta_" 클래스 조합(제출류
# 버튼의 variant 이름으로 보임)으로 폴백한다 - 텍스트 기반 스캔(CONFIRM_BUTTON_TEXTS)
# 은 이 둘 다 없는 위젯에만 최후 폴백으로 사용한다.
SUBMIT_BUTTON_SELECTORS = (
    'button[data-click-code="submitbutton.submit"]',
    'button[class*="SubmitButtonView"][class*="btn_cta"]',
)
# _advance_past_next_button 의 "뭔가는 렌더링됐는지" 대기용 - 여기서는 정밀하게
# 구분할 필요가 없어 두 후보를 합쳐서 쓴다.
SUBMIT_BUTTON_SELECTOR = ", ".join(SUBMIT_BUTTON_SELECTORS)
SUBMIT_BUTTON_DISABLED_MARKER = "is_disabled"

# 아직 예약이 열리지 않음 (오픈 예정)
BEFORE_OPEN_MARKERS = (
    "부터 예약", "오픈 예정", "예약 시작", "예약 오픈", "아직 예약을 받고 있지 않",
)
# "O월 O일 O시부터 예약 가능" 같은 문구에서 시각 추출 시도용 (best-effort)
OPEN_AT_TEXT_RE = re.compile(r"(\d{1,2})월\s*(\d{1,2})일.{0,10}?(\d{1,2})시")

# --- AUTO_ACCEPT_REQUIRED_TERMS: 예약 완료에 필수인 이용약관/개인정보 동의만
# 자동으로 체크하는 정책의 판별 문구 -----------------------------------------
# 2026-09-15 실측(실제 업체 A): 예약 화면(RequestV2 위젯)의 동의 섹션은 체크박스가
# 단 하나("아래 내용에 모두 동의합니다")뿐이고, 그 라벨 안에 "*필수" 표시가 있다.
# "필수"/"선택" 텍스트가 둘 다 있거나 둘 다 없으면(새로운/다른 형태의 동의 항목)
# 추측하지 않고 NEEDS_HUMAN 으로 멈춘다.
REQUIRED_CONSENT_TEXT_MARKERS = ("필수",)
OPTIONAL_CONSENT_TEXT_MARKERS = ("선택",)
# 마케팅/알림/광고성/멤버십 관련 동의는 "필수"로 표시돼 있어도 절대 자동 체크하지
# 않는다 (사용자가 사전 승인한 범위를 명확히 벗어난다) - 이런 항목이 필수로
# 표시돼 있으면 그 자체가 비정상이므로 NEEDS_HUMAN 으로 멈춘다.
MARKETING_CONSENT_MARKERS = (
    "마케팅", "광고", "수신 동의", "수신동의", "알림받기", "알림 받기", "프로모션",
    "이벤트 정보", "혜택 정보", "sms", "문자 수신", "이메일 수신", "뉴스레터", "멤버십",
    "구독",
)

# 마감/만석 (전체 페이지 텍스트 기반 폴백 감지용)
SOLD_OUT_MARKERS = ("마감", "잔여 좌석 없음", "예약 가능한 시간이 없", "만석")

# 슬롯 비활성 여부를 판단할 class/attr 키워드 (구형/커스텀 위젯 폴백용)
DISABLED_CLASS_MARKERS = ("disabled", "is-disabled", "full", "soldout", "sold-out")

# 결제/선결제/보증금/취소수수료/추가 동의 - 발견되면 절대 자동 진행하지 않음
PAYMENT_MARKERS = ("결제하기", "선결제", "예약금", "보증금", "카드 정보", "결제 수단")
CANCEL_FEE_MARKERS = ("취소 수수료", "환불 규정", "노쇼", "위약금")
EXTRA_CONSENT_MARKERS = ("동의합니다", "약관에 동의", "개인정보 수집", "필수 동의")

# CAPTCHA / 추가 인증 / 비밀번호 재입력
CAPTCHA_MARKERS = ("자동입력 방지", "보안문자", "캡차", "captcha", "로봇이 아닙니다")
REAUTH_MARKERS = (
    "비밀번호를 다시 입력", "본인 확인", "인증번호를 입력", "2단계 인증",
    "휴대폰 번호로 인증", "네이버 아이디로 로그인",
)

# 최종 예약 확정 버튼 후보 텍스트 (role=button 검색 시 사용) - reserve()에서만 사용
CONFIRM_BUTTON_TEXTS = ("예약하기", "예약 신청", "신청하기", "확인", "예약 확정", "다음")

# 성공 판정 텍스트
SUCCESS_MARKERS = ("예약이 완료", "예약 완료", "예약이 확정", "예약 신청이 완료", "신청이 완료")
RESERVATION_NO_RE = re.compile(r"예약\s*번호\s*[:\-]?\s*([A-Za-z0-9\-]+)")

# --- 쿠폰 (네이버 플레이스 홈/예약 페이지에 "쿠폰" 섹션으로 노출) ------------------
# 2026-09-14 실제 업체 B 페이지에서 직접 확인: 쿠폰 목록의 각 항목은
# <li> 안에 두 개의 <a data-nlog-area="plc_btp.coupon"> 를 가진다 - 하나는 설명
# 링크, 다른 하나가 실제 "받기/다운로드" 버튼이며 이쪽에만 aria-disabled 속성이
# 있다 (이미 받은 쿠폰이거나 받을 수 없으면 "true"). data-nlog-area 는 네이버
# 자체 분석 태그라 CSS 클래스 해시보다 훨씬 안정적이라 이걸 기준으로 찾는다.
COUPON_NLOG_AREA = "plc_btp.coupon"
COUPON_DOWNLOAD_LINK_SELECTOR = f'a[data-nlog-area="{COUPON_NLOG_AREA}"][aria-disabled]'
COUPON_LIST_ITEM_XPATH = "xpath=ancestor::li[1]"

# 쿠폰 설명에 이런 문구가 있으면 "받기" 버튼이 안 비활성화돼 있어도 자동으로
# 받거나 적용하면 안 된다 (별도 확인 필요) - 유료 멤버십/구독/개인정보/마케팅동의/결제.
COUPON_GATE_MARKERS = (
    "멤버십", "구독",  "가입 시", "가입 후", "알림받기한", "알림받고", "알림 신청",
    "개인정보", "동의 시", "동의 후", "선결제", "예약금", "카드 등록",
)
# 할인 금액 파싱 시도용 (모든 쿠폰이 이 형태는 아님 - 실패하면 discount_value=0 으로
# 두고 raw_text 로 사람이 직접 확인할 수 있게 한다)
COUPON_AMOUNT_RE = re.compile(r"([\d,]+)\s*원\s*(?:할인|쿠폰)")
COUPON_PERCENT_RE = re.compile(r"(\d+)\s*%\s*할인")

# --- 예약 화면(RequestV2 위젯) 자체에 내장된 쿠폰 선택 UI --------------------------
# 2026-09-15 실측(실제 업체 A): /home 페이지의 쿠폰 목록과는 완전히 별개의 소스로,
# "다음" 버튼을 눌러 도착하는 요약/제출 화면에 "쿠폰 선택" 버튼이 있고 클릭하면
# 모달로 이 예약에 실제로 적용 가능한 쿠폰 목록이 뜬다. 별도의 "받기"(다운로드)
# 단계 없이 체크박스로 바로 선택하고 "적용"을 누르면 폼에 반영된다(아직 최종
# 제출 전이라 되돌릴 수 있음). /home 스캔이 이 쿠폰의 게이트 여부를 다르게
# (불일치하게) 보고한 적이 있어, 실제 이 예약에 적용 가능한지는 이 모달이 더
# 신뢰도 높은 소스다.
COUPON_SELECT_BUTTON_SELECTOR = 'button[class*="CouponSelect__btn_show_coupon"]'
COUPON_MODAL_ITEM_SELECTOR = 'li[class*="CouponListModal__coupon_item"]'
COUPON_MODAL_ITEM_TITLE_SELECTOR = '[class*="Coupon__txt_title"]'
COUPON_MODAL_ITEM_DATE_SELECTOR = '[class*="Coupon__txt_date"]'
COUPON_MODAL_ITEM_CHECKBOX_SELECTOR = '[class*="Coupon__state_link"][role="checkbox"]'
COUPON_MODAL_CLOSE_BUTTON_SELECTOR = "button.btn_close"
COUPON_MODAL_CANCEL_BUTTON_TEXT = "취소"
COUPON_MODAL_APPLY_BUTTON_TEXT = "적용"
COUPON_MODAL_EXPIRY_RE = re.compile(r"(\d{4})\.\s*(\d{1,2})\.\s*(\d{1,2})\.")
