"""기능별 저장 계약. 구현은 하나의 연결과 트랜잭션 경계를 공유한다."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Protocol, Sequence

from src.domain.models import (
    MatchSession,
    NotificationRecord,
    PersistenceMutation,
    RosterEntry,
    SubstituteRecruitment,
    TierMessageBinding,
    TierParticipantStatus,
    TierSubmission,
    WebTierDTO,
)
from src.ports.safety import SafetyRepository


class RepositoryLifecycle(Protocol):
    """연결과 마이그레이션의 시작·종료. 조립된 runtime에서 호출한다."""

    async def initialize(self, migration_path: Path) -> None: ...

    async def close(self) -> None: ...


class MatchLookup(Protocol):
    """여러 기능이 공통으로 참조하는 내전 조회."""

    async def get_match(self, match_id: str) -> MatchSession | None: ...


class SessionRepository(MatchLookup, Protocol):
    """내전 생명주기·명단·대타 모집의 저장 계약."""

    async def create_match(self, session: MatchSession) -> None: ...

    async def activate_recruiting(
        self, match_id: str, announcement_message_id: int, now: datetime
    ) -> None: ...

    async def cancel_match(self, match_id: str, now: datetime) -> None: ...

    async def mark_started(self, match_id: str, now: datetime) -> None: ...

    async def get_match_by_code(self, match_code: str) -> MatchSession | None: ...

    async def get_match_by_source(
        self,
        source_request_type: str,
        source_request_id: str,
    ) -> MatchSession | None: ...

    async def get_active_matches(self, guild_id: int) -> list[MatchSession]: ...

    async def get_latest_match(self, guild_id: int) -> MatchSession | None: ...

    async def load_roster(self, match_id: str) -> list[RosterEntry]: ...

    async def create_substitute_recruitment(
        self,
        match_id: str,
        discord_message_id: int,
        now: datetime,
    ) -> SubstituteRecruitment: ...

    async def cancel_substitute_recruitment(
        self,
        discord_message_id: int,
        now: datetime,
    ) -> None: ...

    async def get_active_substitute_recruitments(
        self,
        guild_id: int,
    ) -> list[SubstituteRecruitment]: ...

    async def get_open_substitute_recruitment(
        self,
        match_id: str,
    ) -> SubstituteRecruitment | None: ...


class TierRepository(MatchLookup, Protocol):
    """티어 원문·답장 연결·수집 가능 조건의 저장 계약."""

    async def get_match_by_tier_anchor(
        self,
        tier_anchor_message_id: int,
    ) -> MatchSession | None: ...

    async def get_tier_candidates(
        self,
        guild_id: int,
        discord_user_id: int,
        activity_at: datetime,
    ) -> list[MatchSession]: ...

    async def is_tier_activity_allowed(
        self,
        match_id: str,
        discord_user_id: int,
        activity_at: datetime,
    ) -> bool: ...

    async def get_tier_binding(
        self,
        discord_message_id: int,
    ) -> TierMessageBinding | None: ...

    async def tier_collection_disabled(self, match_id: str, user_id: int) -> bool: ...

    async def upsert_tier_message(
        self,
        binding: TierMessageBinding,
        submission: TierSubmission,
    ) -> bool: ...

    async def delete_bound_tier_message(
        self,
        discord_message_id: int,
        received_at: datetime,
    ) -> TierMessageBinding | None: ...

    async def request_tier_anchor_recreation(
        self,
        tier_anchor_message_id: int,
        now: datetime,
    ) -> MatchSession | None: ...

    async def get_tier_submission(
        self, match_id: str, user_id: int
    ) -> TierSubmission | None: ...

    async def get_tier_status(self, match_id: str) -> list[TierParticipantStatus]: ...

    async def export_web_tiers(self, match_id: str) -> list[WebTierDTO]: ...


class NotificationRepository(MatchLookup, Protocol):
    """알림 예약·claim·전송 결과의 저장 계약. 과거 비활성 알림도 읽는다."""

    async def claim_missing_tier_reminder(
        self, match_id: str, now: datetime, cooldown_seconds: int
    ) -> tuple[bool, list[TierParticipantStatus]]: ...

    async def enqueue_lobby_notification(
        self, match_id: str, now: datetime
    ) -> None: ...

    async def enqueue_tier_missing_reminder_if_due(
        self, match_id: str, now: datetime
    ) -> None: ...

    async def enqueue_tier_complete_if_ready(
        self, match_id: str, now: datetime
    ) -> None: ...

    async def reset_sending_notifications(self) -> None: ...

    async def claim_notifications(
        self, now: datetime, limit: int = 10
    ) -> list[NotificationRecord]: ...

    async def mark_notification_sent(
        self,
        notification: NotificationRecord,
        discord_message_id: int | None,
        sent_at: datetime,
    ) -> None: ...

    async def mark_notification_failed(
        self,
        notification: NotificationRecord,
        error: str,
        now: datetime,
        max_attempts: int,
    ) -> None: ...


class MutationRepository(Protocol):
    """순서가 정해진 변경 배치를 원자적으로 저장하는 계약."""

    async def apply_mutations(
        self, mutations: Sequence[PersistenceMutation]
    ) -> None: ...


class MatchRepository(
    RepositoryLifecycle,
    SessionRepository,
    TierRepository,
    NotificationRepository,
    MutationRepository,
    SafetyRepository,
    Protocol,
):
    """조립과 호환용 통합 계약. 개별 서비스는 필요한 기능 계약만 사용한다."""
