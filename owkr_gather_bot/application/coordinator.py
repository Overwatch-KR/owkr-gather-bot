from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import uuid4

from owkr_gather_bot.config import AppConfig
from owkr_gather_bot.domain.clock import Clock
from owkr_gather_bot.domain.models import (
    MatchSession,
    MatchStatus,
    NotificationKind,
    ReactionAction,
    ReactionEvent,
)
from owkr_gather_bot.infrastructure.persistence_writer import PersistenceWriter
from owkr_gather_bot.parsing.match_command import ParsedMatchCommand
from owkr_gather_bot.ports.repositories import MatchRepository

from .session_actor import SessionActor


logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class CreateMatchRequest:
    manager_user_id: int
    command_channel_id: int
    parsed: ParsedMatchCommand


class SessionCoordinator:
    def __init__(
        self,
        config: AppConfig,
        repository: MatchRepository,
        writer: PersistenceWriter,
        clock: Clock,
    ) -> None:
        self._config = config
        self._repository = repository
        self._writer = writer
        self._clock = clock
        self._actor: SessionActor | None = None
        self._created_session: MatchSession | None = None
        self._lifecycle_lock = asyncio.Lock()

    @property
    def active_actor(self) -> SessionActor | None:
        return self._actor

    @property
    def active_session(self) -> MatchSession | None:
        if self._actor is not None:
            return self._actor.session
        return self._created_session

    async def restore(self) -> None:
        session = await self._repository.get_active_match(self._config.guild_id)
        if session is None:
            logger.info(
                "active session recovery completed result=none guild_id=%s",
                self._config.guild_id,
            )
            return
        if session.starts_at <= self._clock.now():
            await self._repository.mark_started(session.id, self._clock.now())
            logger.info("stale active session marked started match_id=%s", session.id)
            return
        if session.status is MatchStatus.CREATED or session.announcement_message_id is None:
            await self._repository.cancel_match(session.id, self._clock.now())
            logger.warning(
                "incomplete created session canceled during recovery match_id=%s",
                session.id,
            )
            return
        roster = await self._repository.load_roster(session.id)
        self._actor = SessionActor(session, roster, self._writer)
        self._actor.start()
        logger.info(
            "active session recovered match_id=%s status=%s roster_count=%s next_arrival_seq=%s",
            session.id,
            session.status.value,
            len(roster),
            session.next_arrival_seq,
        )

    async def create_replacing_active(self, request: CreateMatchRequest) -> MatchSession:
        async with self._lifecycle_lock:
            now = self._clock.now()
            session = MatchSession(
                id=str(uuid4()),
                guild_id=self._config.guild_id,
                manager_user_id=request.manager_user_id,
                command_channel_id=request.command_channel_id,
                announcement_channel_id=self._config.channels.announcement,
                tier_channel_id=self._config.channels.tier,
                admin_channel_id=self._config.channels.admin,
                mode=request.parsed.mode,
                participant_limit=self._config.defaults.participant_limit,
                status=MatchStatus.CREATED,
                starts_at=request.parsed.starts_at.astimezone(timezone.utc),
                tier_deadline_at=request.parsed.tier_deadline_at.astimezone(timezone.utc),
                lobby_at=request.parsed.lobby_at.astimezone(timezone.utc),
                created_at=now,
                updated_at=now,
            )
            previous_ids = await self._repository.create_replacing_active(session)
            if self._actor is not None:
                await self._actor.stop(discard=True)
                self._actor = None
            self._created_session = session
            logger.info(
                "match created match_id=%s manager_user_id=%s starts_at=%s replaced_match_ids=%s",
                session.id,
                session.manager_user_id,
                session.starts_at.isoformat(),
                previous_ids,
            )
            return session

    async def activate(self, session: MatchSession, announcement_message_id: int) -> None:
        async with self._lifecycle_lock:
            if self._created_session is None or self._created_session.id != session.id:
                raise RuntimeError("match was replaced before activation")
            now = self._clock.now()
            await self._repository.activate_recruiting(session.id, announcement_message_id, now)
            session.announcement_message_id = announcement_message_id
            session.status = MatchStatus.RECRUITING
            session.updated_at = now
            self._created_session = None
            self._actor = SessionActor(session, [], self._writer)
            self._actor.start()
            logger.info(
                "recruitment activated match_id=%s announcement_message_id=%s",
                session.id,
                announcement_message_id,
            )

    async def cancel_current(self, *, expected_match_id: str | None = None) -> MatchSession | None:
        async with self._lifecycle_lock:
            session = self.active_session
            if session is None or (
                expected_match_id is not None and session.id != expected_match_id
            ):
                return None
            await self._repository.cancel_match(session.id, self._clock.now())
            if self._actor is not None:
                await self._actor.stop(discard=True)
            session.status = MatchStatus.CANCELED
            self._actor = None
            self._created_session = None
            logger.info("session canceled match_id=%s", session.id)
            return session

    async def start_current(self) -> MatchSession | None:
        async with self._lifecycle_lock:
            session = self.active_session
            if session is None:
                return None
            if self._actor is not None:
                await self._actor.stop(discard=False)
            await self._writer.flush()
            now = self._clock.now()
            await self._repository.mark_started(session.id, now)
            session.status = MatchStatus.STARTED
            session.updated_at = now
            self._actor = None
            self._created_session = None
            logger.info(
                "session started and automatic processing stopped match_id=%s",
                session.id,
            )
            return session

    def ingest_reaction(
        self,
        *,
        announcement_message_id: int,
        discord_user_id: int,
        discord_display_name: str,
        action: ReactionAction,
        received_at: datetime,
    ) -> bool:
        actor = self._actor
        if (
            actor is None
            or actor.session.announcement_message_id != announcement_message_id
            or received_at >= actor.session.starts_at
        ):
            return False
        return actor.ingest(
            ReactionEvent(
                match_id=actor.session.id,
                discord_user_id=discord_user_id,
                discord_display_name=discord_display_name,
                action=action,
                received_at=received_at,
            )
        )

    def mark_notification_sent(
        self, match_id: str, kind: NotificationKind, sent_at: datetime
    ) -> None:
        if self._actor is not None and self._actor.session.id == match_id:
            self._actor.mark_notification_sent(kind.value, sent_at)

    async def stop(self) -> None:
        async with self._lifecycle_lock:
            if self._actor is not None:
                await self._actor.stop(discard=False)
                self._actor = None
            await self._writer.flush()
