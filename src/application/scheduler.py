from __future__ import annotations

import asyncio
import logging

from src.domain.clock import Clock
from src.infrastructure.persistence_writer import PersistenceWriter
from src.ports.repositories import MatchRepository

from .coordinator import SessionCoordinator


logger = logging.getLogger(__name__)


class MatchScheduler:
    def __init__(
        self,
        coordinator: SessionCoordinator,
        repository: MatchRepository,
        writer: PersistenceWriter,
        clock: Clock,
        *,
        interval_seconds: float = 10.0,
    ) -> None:
        self._coordinator = coordinator
        self._repository = repository
        self._writer = writer
        self._clock = clock
        self._interval = interval_seconds
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="match-scheduler")

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        self._task = None

    async def tick(self) -> None:
        sessions = self._coordinator.active_sessions
        if not sessions:
            return
        now = self._clock.now()
        await self._writer.flush()
        for session in sessions:
            try:
                if now >= session.starts_at:
                    await self._coordinator.start_match(session.id)
                    continue
                if now >= session.lobby_at and session.lobby_notified_at is None:
                    await self._repository.enqueue_lobby_notification(session.id, now)
                if session.recruitment_completed_notified_at is not None:
                    await self._repository.enqueue_tier_complete_if_ready(session.id, now)
                    if now >= session.tier_deadline_at:
                        await self._repository.enqueue_tier_missing_reminder_if_due(
                            session.id,
                            now,
                        )
            except Exception:
                logger.exception(
                    "scheduler session tick failed match_id=%s match_code=%s",
                    session.id,
                    session.match_code,
                )

    async def _run(self) -> None:
        while True:
            try:
                await self.tick()
            except Exception:
                logger.exception("scheduler tick failed")
            await asyncio.sleep(self._interval)
