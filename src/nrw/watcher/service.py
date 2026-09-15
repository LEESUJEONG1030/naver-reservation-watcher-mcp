"""Watcher service entrypoint - the process that must keep running (independent
of Claude Desktop / the MCP server) so monitoring continues in the background.
Register it with Windows Task Scheduler via scripts/register_task_scheduler.ps1.

Usage:
    python -m nrw.watcher.service
Environment:
    NRW_ADAPTER=naver|mock   (default: naver)
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys

from nrw import db
from nrw.browser.persistent import persistent_context
from nrw.config import CONFIG
from nrw.notifier import get_notifier
from nrw.watcher.core import run_due_watches
from nrw.watcher.jobs import poll_and_handle_jobs

log = logging.getLogger("nrw.watcher.service")


def _configure_logging() -> None:
    CONFIG.log_dir.mkdir(parents=True, exist_ok=True)
    handlers = [
        logging.FileHandler(CONFIG.log_dir / "watcher.log", encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ]
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=handlers,
    )


def _make_adapter():
    kind = os.environ.get("NRW_ADAPTER", "naver").lower()
    if kind == "mock":
        from nrw.browser.mock_adapter import MockBookingAdapter
        return MockBookingAdapter()
    from nrw.browser.naver_adapter import NaverBookingAdapter
    return NaverBookingAdapter()


async def run_forever() -> None:
    db.init_db()
    adapter = _make_adapter()
    notifier = get_notifier()
    log.info("watcher service starting (adapter=%s, pid=%s)", type(adapter).__name__, os.getpid())

    while True:
        db.update_heartbeat(os.getpid())
        try:
            handled_jobs = await poll_and_handle_jobs(CONFIG, persistent_context, adapter, notifier)
            if handled_jobs:
                log.info("handled %d job(s)", handled_jobs)
        except Exception:
            log.exception("error while polling jobs")

        try:
            import datetime as _dt
            due_now = db.due_watches(_dt.datetime.now(_dt.UTC).isoformat())
            if due_now:
                async with persistent_context() as context:
                    checked = await run_due_watches(CONFIG, context, adapter, notifier)
                log.info("checked %d due watch(es)", checked)
        except Exception:
            log.exception("error while checking due watches")

        await asyncio.sleep(CONFIG.polling.job_poll_interval_sec)


def main() -> None:
    _configure_logging()
    try:
        asyncio.run(run_forever())
    except KeyboardInterrupt:
        log.info("watcher service stopped (KeyboardInterrupt)")


if __name__ == "__main__":
    main()
