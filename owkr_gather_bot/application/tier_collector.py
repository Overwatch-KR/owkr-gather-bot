from __future__ import annotations

from datetime import datetime

from owkr_gather_bot.domain.clock import Clock
from owkr_gather_bot.domain.models import TierDeleteMutation, TierUpsertMutation
from owkr_gather_bot.infrastructure.persistence_writer import PersistenceWriter

from .coordinator import SessionCoordinator


class TierCollector:
    def __init__(
        self,
        coordinator: SessionCoordinator,
        writer: PersistenceWriter,
        clock: Clock,
    ) -> None:
        self._coordinator = coordinator
        self._writer = writer
        self._clock = clock

    def submit_activity(
        self,
        *,
        guild_id: int,
        channel_id: int,
        discord_user_id: int,
        discord_display_name: str,
        raw_content: str,
        discord_message_id: int,
        activity_at: datetime,
    ) -> bool:
        actor = self._coordinator.active_actor
        if actor is None:
            return False
        session = actor.session
        if (
            guild_id != session.guild_id
            or channel_id != session.tier_channel_id
            or not raw_content.strip()
            or not actor.has_active_user(discord_user_id)
            or not session.accepts_tier_activity_at(activity_at)
        ):
            return False
        self._writer.submit(
            TierUpsertMutation(
                match_id=session.id,
                discord_user_id=discord_user_id,
                discord_display_name=discord_display_name,
                raw_tier_message=raw_content,
                discord_message_id=discord_message_id,
                activity_at=activity_at,
                collected_at=self._clock.now(),
            )
        )
        return True

    def submit_delete(
        self,
        *,
        guild_id: int,
        channel_id: int,
        discord_message_id: int,
        received_at: datetime,
    ) -> bool:
        actor = self._coordinator.active_actor
        if actor is None:
            return False
        session = actor.session
        if (
            guild_id != session.guild_id
            or channel_id != session.tier_channel_id
            or received_at >= session.starts_at
        ):
            return False
        self._writer.submit(
            TierDeleteMutation(match_id=session.id, discord_message_id=discord_message_id)
        )
        return True

