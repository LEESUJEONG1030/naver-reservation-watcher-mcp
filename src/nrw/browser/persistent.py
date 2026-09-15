"""Playwright persistent-profile browser context, shared across processes via
a file lock.

Only one Chromium process can hold a given ``user_data_dir`` at a time, so we
serialize access with ``filelock`` - both the watcher's own polling loop and
any ad-hoc job it services acquire this same lock before touching the
browser, and release it right after. This also means the user's one-time
manual Naver login (``scripts/login_naver.py``) must not run while the
watcher service is already running (same lock, so it will simply wait its
turn - see that script for details).
"""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

from filelock import FileLock
from playwright.async_api import BrowserContext, async_playwright

from nrw.config import CONFIG

log = logging.getLogger("nrw.browser.persistent")

_LOCK_TIMEOUT_SEC = 120


@asynccontextmanager
async def persistent_context(headless: bool | None = None):
    """Async context manager yielding a Playwright ``BrowserContext`` backed by
    the shared persistent profile. Handles the cross-process lock, launch and
    teardown."""
    CONFIG.browser_profile_dir.mkdir(parents=True, exist_ok=True)
    lock = FileLock(str(CONFIG.browser_lock_path), timeout=_LOCK_TIMEOUT_SEC)

    await asyncio.to_thread(lock.acquire)
    try:
        async with async_playwright() as pw:
            context: BrowserContext = await pw.chromium.launch_persistent_context(
                user_data_dir=str(CONFIG.browser_profile_dir),
                headless=CONFIG.browser.headless if headless is None else headless,
                viewport={"width": 1280, "height": 900},
                locale="ko-KR",
                timezone_id="Asia/Seoul",
                args=["--disable-blink-features=AutomationControlled"],
            )
            try:
                yield context
            finally:
                await context.close()
    finally:
        await asyncio.to_thread(lock.release)
