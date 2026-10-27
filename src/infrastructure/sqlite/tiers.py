"""티어 제출·메시지 연결 SQL. 권한·시각·수집 중단을 쓰기 시점에 다시 검사한다."""

from __future__ import annotations

import logging
from datetime import (
    datetime,
)

from src.domain.models import (
    MatchSession,
    NotificationKind,
    RosterStatus,
    SubstituteRecruitmentStatus,
    TierMessageBinding,
    TierParticipantStatus,
    TierSubmission,
    TierUpsertMutation,
    WebTierDTO,
)

from .base import ACTIVE_STATUSES, SQLiteStore, _from_db, _to_db

logger = logging.getLogger(__name__)


class TierStorage(SQLiteStore):
    """티어 제출·메시지 연결 SQL. 권한·시각·수집 중단을 쓰기 시점에 다시 검사한다."""

    async def get_tier_candidates(
        self,
        guild_id: int,
        discord_user_id: int,
        activity_at: datetime,
    ) -> list[MatchSession]:
        rows = await self._fetchall(
            """
            SELECT m.*
            FROM match_sessions m
            JOIN roster_entries r ON r.match_id = m.id
            WHERE m.guild_id = ?
              AND m.status IN (?, ?, ?)
              AND r.discord_user_id = ?
              AND r.status IN (?, ?)
              AND m.starts_at_utc > ?
              AND (
                    (
                        m.recruitment_completed_notified_at_utc IS NOT NULL
                        AND m.recruitment_completed_notified_at_utc <= ?
                        AND m.tier_deadline_at_utc > ?
                    )
                    OR EXISTS (
                        SELECT 1
                        FROM substitute_recruitments sr
                        WHERE sr.match_id = m.id
                          AND sr.status = ?
                          AND sr.recruited_user_id = r.discord_user_id
                          AND sr.filled_at_utc <= ?
                    )
              )
            ORDER BY m.starts_at_utc, m.created_at_utc, m.id
            """,
            (
                guild_id,
                *ACTIVE_STATUSES,
                discord_user_id,
                RosterStatus.CONFIRMED.value,
                RosterStatus.WAITLISTED.value,
                _to_db(activity_at),
                _to_db(activity_at),
                _to_db(activity_at),
                SubstituteRecruitmentStatus.FILLED.value,
                _to_db(activity_at),
            ),
        )
        return [self._session_from_row(row) for row in rows]

    async def is_tier_activity_allowed(
        self,
        match_id: str,
        discord_user_id: int,
        activity_at: datetime,
    ) -> bool:
        row = await self._fetchone(
            "SELECT * FROM match_sessions WHERE id = ?",
            (match_id,),
        )
        if row is None:
            return False
        return await self._is_tier_activity_allowed(
            self._session_from_row(row),
            discord_user_id,
            activity_at,
        )

    async def _is_tier_activity_allowed(
        self,
        session: MatchSession,
        discord_user_id: int,
        activity_at: datetime,
    ) -> bool:
        if await self.tier_collection_disabled(session.id, discord_user_id):
            return False
        if session.accepts_tier_activity_at(activity_at):
            return True
        if not session.accepts_activity_at(activity_at):
            return False
        row = await self._fetchone(
            """
            SELECT 1
            FROM substitute_recruitments
            WHERE match_id = ?
              AND status = ?
              AND recruited_user_id = ?
              AND filled_at_utc <= ?
            LIMIT 1
            """,
            (
                session.id,
                SubstituteRecruitmentStatus.FILLED.value,
                discord_user_id,
                _to_db(activity_at),
            ),
        )
        return row is not None

    async def tier_collection_disabled(self, match_id: str, user_id: int) -> bool:
        return (
            await self._fetchone(
                "SELECT 1 FROM tier_collection_opt_outs WHERE match_id = ? AND user_id = ?",
                (match_id, user_id),
            )
            is not None
        )

    async def get_tier_binding(
        self,
        discord_message_id: int,
    ) -> TierMessageBinding | None:
        row = await self._fetchone(
            """
            SELECT discord_message_id, match_id, discord_user_id, bound_at_utc
            FROM tier_message_bindings
            WHERE discord_message_id = ?
            """,
            (discord_message_id,),
        )
        if row is None:
            return None
        return TierMessageBinding(
            discord_message_id=int(row["discord_message_id"]),
            match_id=str(row["match_id"]),
            discord_user_id=int(row["discord_user_id"]),
            bound_at=_from_db(row["bound_at_utc"]),  # type: ignore[arg-type]
        )

    async def upsert_tier_message(
        self,
        binding: TierMessageBinding,
        submission: TierSubmission,
    ) -> bool:
        if (
            binding.discord_message_id != submission.discord_message_id
            or binding.match_id != submission.match_id
            or binding.discord_user_id != submission.discord_user_id
        ):
            raise ValueError("tier binding and submission do not match")
        async with self._write_lock:
            await self.connection.execute("BEGIN IMMEDIATE")
            try:
                existing = await self._fetchone(
                    """
                    SELECT match_id, discord_user_id
                    FROM tier_message_bindings
                    WHERE discord_message_id = ?
                    """,
                    (binding.discord_message_id,),
                )
                if existing is not None and (
                    str(existing["match_id"]) != binding.match_id
                    or int(existing["discord_user_id"]) != binding.discord_user_id
                ):
                    await self.connection.rollback()
                    return False
                session_row = await self._fetchone(
                    "SELECT * FROM match_sessions WHERE id = ?",
                    (binding.match_id,),
                )
                if session_row is None:
                    await self.connection.rollback()
                    return False
                session = self._session_from_row(session_row)
                if not await self._is_tier_activity_allowed(
                    session,
                    submission.discord_user_id,
                    submission.activity_at,
                ):
                    await self.connection.rollback()
                    return False
                roster = await self._fetchone(
                    """
                    SELECT status FROM roster_entries
                    WHERE match_id = ? AND discord_user_id = ?
                    """,
                    (binding.match_id, binding.discord_user_id),
                )
                if roster is None or roster["status"] not in {
                    RosterStatus.CONFIRMED.value,
                    RosterStatus.WAITLISTED.value,
                }:
                    await self.connection.rollback()
                    return False
                if existing is None:
                    await self.connection.execute(
                        """
                        INSERT INTO tier_message_bindings (
                            discord_message_id, match_id, discord_user_id, bound_at_utc
                        ) VALUES (?, ?, ?, ?)
                        """,
                        (
                            binding.discord_message_id,
                            binding.match_id,
                            binding.discord_user_id,
                            _to_db(binding.bound_at),
                        ),
                    )
                await self._apply_tier_upsert(
                    TierUpsertMutation(
                        match_id=submission.match_id,
                        discord_user_id=submission.discord_user_id,
                        discord_display_name=submission.discord_display_name,
                        raw_tier_message=submission.raw_tier_message,
                        discord_message_id=submission.discord_message_id,
                        activity_at=submission.activity_at,
                        collected_at=submission.collected_at,
                    )
                )
                await self.connection.commit()
                return True
            except Exception:
                await self.connection.rollback()
                raise

    async def delete_bound_tier_message(
        self,
        discord_message_id: int,
        received_at: datetime,
    ) -> TierMessageBinding | None:
        async with self._write_lock:
            await self.connection.execute("BEGIN IMMEDIATE")
            try:
                row = await self._fetchone(
                    """
                    SELECT b.discord_message_id, b.match_id, b.discord_user_id,
                           b.bound_at_utc, m.starts_at_utc, m.status
                    FROM tier_message_bindings b
                    JOIN match_sessions m ON m.id = b.match_id
                    WHERE b.discord_message_id = ?
                    """,
                    (discord_message_id,),
                )
                if (
                    row is None
                    or row["status"] not in ACTIVE_STATUSES
                    or received_at >= _from_db(row["starts_at_utc"])  # type: ignore[operator]
                ):
                    await self.connection.rollback()
                    return None
                await self.connection.execute(
                    """
                    DELETE FROM tier_submissions
                    WHERE match_id = ? AND discord_message_id = ?
                    """,
                    (str(row["match_id"]), discord_message_id),
                )
                await self.connection.commit()
                return TierMessageBinding(
                    discord_message_id=int(row["discord_message_id"]),
                    match_id=str(row["match_id"]),
                    discord_user_id=int(row["discord_user_id"]),
                    bound_at=_from_db(row["bound_at_utc"]),  # type: ignore[arg-type]
                )
            except Exception:
                await self.connection.rollback()
                raise

    async def request_tier_anchor_recreation(
        self,
        tier_anchor_message_id: int,
        now: datetime,
    ) -> MatchSession | None:
        async with self._write_lock:
            await self.connection.execute("BEGIN IMMEDIATE")
            try:
                row = await self._fetchone(
                    """
                    SELECT * FROM match_sessions
                    WHERE tier_anchor_message_id = ?
                      AND status IN (?, ?, ?)
                      AND starts_at_utc > ?
                    """,
                    (
                        tier_anchor_message_id,
                        *ACTIVE_STATUSES,
                        _to_db(now),
                    ),
                )
                if row is None:
                    await self.connection.rollback()
                    return None
                session = self._session_from_row(row)
                await self.connection.execute(
                    """
                    UPDATE match_sessions
                    SET tier_anchor_message_id = NULL, updated_at_utc = ?
                    WHERE id = ? AND tier_anchor_message_id = ?
                    """,
                    (_to_db(now), session.id, tier_anchor_message_id),
                )
                await self._insert_notification(
                    match_id=session.id,
                    kind=NotificationKind.TIER_ANCHOR,
                    channel_id=session.tier_channel_id,
                    payload={
                        "recreated": True,
                        "deleted_anchor_message_id": tier_anchor_message_id,
                    },
                    dedupe_key=(
                        f"match:{session.id}:tier-anchor:replacement:"
                        f"{tier_anchor_message_id}"
                    ),
                    now=now,
                )
                await self.connection.commit()
                session.tier_anchor_message_id = None
                session.updated_at = now
                return session
            except Exception:
                await self.connection.rollback()
                raise

    async def get_tier_submission(
        self, match_id: str, user_id: int
    ) -> TierSubmission | None:
        row = await self._fetchone(
            """
            SELECT * FROM tier_submissions
            WHERE match_id = ? AND discord_user_id = ?
            """,
            (match_id, user_id),
        )
        if row is None:
            return None
        return TierSubmission(
            match_id=str(row["match_id"]),
            discord_user_id=int(row["discord_user_id"]),
            discord_display_name=str(row["discord_display_name"]),
            raw_tier_message=str(row["raw_tier_message"]),
            discord_message_id=int(row["discord_message_id"]),
            activity_at=_from_db(row["activity_at_utc"]),  # type: ignore[arg-type]
            collected_at=_from_db(row["collected_at_utc"]),  # type: ignore[arg-type]
        )

    async def get_tier_status(self, match_id: str) -> list[TierParticipantStatus]:
        rows = await self._fetchall(
            """
            SELECT r.discord_user_id, r.discord_display_name, r.reaction_order,
                   CASE WHEN t.discord_user_id IS NULL THEN 0 ELSE 1 END AS has_tier
            FROM roster_entries r
            LEFT JOIN tier_submissions t
              ON t.match_id = r.match_id AND t.discord_user_id = r.discord_user_id
            WHERE r.match_id = ? AND r.status = ?
            ORDER BY r.reaction_order
            """,
            (match_id, RosterStatus.CONFIRMED.value),
        )
        return [
            TierParticipantStatus(
                discord_user_id=int(row["discord_user_id"]),
                discord_display_name=str(row["discord_display_name"]),
                reaction_order=int(row["reaction_order"]),
                has_tier=bool(row["has_tier"]),
            )
            for row in rows
        ]

    async def export_web_tiers(self, match_id: str) -> list[WebTierDTO]:
        rows = await self._fetchall(
            """
            SELECT r.discord_user_id, t.discord_display_name, r.reaction_order,
                   t.raw_tier_message
            FROM roster_entries r
            JOIN tier_submissions t
              ON t.match_id = r.match_id AND t.discord_user_id = r.discord_user_id
            WHERE r.match_id = ? AND r.status = ?
            ORDER BY r.reaction_order
            """,
            (match_id, RosterStatus.CONFIRMED.value),
        )
        return [
            WebTierDTO(
                discord_user_id=str(row["discord_user_id"]),
                discord_display_name=str(row["discord_display_name"]),
                raw_tier_message=str(row["raw_tier_message"]),
                reaction_order=int(row["reaction_order"]),
            )
            for row in rows
        ]
