"""1회성 수동 로그인 스크립트.

Playwright persistent profile(로그인 세션/쿠키)에 네이버 로그인을 저장합니다.
비밀번호는 어디에도 저장하지 않습니다 - 브라우저가 직접 관리하는 세션 쿠키만
data/browser_profile 에 남습니다.

배경: 네이버 로그인 폼의 '로그인 상태 유지' 체크박스(#loginStay)가 기본 꺼져
있으면 세션 전용 쿠키만 발급되어 브라우저를 완전히 닫는 순간 사라집니다. 이
스크립트는 (1) 로그인 전에 그 체크박스를 미리 켜두고, (2) 로그인 감지 직후
(브라우저를 닫기 전) 아래 4가지를 [진단] 로그로 남기고, (3) 브라우저를 완전히
닫고 새로 열어 "재시작해도 실제로 유지되는지"까지 다시 검증합니다:
  - 현재 URL
  - 로그인된 사용자 페이지(MY_INFO_URL) 접근 성공 여부
  - 저장된 쿠키의 domain/name 목록 (값은 절대 출력하지 않음)
  - data/browser_profile (쿠키 DB 파일) 이 실제로 변경됐는지 (수정시각/크기 비교)

쿠키 이름이 한 번 보였다는 것만으로 성공을 판정하지 않습니다.

터미널 입력을 기다리지 않고, 로그인 완료를 쿠키로 자동 감지합니다 (최대 10분).
브라우저 창에서 직접 로그인(2단계 인증/캡차 포함)한 뒤 창을 그대로 두면 됩니다.

사용법:
    .venv\\Scripts\\python.exe scripts\\login_naver.py
"""
from __future__ import annotations

import asyncio
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from nrw.browser.naver_auth import (  # noqa: E402
    AUTH_COOKIE_NAMES,
    LOGIN_URL,
    MY_INFO_URL,
    check_login_status,
    ensure_stay_signed_in_checked,
)
from nrw.browser.persistent import persistent_context  # noqa: E402
from nrw.config import CONFIG  # noqa: E402

POLL_SEC = 3
TIMEOUT_SEC = 600  # 10분


def _cookie_db_path() -> Path:
    return CONFIG.browser_profile_dir / "Default" / "Network" / "Cookies"


def _cookie_db_stat() -> tuple[float, int] | None:
    """(수정시각, 바이트 크기) - 파일이 없으면 None. 프로필 변경 여부 비교용."""
    path = _cookie_db_path()
    if not path.exists():
        return None
    st = path.stat()
    return (st.st_mtime, st.st_size)


def _fmt_stat(stat: tuple[float, int] | None) -> str:
    if stat is None:
        return "파일 없음"
    mtime, size = stat
    return f"mtime={datetime.fromtimestamp(mtime).isoformat(timespec='seconds')} size={size}B"


async def wait_for_login(context) -> bool:
    """세션 쿠키(NID_SES/NID_AUT)가 생기는지 주기적으로 확인해 로그인 완료를 감지한다."""
    deadline = time.monotonic() + TIMEOUT_SEC
    while time.monotonic() < deadline:
        cookies = await context.cookies(["https://www.naver.com", "https://nid.naver.com"])
        names = {c["name"] for c in cookies}
        if names & AUTH_COOKIE_NAMES:
            return True
        await asyncio.sleep(POLL_SEC)
    return False


