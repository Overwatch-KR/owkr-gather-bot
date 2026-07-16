from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime
from pathlib import Path
from typing import Sequence

from owkr_gather_bot.domain.models import (
    MatchSession,
    NotificationRecord,
    PersistenceMutation,
    RosterEntry,
    TierParticipantStatus,
    TierSubmission,
    WebTierDTO,
)


class MatchRepository(ABC):
    @abstractmethod
    async def initialize(self, migration_path: Path) -> None: ...

    @abstractmethod
    async def close(self) -> None: ...

    @abstractmethod
    async def create_replacing_active(self, session: MatchSession) -> list[str]: ...

    @abstractmethod
    async def activate_recruiting(self, match_id: str, announcement_message_id: int, now: datetime) -> None: ...

    @abstractmethod
    async def cancel_match(self, match_id: str, now: datetime) -> None: ...

    @abstractmethod
    async def mark_started(self, match_id: str, now: datetime) -> None: ...

    @abstractmethod
    async def get_match(self, match_id: str) -> MatchSession | None: ...

    @abstractmethod
    async def get_active_match(self, guild_id: int) -> MatchSession | None: ...

    @abstractmethod
    async def get_latest_match(self, guild_id: int) -> MatchSession | None: ...

    @abstractmethod
    async def load_roster(self, match_id: str) -> list[RosterEntry]: ...

    @abstractmethod
    async def apply_mutations(self, mutations: Sequence[PersistenceMutation]) -> None: ...

    @abstractmethod
    async def get_tier_submission(self, match_id: str, user_id: int) -> TierSubmission | None: ...

    @abstractmethod
    async def get_tier_status(self, match_id: str) -> list[TierParticipantStatus]: ...

    @abstractmethod
    async def claim_missing_tier_reminder(
        self, match_id: str, now: datetime, cooldown_seconds: int
    ) -> tuple[bool, list[TierParticipantStatus]]: ...

    @abstractmethod
    async def enqueue_lobby_notification(self, match_id: str, now: datetime) -> None: ...

    @abstractmethod
    async def enqueue_tier_complete_if_ready(self, match_id: str, now: datetime) -> None: ...

    @abstractmethod
    async def reset_sending_notifications(self) -> None: ...

    @abstractmethod
    async def claim_notifications(self, now: datetime, limit: int = 10) -> list[NotificationRecord]: ...

    @abstractmethod
    async def mark_notification_sent(
        self, notification: NotificationRecord, discord_message_id: int, sent_at: datetime
    ) -> None: ...

    @abstractmethod
    async def mark_notification_failed(
        self, notification: NotificationRecord, error: str, now: datetime, max_attempts: int
    ) -> None: ...

    @abstractmethod
    async def export_web_tiers(self, match_id: str) -> list[WebTierDTO]: ...
