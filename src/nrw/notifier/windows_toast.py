"""Windows Toast notifier backed by win11toast (WinRT wrapper).

- Plain notifications (NOTIFY mode, info/error logs) use the synchronous
  ``win11toast.notify`` - it just shows the toast and returns immediately.
- Notifications with action buttons (ASK mode "예약할까요?") need to receive a
  click while our process is still alive, so we run ``win11toast.toast_async``
  in a dedicated background thread with its own asyncio loop. The click
  handler writes an APPROVE/DECLINE job straight into the shared SQLite DB,
  which the watcher's job-queue poller then picks up - no direct coupling to
  the watcher's own event loop needed.
"""
from __future__ import annotations

import asyncio
import logging
import threading

from nrw.notifier.base import Notifier, NotifyMessage

log = logging.getLogger("nrw.notifier.windows_toast")

APP_ID = "NaverReservationWatcher"


class WindowsToastNotifier(Notifier):
    def notify(self, message: NotifyMessage) -> None:
        if message.actions:
            self._notify_interactive(message)
        else:
            self._notify_plain(message)

    # -- plain -------------------------------------------------------------
    def _notify_plain(self, message: NotifyMessage) -> None:
        try:
            from win11toast import notify as toast_notify

            toast_notify(title=message.title, body=message.body, app_id=APP_ID)
        except Exception:
            log.exception("Failed to show plain toast notification")

    # -- interactive (with approve/decline buttons) -------------------------
    def _notify_interactive(self, message: NotifyMessage) -> None:
        def run():
            try:
                asyncio.run(self._toast_async(message))
            except Exception:
                log.exception("Failed to show interactive toast notification")

        threading.Thread(target=run, daemon=True, name="nrw-toast").start()

    async def _toast_async(self, message: NotifyMessage) -> None:
        from win11toast import toast_async

        buttons = [
            {
                "activationType": "background",
                "arguments": f"{action.action_id}:{message.watch_id}",
                "content": action.label,
            }
            for action in message.actions
        ]

        def on_click(result):
            self._handle_click(message, result)
            return result

        await toast_async(
            title=message.title,
            body=message.body,
            buttons=buttons,
            on_click=on_click,
            app_id=APP_ID,
        )

    def _handle_click(self, message: NotifyMessage, result) -> None:
        try:
            arguments = ""
            if isinstance(result, dict):
                arguments = result.get("arguments") or ""
            if not arguments:
                log.info("Toast for watch %s dismissed without a button click", message.watch_id)
                return

            action_id, _, watch_id_str = arguments.partition(":")
            watch_id = int(watch_id_str) if watch_id_str.isdigit() else message.watch_id

            from nrw import db
            from nrw.models import JobType

            job_type = JobType.APPROVE if action_id == "approve" else JobType.DECLINE
            db.create_job(type_=job_type, watch_id=watch_id, payload=None)
            db.add_event(
                watch_id=watch_id,
                level="INFO",
                message=f"토스트 알림에서 사용자가 '{action_id}' 선택",
            )
        except Exception:
            log.exception("Failed to handle toast click for watch %s", message.watch_id)
