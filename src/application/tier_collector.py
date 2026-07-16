from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from src.domain.clock import Clock
from src.domain.models import MatchSession, TierMessageBinding, TierSubmission
from src.ports.repositories import MatchRepository


class TierRouteStatus(StrEnum):
    ACCEPTED = "ACCEPTED"
    IGNORED = "IGNORED"
    AMBIGUOUS = "AMBIGUOUS"
    NOT_PARTICIPANT = "NOT_PARTICIPANT"
    CLOSED = "CLOSED"
    INVALID_FORMAT = "INVALID_FORMAT"
    BOUND_TO_OTHER_USER = "BOUND_TO_OTHER_USER"


@dataclass(frozen=True, slots=True)
class TierRouteResult:
    status: TierRouteStatus
    session: MatchSession | None = None
    candidates: tuple[MatchSession, ...] = ()

    @property
    def accepted(self) -> bool:
        return self.status is TierRouteStatus.ACCEPTED


class TierCollector:
    def __init__(
        self,
        repository: MatchRepository,
        clock: Clock,
    ) -> None:
        self._repository = repository
        self._clock = clock

    async def submit_activity(
        self,
        *,
        guild_id: int,
        channel_id: int,
        discord_user_id: int,
        discord_display_name: str,
        raw_content: str,
        discord_message_id: int,
        activity_at: datetime,
        reply_to_message_id: int | None = None,
    ) -> TierRouteResult:
        binding = await self._repository.get_tier_binding(discord_message_id)
        if not raw_content.strip():
            if binding is None:
                return TierRouteResult(TierRouteStatus.IGNORED)
            if binding.discord_user_id != discord_user_id:
                return TierRouteResult(TierRouteStatus.BOUND_TO_OTHER_USER)
            deleted = await self._repository.delete_bound_tier_message(
                discord_message_id,
                self._clock.now(),
            )
            return TierRouteResult(
                TierRouteStatus.ACCEPTED if deleted is not None else TierRouteStatus.CLOSED,
                await self._repository.get_match(binding.match_id),
            )

        if binding is not None:
            session = await self._repository.get_match(binding.match_id)
            if session is None:
                return TierRouteResult(TierRouteStatus.IGNORED)
            if binding.discord_user_id != discord_user_id:
                return TierRouteResult(
                    TierRouteStatus.BOUND_TO_OTHER_USER,
                    session,
                )
            if not await self._repository.is_tier_activity_allowed(
                session.id,
                discord_user_id,
                activity_at,
            ):
                return TierRouteResult(TierRouteStatus.CLOSED, session)
            accepted = await self._repository.upsert_tier_message(
                binding,
                self._submission(
                    session,
                    discord_user_id,
                    discord_display_name,
                    raw_content,
                    discord_message_id,
                    activity_at,
                ),
            )
            return TierRouteResult(
                TierRouteStatus.ACCEPTED if accepted else TierRouteStatus.IGNORED,
                session,
            )

        if reply_to_message_id is not None:
            session = await self._repository.get_match_by_tier_anchor(
                reply_to_message_id
            )
            if session is None:
                return TierRouteResult(TierRouteStatus.IGNORED)
            if (
                guild_id != session.guild_id
                or channel_id != session.tier_channel_id
            ):
                return TierRouteResult(TierRouteStatus.IGNORED)
            if not await self._repository.is_tier_activity_allowed(
                session.id,
                discord_user_id,
                activity_at,
            ):
                return TierRouteResult(TierRouteStatus.CLOSED, session)
            roster = await self._repository.load_roster(session.id)
            if not any(
                entry.discord_user_id == discord_user_id and entry.is_active
                for entry in roster
            ):
                return TierRouteResult(TierRouteStatus.NOT_PARTICIPANT, session)
            return await self._bind_and_store(
                session,
                discord_user_id,
                discord_display_name,
                raw_content,
                discord_message_id,
                activity_at,
            )

        if not self._looks_like_tier_message(raw_content):
            return TierRouteResult(TierRouteStatus.INVALID_FORMAT)
        candidates = await self._repository.get_tier_candidates(
            guild_id,
            discord_user_id,
            activity_at,
        )
        if not candidates:
            return TierRouteResult(TierRouteStatus.IGNORED)
        if len(candidates) > 1:
            return TierRouteResult(
                TierRouteStatus.AMBIGUOUS,
                candidates=tuple(candidates),
            )
        return await self._bind_and_store(
            candidates[0],
            discord_user_id,
            discord_display_name,
            raw_content,
            discord_message_id,
            activity_at,
        )

    async def submit_delete(
        self,
        *,
        guild_id: int,
        channel_id: int,
        discord_message_id: int,
        received_at: datetime,
    ) -> TierRouteResult:
        binding = await self._repository.get_tier_binding(discord_message_id)
        if binding is None:
            return TierRouteResult(TierRouteStatus.IGNORED)
        session = await self._repository.get_match(binding.match_id)
        if (
            session is None
            or guild_id != session.guild_id
            or channel_id != session.tier_channel_id
        ):
            return TierRouteResult(TierRouteStatus.IGNORED)
        deleted = await self._repository.delete_bound_tier_message(
            discord_message_id,
            received_at,
        )
        return TierRouteResult(
            TierRouteStatus.ACCEPTED if deleted is not None else TierRouteStatus.CLOSED,
            session,
        )

    async def _bind_and_store(
        self,
        session: MatchSession,
        discord_user_id: int,
        discord_display_name: str,
        raw_content: str,
        discord_message_id: int,
        activity_at: datetime,
    ) -> TierRouteResult:
        binding = TierMessageBinding(
            discord_message_id=discord_message_id,
            match_id=session.id,
            discord_user_id=discord_user_id,
            bound_at=self._clock.now(),
        )
        accepted = await self._repository.upsert_tier_message(
            binding,
            self._submission(
                session,
                discord_user_id,
                discord_display_name,
                raw_content,
                discord_message_id,
                activity_at,
            ),
        )
        return TierRouteResult(
            TierRouteStatus.ACCEPTED if accepted else TierRouteStatus.IGNORED,
            session,
        )

    def _submission(
        self,
        session: MatchSession,
        discord_user_id: int,
        discord_display_name: str,
        raw_content: str,
        discord_message_id: int,
        activity_at: datetime,
    ) -> TierSubmission:
        return TierSubmission(
            match_id=session.id,
            discord_user_id=discord_user_id,
            discord_display_name=discord_display_name,
            raw_tier_message=raw_content,
            discord_message_id=discord_message_id,
            activity_at=activity_at,
            collected_at=self._clock.now(),
        )

    @staticmethod
    def _looks_like_tier_message(content: str) -> bool:
        stripped = content.strip()
        return bool(stripped) and ("/" in stripped or "#" in stripped)
