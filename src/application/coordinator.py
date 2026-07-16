from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from src.config import AppConfig
from src.domain.clock import Clock
from src.domain.match_code import random_match_code
from src.domain.models import (
    MatchSession,
    MatchStatus,
    NotificationKind,
    ReactionAction,
    ReactionEvent,
    SubstituteRecruitment,
)
from src.infrastructure.persistence_writer import PersistenceWriter
from src.parsing.match_command import ParsedMatchCommand
from src.ports.repositories import MatchRepository

from .session_actor import SessionActor


logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class CreateMatchRequest:
    manager_user_id: int
    command_channel_id: int
    parsed: ParsedMatchCommand
    source_request_id: str | None = None
    source_request_type: str | None = None


class DuplicateSourceRequest(RuntimeError):
    def __init__(self, session: MatchSession) -> None:
        self.session = session
        super().__init__(f"source request already created match {session.id}")


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
        self._actors: dict[str, SessionActor] = {}
        self._created_sessions: dict[str, MatchSession] = {}
        self._announcement_match_ids: dict[int, str] = {}
        self._substitute_match_ids: dict[int, str] = {}
        self._tier_anchor_match_ids: dict[int, str] = {}
        self._confirmed_slots: dict[tuple[int, datetime], str] = {}
        self._lifecycle_lock = asyncio.Lock()
        self._confirmation_lock = asyncio.Lock()

    @property
    def active_sessions(self) -> tuple[MatchSession, ...]:
        sessions = [
            *(actor.session for actor in self._actors.values()),
            *self._created_sessions.values(),
        ]
        return tuple(
            sorted(
                sessions,
                key=lambda session: (
                    session.starts_at,
                    session.created_at,
                    session.id,
                ),
            )
        )

    @property
    def nearest_active_session(self) -> MatchSession | None:
        sessions = self.active_sessions
        return sessions[0] if sessions else None

    def actor_for_match(self, match_id: str) -> SessionActor | None:
        return self._actors.get(match_id)

    def lobby_assignment_for(self, starts_at: datetime) -> tuple[int, str]:
        starts_at_utc = starts_at.astimezone(timezone.utc)
        overlaps = any(
            abs(session.starts_at - starts_at_utc) < timedelta(hours=1)
            for session in self.active_sessions
        )
        defaults = self._config.defaults
        if overlaps and defaults.lobby_voice_channel_2_id is not None:
            return (
                defaults.lobby_voice_channel_2_id,
                defaults.lobby_2_name,
            )
        if defaults.lobby_voice_channel_id is None:
            raise RuntimeError("primary lobby voice channel is not configured")
        return defaults.lobby_voice_channel_id, defaults.lobby_name

    def actor_for_announcement(
        self,
        announcement_message_id: int,
    ) -> SessionActor | None:
        match_id = self._announcement_match_ids.get(announcement_message_id)
        if match_id is None:
            match_id = self._substitute_match_ids.get(announcement_message_id)
        return self._actors.get(match_id) if match_id is not None else None

    def is_substitute_recruitment_message(self, discord_message_id: int) -> bool:
        return discord_message_id in self._substitute_match_ids

    def substitute_message_ids_for_match(self, match_id: str) -> tuple[int, ...]:
        return tuple(
            message_id
            for message_id, routed_match_id in self._substitute_match_ids.items()
            if routed_match_id == match_id
        )

    def session_for_tier_anchor(
        self,
        tier_anchor_message_id: int,
    ) -> MatchSession | None:
        match_id = self._tier_anchor_match_ids.get(tier_anchor_message_id)
        actor = self._actors.get(match_id) if match_id is not None else None
        return actor.session if actor is not None else None

    async def restore(self) -> None:
        sessions = await self._repository.get_active_matches(self._config.guild_id)
        restored = 0
        for session in sessions:
            if session.starts_at <= self._clock.now():
                await self._repository.mark_started(session.id, self._clock.now())
                logger.info("stale active session marked started match_id=%s", session.id)
                continue
            if session.status is MatchStatus.CREATED or session.announcement_message_id is None:
                await self._repository.cancel_match(session.id, self._clock.now())
                logger.warning(
                    "incomplete created session canceled during recovery match_id=%s",
                    session.id,
                )
                continue
            roster = await self._repository.load_roster(session.id)
            actor = self._build_actor(session, roster)
            self._actors[session.id] = actor
            self._announcement_match_ids[session.announcement_message_id] = session.id
            if session.tier_anchor_message_id is not None:
                self._tier_anchor_match_ids[session.tier_anchor_message_id] = session.id
            for entry in actor.current_confirmed():
                key = (entry.discord_user_id, session.starts_at)
                existing = self._confirmed_slots.setdefault(key, session.id)
                if existing != session.id:
                    logger.warning(
                        "duplicate confirmed schedule found during recovery "
                        "user_id=%s starts_at=%s match_id=%s conflict_match_id=%s",
                        entry.discord_user_id,
                        session.starts_at.isoformat(),
                        session.id,
                        existing,
                    )
            actor.start()
            restored += 1
        substitute_recruitments = (
            await self._repository.get_active_substitute_recruitments(
                self._config.guild_id
            )
        )
        for recruitment in substitute_recruitments:
            actor = self._actors.get(recruitment.match_id)
            if actor is None:
                continue
            self._substitute_match_ids[
                recruitment.discord_message_id
            ] = recruitment.match_id
            actor.register_substitute_recruitment(
                recruitment.discord_message_id,
                recruitment.recruited_user_id,
            )
        logger.info(
            "active session recovery completed guild_id=%s session_count=%s",
            self._config.guild_id,
            restored,
        )

    async def create_match(self, request: CreateMatchRequest) -> MatchSession:
        async with self._lifecycle_lock:
            if request.source_request_id and request.source_request_type:
                existing = await self._repository.get_match_by_source(
                    request.source_request_type,
                    request.source_request_id,
                )
                if existing is not None:
                    raise DuplicateSourceRequest(existing)

            match_code = await self._new_match_code()
            now = self._clock.now()
            lobby_voice_channel_id, lobby_name = self.lobby_assignment_for(
                request.parsed.starts_at
            )
            session = MatchSession(
                id=str(uuid4()),
                match_code=match_code,
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
                lobby_voice_channel_id=lobby_voice_channel_id,
                lobby_name=lobby_name,
                source_request_id=request.source_request_id,
                source_request_type=request.source_request_type,
                created_at=now,
                updated_at=now,
            )
            await self._repository.create_match(session)
            self._created_sessions[session.id] = session
            logger.info(
                "match created match_id=%s match_code=%s manager_user_id=%s starts_at=%s",
                session.id,
                session.match_code,
                session.manager_user_id,
                session.starts_at.isoformat(),
            )
            return session

    async def _new_match_code(self) -> str:
        for _ in range(100):
            match_code = random_match_code()
            if await self._repository.get_match_by_code(match_code) is None:
                return match_code
        raise RuntimeError("failed to allocate a unique match code")

    async def activate(
        self,
        session: MatchSession,
        announcement_message_id: int,
    ) -> None:
        async with self._lifecycle_lock:
            created = self._created_sessions.get(session.id)
            if created is None:
                raise RuntimeError("match is not waiting for activation")
            now = self._clock.now()
            await self._repository.activate_recruiting(
                session.id,
                announcement_message_id,
                now,
            )
            session.announcement_message_id = announcement_message_id
            session.status = MatchStatus.RECRUITING
            session.updated_at = now
            self._created_sessions.pop(session.id, None)
            actor = self._build_actor(session, [])
            self._actors[session.id] = actor
            self._announcement_match_ids[announcement_message_id] = session.id
            actor.start()
            logger.info(
                "recruitment activated match_id=%s match_code=%s "
                "announcement_message_id=%s",
                session.id,
                session.match_code,
                announcement_message_id,
            )

    async def cancel_match(self, match_id: str) -> MatchSession | None:
        async with self._lifecycle_lock:
            session = self._session_by_id(match_id)
            if session is None:
                stored = await self._repository.get_match(match_id)
                if stored is None or not stored.is_automatic():
                    return None
                session = stored
            await self._repository.cancel_match(session.id, self._clock.now())
            actor = self._actors.pop(session.id, None)
            if actor is not None:
                await actor.stop(discard=True)
                await self._release_actor_confirmations(actor)
            self._created_sessions.pop(session.id, None)
            self._remove_routes(session)
            session.status = MatchStatus.CANCELED
            logger.info(
                "session canceled match_id=%s match_code=%s",
                session.id,
                session.match_code,
            )
            return session

    async def register_substitute_recruitment(
        self,
        *,
        match_id: str,
        discord_message_id: int,
    ) -> SubstituteRecruitment:
        async with self._lifecycle_lock:
            actor = self._actors.get(match_id)
            if actor is None or actor.session.full_reached_at is None:
                raise ValueError("모집이 완료된 활성 내전을 찾을 수 없습니다.")
            if actor.session.recruitment_completed_notified_at is None:
                raise ValueError(
                    "모집 완료 공지가 전송된 뒤 대타 모집을 열 수 있습니다."
                )
            await actor.drain()
            if actor.current_waitlist():
                raise ValueError(
                    "이미 대기자가 있습니다. 기존 대기열을 먼저 확인해 주세요."
                )
            existing = await self._repository.get_open_substitute_recruitment(
                match_id
            )
            if existing is not None:
                raise ValueError("이미 진행 중인 대타 모집이 있습니다.")
            recruitment = await self._repository.create_substitute_recruitment(
                match_id,
                discord_message_id,
                self._clock.now(),
            )
            self._substitute_match_ids[discord_message_id] = match_id
            actor.register_substitute_recruitment(discord_message_id)
            return recruitment

    async def cancel_substitute_recruitment(
        self,
        discord_message_id: int,
    ) -> None:
        async with self._lifecycle_lock:
            match_id = self._substitute_match_ids.pop(
                discord_message_id,
                None,
            )
            if match_id is not None:
                actor = self._actors.get(match_id)
                if actor is not None:
                    actor.unregister_substitute_recruitment(
                        discord_message_id
                    )
            await self._repository.cancel_substitute_recruitment(
                discord_message_id,
                self._clock.now(),
            )

    async def start_match(self, match_id: str) -> MatchSession | None:
        async with self._lifecycle_lock:
            session = self._session_by_id(match_id)
            if session is None:
                stored = await self._repository.get_match(match_id)
                if stored is None or not stored.is_automatic():
                    return None
                session = stored
            actor = self._actors.pop(session.id, None)
            if actor is not None:
                await actor.stop(discard=False)
            await self._writer.flush()
            now = self._clock.now()
            await self._repository.mark_started(session.id, now)
            self._created_sessions.pop(session.id, None)
            self._remove_routes(session)
            session.status = MatchStatus.STARTED
            session.updated_at = now
            logger.info(
                "session started and automatic processing stopped "
                "match_id=%s match_code=%s",
                session.id,
                session.match_code,
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
        actor = self.actor_for_announcement(announcement_message_id)
        if actor is None or received_at >= actor.session.starts_at:
            return False
        return actor.ingest(
            ReactionEvent(
                match_id=actor.session.id,
                discord_user_id=discord_user_id,
                discord_display_name=discord_display_name,
                action=action,
                received_at=received_at,
                substitute_recruitment_message_id=(
                    announcement_message_id
                    if self.is_substitute_recruitment_message(
                        announcement_message_id
                    )
                    else None
                ),
            )
        )

    def mark_notification_sent(
        self,
        match_id: str,
        kind: NotificationKind,
        sent_at: datetime,
        discord_message_id: int | None = None,
    ) -> None:
        actor = self._actors.get(match_id)
        if actor is None:
            return
        actor.mark_notification_sent(kind.value, sent_at)
        if kind is NotificationKind.TIER_ANCHOR and discord_message_id is not None:
            previous = actor.session.tier_anchor_message_id
            if previous is not None:
                self._tier_anchor_match_ids.pop(previous, None)
            actor.session.tier_anchor_message_id = discord_message_id
            self._tier_anchor_match_ids[discord_message_id] = match_id

    def clear_tier_anchor_route(self, tier_anchor_message_id: int) -> None:
        match_id = self._tier_anchor_match_ids.pop(tier_anchor_message_id, None)
        actor = self._actors.get(match_id) if match_id is not None else None
        if actor is not None and actor.session.tier_anchor_message_id == tier_anchor_message_id:
            actor.session.tier_anchor_message_id = None

    async def stop(self) -> None:
        async with self._lifecycle_lock:
            actors = list(self._actors.values())
            self._actors.clear()
            self._created_sessions.clear()
            self._announcement_match_ids.clear()
            self._substitute_match_ids.clear()
            self._tier_anchor_match_ids.clear()
            for actor in actors:
                await actor.stop(discard=False)
            self._confirmed_slots.clear()
            await self._writer.flush()

    def _build_actor(
        self,
        session: MatchSession,
        roster,
    ) -> SessionActor:
        return SessionActor(
            session,
            roster,
            self._writer,
            self._reserve_confirmation,
            self._release_confirmation,
        )

    def _session_by_id(self, match_id: str) -> MatchSession | None:
        actor = self._actors.get(match_id)
        if actor is not None:
            return actor.session
        return self._created_sessions.get(match_id)

    def _remove_routes(self, session: MatchSession) -> None:
        if session.announcement_message_id is not None:
            self._announcement_match_ids.pop(session.announcement_message_id, None)
        if session.tier_anchor_message_id is not None:
            self._tier_anchor_match_ids.pop(session.tier_anchor_message_id, None)
        for message_id in self.substitute_message_ids_for_match(session.id):
            self._substitute_match_ids.pop(message_id, None)

    async def _reserve_confirmation(
        self,
        session: MatchSession,
        discord_user_id: int,
    ) -> str | None:
        key = (discord_user_id, session.starts_at)
        async with self._confirmation_lock:
            conflict_match_id = self._confirmed_slots.get(key)
            if conflict_match_id is not None and conflict_match_id != session.id:
                return conflict_match_id
            self._confirmed_slots[key] = session.id
            return None

    async def _release_confirmation(
        self,
        session: MatchSession,
        discord_user_id: int,
    ) -> None:
        key = (discord_user_id, session.starts_at)
        async with self._confirmation_lock:
            if self._confirmed_slots.get(key) == session.id:
                self._confirmed_slots.pop(key, None)

    async def _release_actor_confirmations(self, actor: SessionActor) -> None:
        for entry in actor.current_confirmed():
            await self._release_confirmation(
                actor.session,
                entry.discord_user_id,
            )
