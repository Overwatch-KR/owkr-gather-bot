from __future__ import annotations

import asyncio
import logging
from typing import Protocol

from owkr_gather_bot.domain.clock import Clock
from owkr_gather_bot.domain.models import NotificationRecord
from owkr_gather_bot.ports.repositories import MatchRepository

from .coordinator import SessionCoordinator
from .rendering import NotificationRenderer, RenderedNotification


logger = logging.getLogger(__name__)


class NotificationTransport(Protocol):
    async def send(self, channel_id: int, rendered: RenderedNotification) -> int: ...


class NotificationWorker:
    def __init__(
        self,
        repository: MatchRepository,
        coordinator: SessionCoordinator,
        renderer: NotificationRenderer,
        transport: NotificationTransport,
        clock: Clock,
        *,
        poll_interval_seconds: float = 1.0,
        max_attempts: int = 5,
    ) -> None:
        self._repository = repository
        self._coordinator = coordinator
        self._renderer = renderer
        self._transport = transport
        self._clock = clock
        self._poll_interval = poll_interval_seconds
        self._max_attempts = max_attempts
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        await self._repository.reset_sending_notifications()
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="notification-outbox-worker")

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        self._task = None

    async def process_once(self) -> int:
        notifications = await self._repository.claim_notifications(self._clock.now())
        for notification in notifications:
            await self._deliver(notification)
        return len(notifications)

    async def _deliver(self, notification: NotificationRecord) -> None:
        try:
            session = await self._repository.get_match(notification.match_id)
            if session is None or not session.is_automatic():
                raise RuntimeError("match is no longer active")
            if self._clock.now() >= session.starts_at:
                await self._coordinator.start_current()
                return
            rendered = self._renderer.render(notification, session)
            message_id = await self._transport.send(notification.channel_id, rendered)
            sent_at = self._clock.now()
            await self._repository.mark_notification_sent(notification, message_id, sent_at)
            self._coordinator.mark_notification_sent(
                notification.match_id, notification.kind, sent_at
            )
            logger.info(
                "notification sent match_id=%s kind=%s discord_message_id=%s",
                notification.match_id,
                notification.kind.value,
                message_id,
            )
        except Exception as exc:
            logger.warning(
                "notification delivery failed match_id=%s kind=%s attempt=%s will_retry=%s",
                notification.match_id,
                notification.kind.value,
                notification.attempts,
                notification.attempts < self._max_attempts,
                exc_info=True,
            )
            await self._repository.mark_notification_failed(
                notification, str(exc), self._clock.now(), self._max_attempts
            )

    async def _run(self) -> None:
        while True:
            await self.process_once()
            await asyncio.sleep(self._poll_interval)
