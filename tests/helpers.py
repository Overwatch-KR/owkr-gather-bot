from __future__ import annotations

from datetime import datetime, timedelta, timezone

from owkr_gather_bot.config import (
    AppConfig,
    ChannelConfig,
    DefaultConfig,
    ManagerConfig,
    MessageConfig,
)
from owkr_gather_bot.domain.models import MatchSession, MatchStatus, PersistenceMutation


UTC = timezone.utc


class MutableClock:
    def __init__(self, value: datetime) -> None:
        self.value = value

    def now(self) -> datetime:
        return self.value


class RecordingWriter:
    def __init__(self) -> None:
        self.mutations: list[PersistenceMutation] = []

    def submit(self, mutation: PersistenceMutation) -> None:
        self.mutations.append(mutation)

    async def flush(self) -> None:
        return None


def make_config() -> AppConfig:
    return AppConfig(
        guild_id=100,
        channels=ChannelConfig(command=101, announcement=102, tier=103, admin=104),
        admin_user_ids=frozenset({200}),
        admin_role_ids=frozenset(),
        defaults=DefaultConfig(),
        messages=MessageConfig(
            participation_notice="✅ 반응",
            tier_notice="티어 작성",
            manner_notice="매너 게임",
        ),
        managers={200: ManagerConfig()},
    )


def make_session(
    *,
    match_id: str = "match-1",
    now: datetime | None = None,
    status: MatchStatus = MatchStatus.RECRUITING,
    completion_notified_at: datetime | None = None,
    full_reached_at: datetime | None = None,
) -> MatchSession:
    now = now or datetime(2026, 7, 16, 8, 0, tzinfo=UTC)
    starts_at = now + timedelta(hours=3)
    return MatchSession(
        id=match_id,
        guild_id=100,
        manager_user_id=200,
        command_channel_id=101,
        announcement_channel_id=102,
        announcement_message_id=1000,
        tier_channel_id=103,
        admin_channel_id=104,
        mode=None,
        participant_limit=10,
        status=status,
        starts_at=starts_at,
        tier_deadline_at=starts_at - timedelta(minutes=30),
        lobby_at=starts_at - timedelta(minutes=10),
        full_reached_at=full_reached_at,
        recruitment_completed_notified_at=completion_notified_at,
        created_at=now,
        updated_at=now,
    )

