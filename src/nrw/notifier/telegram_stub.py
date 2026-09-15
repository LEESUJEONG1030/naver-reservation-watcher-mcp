"""Placeholder for a future Telegram notifier.

Implement the same ``Notifier`` interface (see base.py) - accept a bot token
and chat id (from env vars, never hardcoded), send ``message.body`` via the
Bot API, and if ``message.actions`` is non-empty, send an inline keyboard and
run a small long-polling/webhook loop that writes APPROVE/DECLINE jobs into
the DB the same way windows_toast.py does. Not implemented yet - selecting
``channel = "telegram"`` in config.toml will raise NotImplementedError until
this is filled in.
"""
from __future__ import annotations

from nrw.notifier.base import Notifier, NotifyMessage


class TelegramNotifier(Notifier):
    def __init__(self) -> None:
        raise NotImplementedError(
            "Telegram notifier is not implemented yet. "
            "Implement TelegramNotifier using the Notifier interface in nrw/notifier/base.py."
        )

    def notify(self, message: NotifyMessage) -> None:  # pragma: no cover
        raise NotImplementedError
