"""Notifier interface. Any new channel (Telegram, Slack, ...) implements this
same interface so watcher core code never depends on a specific channel."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field


@dataclass
class NotifyAction:
    """알림에 붙는 액션 버튼 (지원되는 채널에서만 사용됨)."""

    label: str
    action_id: str  # 예: "approve", "decline"


@dataclass
class NotifyMessage:
    title: str
    body: str
    actions: list[NotifyAction] = field(default_factory=list)
    watch_id: int | None = None


class Notifier(ABC):
    @abstractmethod
    def notify(self, message: NotifyMessage) -> None:
        """알림을 보낸다. 액션 버튼 클릭은 채널 구현체가 처리(콜백 등)하며,
        watcher core는 결과를 DB(jobs 테이블에 APPROVE/DECLINE job 생성)로 받는다."""
        raise NotImplementedError
