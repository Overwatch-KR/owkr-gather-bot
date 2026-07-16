from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any


class MatchStatus(StrEnum):
    CREATED = "CREATED"
    RECRUITING = "RECRUITING"
    FULL = "FULL"
    STARTED = "STARTED"
    CANCELED = "CANCELED"


class RosterStatus(StrEnum):
    CONFIRMED = "CONFIRMED"
    WAITLISTED = "WAITLISTED"
    WITHDRAWN = "WITHDRAWN"


class WaitlistReason(StrEnum):
    CAPACITY = "CAPACITY"
    SCHEDULE_CONFLICT = "SCHEDULE_CONFLICT"


class ReactionAction(StrEnum):
    ADD = "ADD"
    REMOVE = "REMOVE"


class NotificationKind(StrEnum):
    RECRUITMENT_COMPLETE = "RECRUITMENT_COMPLETE"
    TIER_ANCHOR = "TIER_ANCHOR"
    TIER_ANCHOR_RECREATED = "TIER_ANCHOR_RECREATED"
    TIER_MISSING_REMINDER = "TIER_MISSING_REMINDER"
    LOBBY_REMINDER = "LOBBY_REMINDER"
    TIER_COMPLETE = "TIER_COMPLETE"


class NotificationStatus(StrEnum):
    PENDING = "PENDING"
    SENDING = "SENDING"
    SENT = "SENT"
    FAILED = "FAILED"
    CANCELED = "CANCELED"


@dataclass(slots=True)
class MatchSession:
    id: str
    match_code: str
    guild_id: int
    manager_user_id: int
    command_channel_id: int
    announcement_channel_id: int
    tier_channel_id: int
    admin_channel_id: int
    mode: str | None
    participant_limit: int
    status: MatchStatus
    starts_at: datetime
    tier_deadline_at: datetime
    lobby_at: datetime
    created_at: datetime
    updated_at: datetime
    source_request_id: str | None = None
    source_request_type: str | None = None
    announcement_message_id: int | None = None
    tier_anchor_message_id: int | None = None
    full_reached_at: datetime | None = None
    recruitment_completed_notified_at: datetime | None = None
    tier_complete_notified_at: datetime | None = None
    tier_missing_reminder_notified_at: datetime | None = None
    lobby_notified_at: datetime | None = None
    last_missing_tier_reminder_at: datetime | None = None
    next_arrival_seq: int = 1

    def is_automatic(self) -> bool:
        return self.status in {
            MatchStatus.CREATED,
            MatchStatus.RECRUITING,
            MatchStatus.FULL,
        }

    def accepts_activity_at(self, activity_at: datetime) -> bool:
        return self.is_automatic() and activity_at < self.starts_at

    def accepts_tier_activity_at(self, activity_at: datetime) -> bool:
        opened_at = self.recruitment_completed_notified_at
        return (
            opened_at is not None
            and self.accepts_activity_at(activity_at)
            and opened_at <= activity_at < self.tier_deadline_at
        )


@dataclass(slots=True)
class RosterEntry:
    match_id: str
    discord_user_id: int
    discord_display_name: str
    reaction_order: int
    status: RosterStatus
    reacted_at: datetime
    removed_at: datetime | None = None
    waitlist_reason: WaitlistReason | None = None
    conflict_match_id: str | None = None

    @property
    def is_active(self) -> bool:
        return self.status in {RosterStatus.CONFIRMED, RosterStatus.WAITLISTED}


@dataclass(frozen=True, slots=True)
class ReactionEvent:
    match_id: str
    discord_user_id: int
    discord_display_name: str
    action: ReactionAction
    received_at: datetime


@dataclass(frozen=True, slots=True)
class ReactionMutation:
    match_id: str
    discord_user_id: int
    action: ReactionAction
    received_at: datetime
    arrival_seq: int
    outcome: str
    next_arrival_seq: int
    match_status: MatchStatus
    roster_entry: RosterEntry | None = None
    full_reached_at: datetime | None = None
    completion_user_ids: tuple[int, ...] = ()


@dataclass(frozen=True, slots=True)
class TierUpsertMutation:
    match_id: str
    discord_user_id: int
    discord_display_name: str
    raw_tier_message: str
    discord_message_id: int
    activity_at: datetime
    collected_at: datetime


@dataclass(frozen=True, slots=True)
class TierDeleteMutation:
    match_id: str
    discord_message_id: int


PersistenceMutation = ReactionMutation | TierUpsertMutation | TierDeleteMutation


@dataclass(slots=True)
class TierSubmission:
    match_id: str
    discord_user_id: int
    discord_display_name: str
    raw_tier_message: str
    discord_message_id: int
    activity_at: datetime
    collected_at: datetime


@dataclass(frozen=True, slots=True)
class TierMessageBinding:
    discord_message_id: int
    match_id: str
    discord_user_id: int
    bound_at: datetime


@dataclass(frozen=True, slots=True)
class TierParticipantStatus:
    discord_user_id: int
    discord_display_name: str
    reaction_order: int
    has_tier: bool


@dataclass(frozen=True, slots=True)
class WebTierDTO:
    discord_user_id: str
    discord_display_name: str
    raw_tier_message: str
    reaction_order: int

    def to_dict(self) -> dict[str, str | int]:
        return {
            "discordUserId": self.discord_user_id,
            "discordDisplayName": self.discord_display_name,
            "rawTierMessage": self.raw_tier_message,
            "reactionOrder": self.reaction_order,
        }


@dataclass(slots=True)
class NotificationRecord:
    id: int
    match_id: str
    kind: NotificationKind
    channel_id: int
    payload: dict[str, Any]
    dedupe_key: str
    status: NotificationStatus
    attempts: int
    next_attempt_at: datetime
