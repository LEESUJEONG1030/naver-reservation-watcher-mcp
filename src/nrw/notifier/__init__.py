from nrw.config import CONFIG
from nrw.notifier.base import Notifier
from nrw.notifier.windows_toast import WindowsToastNotifier


def get_notifier() -> Notifier:
    channel = CONFIG.notify.channel
    if channel == "windows_toast":
        return WindowsToastNotifier()
    if channel == "telegram":
        from nrw.notifier.telegram_stub import TelegramNotifier
        return TelegramNotifier()
    raise ValueError(f"Unknown notify channel: {channel}")