async def _log_pre_close_diagnostics(page, context, baseline_stat) -> None:
    """로그인 감지 직후, 브라우저를 닫기 전에 4가지 진단 정보를 로그로 남긴다."""
    print("\n[진단] ===== 로그인 감지됨 - 브라우저를 닫기 전 상태 =====")

    # 1) 현재 URL
    print(f"[진단] 현재 URL: {page.url}")

    # 2) 로그인된 사용자 페이지 접근 성공 여부
    try:
        await page.goto(MY_INFO_URL, wait_until="domcontentloaded", timeout=10000)
        final_url = page.url
        my_info_ok = "nidlogin.login" not in final_url
        print(f"[진단] 로그인 사용자 페이지({MY_INFO_URL}) 접근: "
              f"{'성공' if my_info_ok else '실패 - 로그인 화면으로 리다이렉트됨'} (최종 URL: {final_url})")
    except Exception as e:
        print(f"[진단] 로그인 사용자 페이지 접근 확인 중 오류: {e}")

    # 3) 저장된 쿠키의 domain/name 목록 (값은 절대 출력하지 않음)
    cookies = await context.cookies()
    print(f"[진단] 저장된 쿠키 수: {len(cookies)} (이름/도메인만 표시, 값은 출력하지 않음)")
    for c in sorted(cookies, key=lambda c: (c.get("domain", ""), c.get("name", ""))):
        marker = " <- 인증 쿠키" if c.get("name") in AUTH_COOKIE_NAMES else ""
        print(f"[진단]   domain={c.get('domain')!r:35s} name={c.get('name')!r}{marker}")

    # 4) data/browser_profile (쿠키 DB) 변경 여부 - 아직 close 전이라 OS 캐시에만
    #    있고 디스크에 flush 되지 않았을 수 있음을 감안해 참고용으로만 기록한다.
    current_stat = _cookie_db_stat()
    print(f"[진단] 쿠키 DB 파일 - 시작 전: {_fmt_stat(baseline_stat)}")
    print(f"[진단] 쿠키 DB 파일 - 닫기 전(지금): {_fmt_stat(current_stat)}")
    print("[진단] (참고: 브라우저가 아직 열려있어 OS 캐시에만 있고 디스크에 반영 안 됐을 수 있음 - "
          "완전히 닫은 뒤 최종 비교가 진짜 결과입니다)")
    print("[진단] ================================================\n")


async def main() -> None:
    baseline_stat = _cookie_db_stat()
    print(f"[진단] 시작 전 쿠키 DB 파일: {_fmt_stat(baseline_stat)}")

    print("브라우저 창을 엽니다. 그 창에서 네이버에 직접 로그인해주세요.")
    print("(2단계 인증/캡차가 나오면 창에서 직접 완료하시면 됩니다)")
    print(f"로그인이 완료되면 자동으로 감지합니다 (최대 {TIMEOUT_SEC // 60}분 대기).")

    logged_in = False
    async with persistent_context(headless=False) as context:
        page = await context.new_page()
        await page.goto(LOGIN_URL)
        await ensure_stay_signed_in_checked(page)
        print("'로그인 상태 유지' 체크박스를 미리 켜뒀습니다 (브라우저를 닫아도 로그인이 유지되도록).")

        logged_in = await wait_for_login(context)

        if logged_in:
            await _log_pre_close_diagnostics(page, context, baseline_stat)
        await page.close()

    # 컨텍스트가 완전히 닫힌 뒤 (Chromium 프로세스 종료 + 디스크 flush)
    after_close_stat = _cookie_db_stat()
    print(f"[진단] 브라우저를 완전히 닫은 후 쿠키 DB 파일: {_fmt_stat(after_close_stat)}")
    print(f"[진단] 프로필이 실제로 변경되었는가 (시작 전 대비): "
          f"{'예' if after_close_stat != baseline_stat else '아니오'}")

    if not logged_in:
        print(
            f"{TIMEOUT_SEC // 60}분 동안 로그인이 감지되지 않았습니다. "
            "이 스크립트를 다시 실행해 로그인을 완료해주세요."
        )
        return

    print("실제로 재시작 후에도 유지되는지 확인하기 위해 headless로 새로 엽니다...")

    async with persistent_context(headless=True) as context:
        ok, detail = await check_login_status(context)

    if ok:
        print(f"확인 완료: 브라우저를 다시 열어도 로그인 상태가 유지됩니다. ({detail})")
        print("이제 watcher 서비스가 이 세션을 재사용할 수 있습니다.")
    else:
        print(f"경고: 브라우저를 완전히 닫으면 로그인 상태가 사라집니다. ({detail})")
        print(
            "이 스크립트가 로그인 폼의 '로그인 상태 유지' 체크박스를 자동으로 켜두지만, "
            "네이버가 다른 로그인 화면(QR/간편로그인 등)을 보여준 경우 그 체크박스가 "
            "적용되지 않았을 수 있습니다. 위 [진단] 로그(특히 인증 쿠키 존재 여부와 "
            "프로필 변경 여부)를 함께 보내주시면 원인을 좁힐 수 있습니다."
        )


if __name__ == "__main__":
    asyncio.run(main())
