"""내전 생명주기·참가 명단·대타 모집 SQL."""

from __future__ import annotations

import logging
from datetime import (
    datetime,
)

from src.domain.models import (
    MatchSession,
    MatchStatus,
    NotificationStatus,
    RosterEntry,
    SubstituteRecruitment,
    SubstituteRecruitmentStatus,
)

from .base import (
    ACTIVE_STATUSES,
    SQLiteStore,
    _to_db,
)

logger = logging.getLogger(__name__)


class SessionStorage(SQLiteStore):
    """내전 생명주기·참가 명단·대타 모집 SQL."""

    async def create_match(self, session: MatchSession) -> None:
        async with self._write_lock:
            await self.connection.execute("BEGIN IMMEDIATE")
            try:
                await self.connection.execute(
                    """
                    INSERT INTO match_sessions (
                        id, match_code, source_request_id, source_request_type,
                        guild_id, manager_user_id, command_channel_id,
                        announcement_channel_id, announcement_message_id,
                        tier_channel_id, tier_anchor_message_id,
                        admin_channel_id, mode, participant_limit,
                        status, starts_at_utc, tier_deadline_at_utc, lobby_at_utc,
                        lobby_voice_channel_id, lobby_name,
                        full_reached_at_utc, recruitment_completed_notified_at_utc,
                        tier_complete_notified_at_utc,
                        tier_missing_reminder_notified_at_utc, lobby_notified_at_utc,
                        last_missing_tier_reminder_at_utc, next_arrival_seq,
                        created_at_utc, updated_at_utc
                    ) VALUES (
                        ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                        ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                    )
                    """,
                    (
                        session.id,
                        session.match_code,
                        session.source_request_id,
                        session.source_request_type,
                        session.guild_id,
                        session.manager_user_id,
                        session.command_channel_id,
                        session.announcement_channel_id,
                        session.announcement_message_id,
                        session.tier_channel_id,
                        session.tier_anchor_message_id,
                        session.admin_channel_id,
                        session.mode,
                        session.participant_limit,
                        session.status.value,
                        _to_db(session.starts_at),
                        _to_db(session.tier_deadline_at),
                        _to_db(session.lobby_at),
                        session.lobby_voice_channel_id,
                        session.lobby_name,
                        _to_db(session.full_reached_at)
                        if session.full_reached_at
                        else None,
                        _to_db(session.recruitment_completed_notified_at)
                        if session.recruitment_completed_notified_at
                        else None,
                        _to_db(session.tier_complete_notified_at)
                        if session.tier_complete_notified_at
                        else None,
                        _to_db(session.tier_missing_reminder_notified_at)
                        if session.tier_missing_reminder_notified_at
                        else None,
                        _to_db(session.lobby_notified_at)
                        if session.lobby_notified_at
                        else None,
                        _to_db(session.last_missing_tier_reminder_at)
                        if session.last_missing_tier_reminder_at
                        else None,
                        session.next_arrival_seq,
                        _to_db(session.created_at),
                        _to_db(session.updated_at),
                    ),
                )
                await self.connection.commit()
            except Exception:
                await self.connection.rollback()
                raise

    async def activate_recruiting(
        self, match_id: str, announcement_message_id: int, now: datetime
    ) -> None:
        await self._execute_write(
            """
            UPDATE match_sessions
            SET announcement_message_id = ?, status = ?, updated_at_utc = ?
            WHERE id = ? AND status = ?
            """,
            (
                announcement_message_id,
                MatchStatus.RECRUITING.value,
                _to_db(now),
                match_id,
                MatchStatus.CREATED.value,
            ),
        )

    async def cancel_match(self, match_id: str, now: datetime) -> None:
        async with self._write_lock:
            await self.connection.execute("BEGIN IMMEDIATE")
            try:
                await self.connection.execute(
                    """
                    UPDATE match_sessions SET status = ?, updated_at_utc = ?
                    WHERE id = ? AND status IN (?, ?, ?)
                    """,
                    (
                        MatchStatus.CANCELED.value,
                        _to_db(now),
                        match_id,
                        *ACTIVE_STATUSES,
                    ),
                )
                await self.connection.execute(
                    """
                    UPDATE notification_outbox SET status = ?
                    WHERE match_id = ? AND status IN (?, ?)
                    """,
                    (
                        NotificationStatus.CANCELED.value,
                        match_id,
                        NotificationStatus.PENDING.value,
                        NotificationStatus.SENDING.value,
                    ),
                )
                await self.connection.execute(
                    """
                    UPDATE substitute_recruitments
                    SET status = ?
                    WHERE match_id = ? AND status = ?
                    """,
                    (
                        SubstituteRecruitmentStatus.CANCELED.value,
                        match_id,
                        SubstituteRecruitmentStatus.OPEN.value,
                    ),
                )
                await self.connection.commit()
            except Exception:
                await self.connection.rollback()
                raise

    async def mark_started(self, match_id: str, now: datetime) -> None:
        async with self._write_lock:
            await self.connection.execute("BEGIN IMMEDIATE")
            try:
                await self.connection.execute(
                    """
                    UPDATE match_sessions SET status = ?, updated_at_utc = ?
                    WHERE id = ? AND status IN (?, ?, ?)
                    """,
                    (
                        MatchStatus.STARTED.value,
                        _to_db(now),
                        match_id,
                        *ACTIVE_STATUSES,
                    ),
                )
                await self.connection.execute(
                    """
                    UPDATE notification_outbox SET status = ?
                    WHERE match_id = ? AND status IN (?, ?)
                    """,
                    (
                        NotificationStatus.CANCELED.value,
                        match_id,
                        NotificationStatus.PENDING.value,
                        NotificationStatus.SENDING.value,
                    ),
                )
                await self.connection.execute(
                    """
                    UPDATE substitute_recruitments
                    SET status = ?
                    WHERE match_id = ? AND status = ?
                    """,
                    (
                        SubstituteRecruitmentStatus.CANCELED.value,
                        match_id,
                        SubstituteRecruitmentStatus.OPEN.value,
                    ),
                )
                await self.connection.commit()
            except Exception:
                await self.connection.rollback()
                raise

    async def get_match(self, match_id: str) -> MatchSession | None:
        row = await self._fetchone(
            "SELECT * FROM match_sessions WHERE id = ?", (match_id,)
        )
        return self._session_from_row(row) if row else None

    async def get_match_by_code(self, match_code: str) -> MatchSession | None:
        row = await self._fetchone(
            "SELECT * FROM match_sessions WHERE match_code = ?",
            (match_code.upper(),),
        )
        return self._session_from_row(row) if row else None

    async def get_match_by_source(
        self,
        source_request_type: str,
        source_request_id: str,
    ) -> MatchSession | None:
        row = await self._fetchone(
            """
            SELECT * FROM match_sessions
            WHERE source_request_type = ? AND source_request_id = ?
            """,
            (source_request_type, source_request_id),
        )
        return self._session_from_row(row) if row else None

    async def get_match_by_tier_anchor(
        self,
        tier_anchor_message_id: int,
    ) -> MatchSession | None:
        row = await self._fetchone(
            "SELECT * FROM match_sessions WHERE tier_anchor_message_id = ?",
            (tier_anchor_message_id,),
        )
        return self._session_from_row(row) if row else None

    async def get_active_matches(self, guild_id: int) -> list[MatchSession]:
        rows = await self._fetchall(
            """
            SELECT * FROM match_sessions
            WHERE guild_id = ? AND status IN (?, ?, ?)
            ORDER BY starts_at_utc, created_at_utc, id
            """,
            (guild_id, *ACTIVE_STATUSES),
        )
        return [self._session_from_row(row) for row in rows]

    async def get_latest_match(self, guild_id: int) -> MatchSession | None:
        row = await self._fetchone(
            """
            SELECT * FROM match_sessions
            WHERE guild_id = ?
            ORDER BY created_at_utc DESC LIMIT 1
            """,
            (guild_id,),
        )
        return self._session_from_row(row) if row else None

    async def load_roster(self, match_id: str) -> list[RosterEntry]:
        rows = await self._fetchall(
            "SELECT * FROM roster_entries WHERE match_id = ? ORDER BY reaction_order",
            (match_id,),
        )
        return [self._roster_from_row(row) for row in rows]

    async def create_substitute_recruitment(
        self,
        match_id: str,
        discord_message_id: int,
        now: datetime,
    ) -> SubstituteRecruitment:
        async with self._write_lock:
            await self.connection.execute("BEGIN IMMEDIATE")
            try:
                session = await self._fetchone(
                    """
                    SELECT full_reached_at_utc
                    FROM match_sessions
                    WHERE id = ? AND status = ?
                    """,
                    (match_id, MatchStatus.FULL.value),
                )
                if session is None or session["full_reached_at_utc"] is None:
                    raise ValueError("모집이 완료된 활성 내전이 아닙니다.")
                await self.connection.execute(
                    """
                    INSERT INTO substitute_recruitments (
                        discord_message_id, match_id, status, created_at_utc
                    ) VALUES (?, ?, ?, ?)
                    """,
                    (
                        discord_message_id,
                        match_id,
                        SubstituteRecruitmentStatus.OPEN.value,
                        _to_db(now),
                    ),
                )
                await self.connection.commit()
            except Exception:
                await self.connection.rollback()
                raise
        return SubstituteRecruitment(
            match_id=match_id,
            discord_message_id=discord_message_id,
            status=SubstituteRecruitmentStatus.OPEN,
            created_at=now,
        )

    async def cancel_substitute_recruitment(
        self,
        discord_message_id: int,
        now: datetime,
    ) -> None:
        await self._execute_write(
            """
            UPDATE substitute_recruitments
            SET status = ?
            WHERE discord_message_id = ? AND status = ?
            """,
            (
                SubstituteRecruitmentStatus.CANCELED.value,
                discord_message_id,
                SubstituteRecruitmentStatus.OPEN.value,
            ),
        )

    async def get_active_substitute_recruitments(
        self,
        guild_id: int,
    ) -> list[SubstituteRecruitment]:
        rows = await self._fetchall(
            """
            SELECT sr.*
            FROM substitute_recruitments AS sr
            JOIN match_sessions AS ms ON ms.id = sr.match_id
            WHERE ms.guild_id = ?
              AND ms.status IN (?, ?, ?)
              AND sr.status IN (?, ?)
            ORDER BY sr.created_at_utc, sr.discord_message_id
            """,
            (
                guild_id,
                *ACTIVE_STATUSES,
                SubstituteRecruitmentStatus.OPEN.value,
                SubstituteRecruitmentStatus.FILLED.value,
            ),
        )
        return [self._substitute_recruitment_from_row(row) for row in rows]

    async def get_open_substitute_recruitment(
        self,
        match_id: str,
    ) -> SubstituteRecruitment | None:
        row = await self._fetchone(
            """
            SELECT * FROM substitute_recruitments
            WHERE match_id = ? AND status = ?
            """,
            (match_id, SubstituteRecruitmentStatus.OPEN.value),
        )
        return self._substitute_recruitment_from_row(row) if row else None
