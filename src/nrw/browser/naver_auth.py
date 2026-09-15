"""Naver login/session helpers.

Root cause of an earlier apparent contradiction (login_naver.py reported
success mid-session, but a later fresh headless check found no session): the
Naver login form has a "로그인 상태 유지" (stay signed in) checkbox
(``#loginStay``) that is unchecked by default. Without it, Naver issues only
session-scoped auth cookies, which Chromium correctly discards once the
browser process fully closes - so the cookie really was there mid-session and
really was gone after a full restart. Both observations were correct; the
session just wasn't configured to persist.

Fix here is two-fold:
1. ``ensure_stay_signed_in_checked`` toggles that checkbox before the user
   logs in (a plain UI checkbox, not a credential field - never touches
   username/password).
2. ``check_login_status`` verifies persistence for real: cookie name
   presence alone is not proof, so callers should close the context and open
   a fresh one on the same profile before calling this, to test what will
   actually be there next time the watcher/MCP server starts.
"""
from __future__ import annotations

from playwright.async_api import BrowserContext

LOGIN_URL = "https://nid.naver.com/nidlogin.login"
STAY_SIGNED_IN_CHECKBOX = "#loginStay"
AUTH_COOKIE_NAMES = {"NID_SES", "NID_AUT"}
# 로그인 안 된 상태로 접근하면 nidlogin.login으로 리다이렉트되는 것을 확인함
# (2026-09-14 직접 확인) - "로그인된 사용자 페이지 접근 성공 여부" 진단용.
MY_INFO_URL = "https://nid.naver.com/user2/help/myInfo.nhn"
# naver.com 헤더의 실제 로그인 버튼 - 로그아웃 상태일 때만 화면에 보인다.
# ("text=로그아웃" 매칭은 실제로 시도해봤더니 화면에 보이지 않는(hidden) 무관한
#  안내문구("로그아웃 시 인기 검색 종목이 제공됩니다")에 우연히 걸리는 false positive가
#  있었다 - 그래서 visible 여부까지 반드시 함께 확인한다.)
LOGIN_CTA_SELECTOR = 'a[href^="https://nid.naver.com/nidlogin.login"]'


async def ensure_stay_signed_in_checked(page) -> None:
    """로그인 페이지의 '로그인 상태 유지' 체크박스를 미리 켜둔다.
    UI 토글일 뿐이며 아이디/비밀번호 입력란은 절대 건드리지 않는다."""
    try:
        checkbox = page.locator(STAY_SIGNED_IN_CHECKBOX)
        if await checkbox.count() == 0:
            return
        if not await checkbox.is_checked():
            await checkbox.check()
    except Exception:
        pass  # 로그인 페이지 구조가 다르면 그냥 넘어간다 - 사용자가 화면에서 직접 체크할 수 있음


async def check_login_status(context: BrowserContext) -> tuple[bool, str]:
    """저장된 profile로 실제 로그인 상태가 유지되는지 확인한다.

    쿠키 이름 존재만으로 판단하지 않고, naver.com에 실제로 접속해 화면상
    로그인 버튼(로그인 CTA)이 "보이는지" 까지 함께 확인한다 - 이 CTA는
    로그아웃 상태일 때만 화면에 나타나므로, 이게 보이면 쿠키가 있어도
    로그인되지 않은 것으로 판단한다. 호출자는 이 함수를 부르기 전에
    컨텍스트를 한 번 완전히 닫고 새로 열어야 "재시작 후에도 유지되는가"를
    제대로 검증할 수 있다 (같은 세션 안에서의 쿠키 확인은 브라우저를 껐다
    켰을 때의 결과를 보장하지 않는다)."""
    cookies = await context.cookies(["https://www.naver.com", "https://nid.naver.com"])
    cookie_names = {c["name"] for c in cookies}
    matched = cookie_names & AUTH_COOKIE_NAMES
    has_auth_cookie = bool(matched)

    page = await context.new_page()
    try:
        await page.goto("https://www.naver.com", wait_until="domcontentloaded")
        await page.wait_for_timeout(1000)
        login_cta_visible = False
        try:
            login_cta = page.locator(LOGIN_CTA_SELECTOR)
            for i in range(await login_cta.count()):
                if await login_cta.nth(i).is_visible():
                    login_cta_visible = True
                    break
        except Exception:
            login_cta_visible = False
    finally:
        await page.close()

    if login_cta_visible:
        reason = "화면에 로그인 버튼이 보입니다 (로그아웃 상태)"
        if has_auth_cookie:
            reason += f" - 인증 쿠키({sorted(matched)})는 있지만 실제로는 로그인되어 있지 않습니다"
        return False, reason

    if has_auth_cookie:
        return True, f"인증 쿠키({sorted(matched)}) 확인 + 화면에 로그인 버튼이 보이지 않음"

    return False, "인증 쿠키(NID_SES/NID_AUT)가 없고 로그인 버튼 표시 여부도 불확실합니다"
