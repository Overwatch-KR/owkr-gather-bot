"""알림 전송 경계의 표시 데이터와 계약. 멘션 제한은 전송 구현에서도 적용한다."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class RenderedNotification:
    content: str
    allowed_user_ids: tuple[int, ...] = ()
    voice_channel_id: int | None = None
    content_before_mentions: str | None = None
    content_after_mentions: str | None = None

    def with_allowed_user_ids(
        self,
        user_ids: tuple[int, ...],
    ) -> RenderedNotification:
        if self.content_before_mentions is None or self.content_after_mentions is None:
            raise ValueError(
                "notification content cannot be rebuilt with filtered mentions"
            )
        return RenderedNotification(
            content=(
                self.content_before_mentions
                + _mention_lines(user_ids)
                + self.content_after_mentions
            ),
            allowed_user_ids=user_ids,
            voice_channel_id=self.voice_channel_id,
            content_before_mentions=self.content_before_mentions,
            content_after_mentions=self.content_after_mentions,
        )


def _mention_lines(user_ids: tuple[int, ...]) -> str:
    mentions = [f"<@{user_id}>" for user_id in user_ids]
    return "\n".join(
        " ".join(mentions[index : index + 5]) for index in range(0, len(mentions), 5)
    )


class NotificationTransport(Protocol):
    async def send(self, channel_id: int, rendered: RenderedNotification) -> int | None:
        """메시지 ID를 반환한다. 전원 입장 등으로 전송을 생략하면 None을 반환한다."""
        ...
