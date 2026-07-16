from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Sequence

import aiosqlite

from src.domain.models import (
    MatchSession,
    MatchStatus,
    NotificationKind,
    NotificationRecord,
    NotificationStatus,
    PersistenceMutation,
    ReactionMutation,
    RosterEntry,
    RosterStatus,
    TierMessageBinding,
    TierDeleteMutation,
    TierParticipantStatus,
    TierSubmission,
    TierUpsertMutation,
    WebTierDTO,
    WaitlistReason,
)
from src.domain.match_code import deterministic_match_code
from src.ports.repositories import MatchRepository


logger = logging.getLogger(__name__)


ACTIVE_STATUSES = (
    MatchStatus.CREATED.value,
    MatchStatus.RECRUITING.value,
    MatchStatus.FULL.value,
)


def _to_db(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("naive datetimes are not allowed")
    return value.astimezone(timezone.utc).isoformat()


def _from_db(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


class SQLiteMatchRepository(MatchRepository):
    def __init__(self, database_path: Path) -> None:
        self._database_path = database_path
        self._connection: aiosqlite.Connection | None = None
        self._write_lock = asyncio.Lock()

    @property
    def connection(self) -> aiosqlite.Connection:
        if self._connection is None:
            raise RuntimeError("repository is not initialized")
        return self._connection

    async def initialize(self, migration_path: Path) -> None:
        self._database_path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = await aiosqlite.connect(self._database_path)
        self._connection.row_factory = aiosqlite.Row
        await self._connection.execute("PRAGMA foreign_keys = ON")
        await self._connection.execute("PRAGMA journal_mode = WAL")
        await self._connection.execute("PRAGMA busy_timeout = 5000")
        await self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                name TEXT PRIMARY KEY,
                applied_at_utc TEXT NOT NULL
            )
            """
        )
        await self._connection.commit()
        migration_paths = sorted(migration_path.parent.glob("*.sql"))
        if migration_path not in migration_paths:
            migration_paths.insert(0, migration_path)
        applied_rows = await self._fetchall("SELECT name FROM schema_migrations")
        applied = {str(row["name"]) for row in applied_rows}
        for path in migration_paths:
            if path.name in applied:
                continue
            if path.name == "002_multi_match.sql":
                await self._ensure_tier_missing_reminder_schema()
            await self._connection.executescript(path.read_text(encoding="utf-8"))
            await self._connection.execute(
                "INSERT INTO schema_migrations (name, applied_at_utc) VALUES (?, ?)",
                (path.name, _to_db(datetime.now(timezone.utc))),
            )
            await self._connection.commit()
        await self._backfill_match_codes()
        logger.info(
            "SQLite connected and migrations completed database=%s directory=%s",
            self._database_path,
            migration_path.parent,
        )

    async def _backfill_match_codes(self) -> None:
        rows = await self._fetchall(
            """
            SELECT id FROM match_sessions
            WHERE match_code IS NULL OR TRIM(match_code) = ''
            ORDER BY created_at_utc, id
            """
        )
        if not rows:
            return
        existing_rows = await self._fetchall(
            "SELECT match_code FROM match_sessions WHERE match_code IS NOT NULL"
        )
        existing = {str(row["match_code"]) for row in existing_rows}
        for row in rows:
            match_id = str(row["id"])
            attempt = 0
            while True:
                code = deterministic_match_code(match_id, attempt)
                if code not in existing:
                    break
                attempt += 1
            await self.connection.execute(
                "UPDATE match_sessions SET match_code = ? WHERE id = ?",
                (code, match_id),
            )
            existing.add(code)
        await self.connection.commit()

    async def _ensure_tier_missing_reminder_schema(self) -> None:
        columns = await self._fetchall("PRAGMA table_info(match_sessions)")
        column_names = {str(row["name"]) for row in columns}
        if "tier_missing_reminder_notified_at_utc" not in column_names:
            await self.connection.execute(
                "ALTER TABLE match_sessions "
                "ADD COLUMN tier_missing_reminder_notified_at_utc TEXT"
            )
            await self.connection.commit()

        outbox = await self._fetchone(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'notification_outbox'"
        )
        create_sql = str(outbox["sql"] or "") if outbox is not None else ""
        if "TIER_MISSING_REMINDER" in create_sql:
            return
        await self.connection.executescript(
            """
            PRAGMA foreign_keys = OFF;
            BEGIN IMMEDIATE;
            DROP TABLE IF EXISTS notification_outbox_v2;
            CREATE TABLE notification_outbox_v2 (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                match_id TEXT NOT NULL REFERENCES match_sessions(id) ON DELETE CASCADE,
                kind TEXT NOT NULL CHECK (kind IN (
                    'RECRUITMENT_COMPLETE', 'TIER_MISSING_REMINDER',
                    'LOBBY_REMINDER', 'TIER_COMPLETE'
                )),
                channel_id INTEGER NOT NULL,
                payload_json TEXT NOT NULL,
                dedupe_key TEXT NOT NULL UNIQUE,
                status TEXT NOT NULL CHECK (status IN (
                    'PENDING', 'SENDING', 'SENT', 'FAILED', 'CANCELED'
                )),
                attempts INTEGER NOT NULL DEFAULT 0,
                next_attempt_at_utc TEXT NOT NULL,
                last_error TEXT,
                discord_message_id INTEGER,
                created_at_utc TEXT NOT NULL,
                sent_at_utc TEXT
            );
            INSERT INTO notification_outbox_v2 (
                id, match_id, kind, channel_id, payload_json, dedupe_key,
                status, attempts, next_attempt_at_utc, last_error,
                discord_message_id, created_at_utc, sent_at_utc
            )
            SELECT
                id, match_id, kind, channel_id, payload_json, dedupe_key,
                status, attempts, next_attempt_at_utc, last_error,
                discord_message_id, created_at_utc, sent_at_utc
            FROM notification_outbox;
            DROP TABLE notification_outbox;
            ALTER TABLE notification_outbox_v2 RENAME TO notification_outbox;
            CREATE INDEX ix_notification_pending
            ON notification_outbox(status, next_attempt_at_utc);
            COMMIT;
            PRAGMA foreign_keys = ON;
            """
        )

    async def close(self) -> None:
        if self._connection is not None:
            await self._connection.close()
            self._connection = None

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
                        full_reached_at_utc, recruitment_completed_notified_at_utc,
                        tier_complete_notified_at_utc,
                        tier_missing_reminder_notified_at_utc, lobby_notified_at_utc,
                        last_missing_tier_reminder_at_utc, next_arrival_seq,
                        created_at_utc, updated_at_utc
                    ) VALUES (
                        ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                        ?, ?, ?, ?, ?, ?, ?, ?
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
                        _to_db(session.full_reached_at) if session.full_reached_at else None,
                        _to_db(session.recruitment_completed_notified_at)
                        if session.recruitment_completed_notified_at
                        else None,
                        _to_db(session.tier_complete_notified_at)
                        if session.tier_complete_notified_at
                        else None,
                        _to_db(session.tier_missing_reminder_notified_at)
                        if session.tier_missing_reminder_notified_at
                        else None,
                        _to_db(session.lobby_notified_at) if session.lobby_notified_at else None,
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
                    (MatchStatus.CANCELED.value, _to_db(now), match_id, *ACTIVE_STATUSES),
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
                    (MatchStatus.STARTED.value, _to_db(now), match_id, *ACTIVE_STATUSES),
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
                await self.connection.commit()
            except Exception:
                await self.connection.rollback()
                raise

    async def get_match(self, match_id: str) -> MatchSession | None:
        row = await self._fetchone("SELECT * FROM match_sessions WHERE id = ?", (match_id,))
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
              AND m.recruitment_completed_notified_at_utc IS NOT NULL
              AND m.recruitment_completed_notified_at_utc <= ?
              AND m.tier_deadline_at_utc > ?
              AND m.starts_at_utc > ?
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
            ),
        )
        return [self._session_from_row(row) for row in rows]

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
                if not session.accepts_tier_activity_at(submission.activity_at):
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

    async def apply_mutations(self, mutations: Sequence[PersistenceMutation]) -> None:
        if not mutations:
            return
        async with self._write_lock:
            await self.connection.execute("BEGIN IMMEDIATE")
            try:
                match_ids = tuple(dict.fromkeys(mutation.match_id for mutation in mutations))
                placeholders = ",".join("?" for _ in match_ids)
                rows = await self._fetchall(
                    f"SELECT * FROM match_sessions WHERE id IN ({placeholders})",
                    match_ids,
                )
                sessions = {str(row["id"]): self._session_from_row(row) for row in rows}

                for mutation in mutations:
                    session = sessions.get(mutation.match_id)
                    if session is None or not session.is_automatic():
                        continue
                    if isinstance(mutation, ReactionMutation):
                        await self._apply_reaction_mutation(mutation)
                    elif isinstance(mutation, TierUpsertMutation):
                        if session.accepts_tier_activity_at(mutation.activity_at):
                            await self._apply_tier_upsert(mutation)
                    elif isinstance(mutation, TierDeleteMutation):
                        await self.connection.execute(
                            "DELETE FROM tier_submissions WHERE match_id = ? AND discord_message_id = ?",
                            (mutation.match_id, mutation.discord_message_id),
                        )
                await self.connection.commit()
            except Exception:
                await self.connection.rollback()
                raise

    async def _apply_reaction_mutation(self, mutation: ReactionMutation) -> None:
        await self.connection.execute(
            """
            INSERT OR IGNORE INTO reaction_events (
                match_id, discord_user_id, action, arrival_seq, received_at_utc, outcome
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                mutation.match_id,
                mutation.discord_user_id,
                mutation.action.value,
                mutation.arrival_seq,
                _to_db(mutation.received_at),
                mutation.outcome,
            ),
        )
        entry = mutation.roster_entry
        if entry is not None:
            await self.connection.execute(
                """
                INSERT INTO roster_entries (
                    match_id, discord_user_id, discord_display_name, reaction_order,
                    status, reacted_at_utc, removed_at_utc,
                    waitlist_reason, conflict_match_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(match_id, discord_user_id) DO UPDATE SET
                    discord_display_name = excluded.discord_display_name,
                    reaction_order = excluded.reaction_order,
                    status = excluded.status,
                    reacted_at_utc = excluded.reacted_at_utc,
                    removed_at_utc = excluded.removed_at_utc,
                    waitlist_reason = excluded.waitlist_reason,
                    conflict_match_id = excluded.conflict_match_id
                """,
                (
                    entry.match_id,
                    entry.discord_user_id,
                    entry.discord_display_name,
                    entry.reaction_order,
                    entry.status.value,
                    _to_db(entry.reacted_at),
                    _to_db(entry.removed_at) if entry.removed_at else None,
                    entry.waitlist_reason.value if entry.waitlist_reason else None,
                    entry.conflict_match_id,
                ),
            )

        await self.connection.execute(
            """
            UPDATE match_sessions
            SET status = ?, next_arrival_seq = MAX(next_arrival_seq, ?),
                full_reached_at_utc = COALESCE(full_reached_at_utc, ?),
                updated_at_utc = ?
            WHERE id = ? AND status IN (?, ?, ?)
            """,
            (
                mutation.match_status.value,
                mutation.next_arrival_seq,
                _to_db(mutation.full_reached_at) if mutation.full_reached_at else None,
                _to_db(mutation.received_at),
                mutation.match_id,
                *ACTIVE_STATUSES,
            ),
        )

        if mutation.completion_user_ids:
            await self._insert_notification(
                match_id=mutation.match_id,
                kind=NotificationKind.RECRUITMENT_COMPLETE,
                channel_id=await self._match_channel(mutation.match_id, "announcement_channel_id"),
                payload={"user_ids": list(mutation.completion_user_ids)},
                dedupe_key=f"match:{mutation.match_id}:recruitment-complete",
                now=mutation.received_at,
            )

    async def _apply_tier_upsert(self, mutation: TierUpsertMutation) -> None:
        roster = await self._fetchone(
            """
            SELECT status FROM roster_entries
            WHERE match_id = ? AND discord_user_id = ?
            """,
            (mutation.match_id, mutation.discord_user_id),
        )
        if roster is None or roster["status"] not in {
            RosterStatus.CONFIRMED.value,
            RosterStatus.WAITLISTED.value,
        }:
            return

        await self.connection.execute(
            """
            INSERT INTO tier_submissions (
                match_id, discord_user_id, discord_display_name, raw_tier_message,
                discord_message_id, activity_at_utc, collected_at_utc
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(match_id, discord_user_id) DO UPDATE SET
                discord_display_name = excluded.discord_display_name,
                raw_tier_message = excluded.raw_tier_message,
                discord_message_id = excluded.discord_message_id,
                activity_at_utc = excluded.activity_at_utc,
                collected_at_utc = excluded.collected_at_utc
            WHERE excluded.activity_at_utc > tier_submissions.activity_at_utc
               OR (
                    excluded.activity_at_utc = tier_submissions.activity_at_utc
                    AND excluded.discord_message_id > tier_submissions.discord_message_id
               )
            """,
            (
                mutation.match_id,
                mutation.discord_user_id,
                mutation.discord_display_name,
                mutation.raw_tier_message,
                mutation.discord_message_id,
                _to_db(mutation.activity_at),
                _to_db(mutation.collected_at),
            ),
        )

    async def get_tier_submission(self, match_id: str, user_id: int) -> TierSubmission | None:
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

    async def claim_missing_tier_reminder(
        self, match_id: str, now: datetime, cooldown_seconds: int
    ) -> tuple[bool, list[TierParticipantStatus]]:
        async with self._write_lock:
            await self.connection.execute("BEGIN IMMEDIATE")
            try:
                session = await self._fetchone(
                    "SELECT last_missing_tier_reminder_at_utc FROM match_sessions WHERE id = ?",
                    (match_id,),
                )
                if session is None:
                    await self.connection.rollback()
                    return False, []
                last = _from_db(session["last_missing_tier_reminder_at_utc"])
                if last and (now - last).total_seconds() < cooldown_seconds:
                    await self.connection.rollback()
                    return False, []
                statuses = await self.get_tier_status(match_id)
                missing = [item for item in statuses if not item.has_tier]
                if not missing:
                    await self.connection.rollback()
                    return True, []
                await self.connection.execute(
                    """
                    UPDATE match_sessions
                    SET last_missing_tier_reminder_at_utc = ?, updated_at_utc = ?
                    WHERE id = ?
                    """,
                    (_to_db(now), _to_db(now), match_id),
                )
                await self.connection.commit()
                return True, missing
            except Exception:
                await self.connection.rollback()
                raise

    async def enqueue_lobby_notification(self, match_id: str, now: datetime) -> None:
        async with self._write_lock:
            await self.connection.execute("BEGIN IMMEDIATE")
            try:
                session = await self._fetchone(
                    """
                    SELECT announcement_channel_id, full_reached_at_utc
                    FROM match_sessions
                    WHERE id = ? AND status IN (?, ?, ?)
                    """,
                    (match_id, *ACTIVE_STATUSES),
                )
                if session is None or session["full_reached_at_utc"] is None:
                    await self.connection.rollback()
                    return
                rows = await self._fetchall(
                    """
                    SELECT discord_user_id FROM roster_entries
                    WHERE match_id = ? AND status = ?
                    ORDER BY reaction_order
                    """,
                    (match_id, RosterStatus.CONFIRMED.value),
                )
                await self._insert_notification(
                    match_id=match_id,
                    kind=NotificationKind.LOBBY_REMINDER,
                    channel_id=int(session["announcement_channel_id"]),
                    payload={"user_ids": [int(row["discord_user_id"]) for row in rows]},
                    dedupe_key=f"match:{match_id}:lobby-reminder",
                    now=now,
                )
                await self.connection.commit()
            except Exception:
                await self.connection.rollback()
                raise

    async def enqueue_tier_missing_reminder_if_due(
        self, match_id: str, now: datetime
    ) -> None:
        async with self._write_lock:
            await self.connection.execute("BEGIN IMMEDIATE")
            try:
                session = await self._fetchone(
                    """
                    SELECT announcement_channel_id, tier_deadline_at_utc,
                           recruitment_completed_notified_at_utc,
                           tier_missing_reminder_notified_at_utc
                    FROM match_sessions
                    WHERE id = ? AND status IN (?, ?, ?)
                    """,
                    (match_id, *ACTIVE_STATUSES),
                )
                deadline = (
                    _from_db(session["tier_deadline_at_utc"])
                    if session is not None
                    else None
                )
                if (
                    session is None
                    or deadline is None
                    or now < deadline
                    or session["recruitment_completed_notified_at_utc"] is None
                    or session["tier_missing_reminder_notified_at_utc"] is not None
                ):
                    await self.connection.rollback()
                    return
                statuses = await self.get_tier_status(match_id)
                missing_user_ids = [
                    item.discord_user_id for item in statuses if not item.has_tier
                ]
                if not missing_user_ids:
                    await self.connection.rollback()
                    return
                await self._insert_notification(
                    match_id=match_id,
                    kind=NotificationKind.TIER_MISSING_REMINDER,
                    channel_id=int(session["announcement_channel_id"]),
                    payload={"user_ids": missing_user_ids},
                    dedupe_key=f"match:{match_id}:tier-missing-reminder",
                    now=now,
                )
                await self.connection.commit()
            except Exception:
                await self.connection.rollback()
                raise

    async def enqueue_tier_complete_if_ready(self, match_id: str, now: datetime) -> None:
        async with self._write_lock:
            await self.connection.execute("BEGIN IMMEDIATE")
            try:
                session = await self._fetchone(
                    """
                    SELECT admin_channel_id, recruitment_completed_notified_at_utc,
                           tier_complete_notified_at_utc
                    FROM match_sessions
                    WHERE id = ? AND status IN (?, ?, ?)
                    """,
                    (match_id, *ACTIVE_STATUSES),
                )
                if (
                    session is None
                    or session["recruitment_completed_notified_at_utc"] is None
                    or session["tier_complete_notified_at_utc"] is not None
                ):
                    await self.connection.rollback()
                    return
                counts = await self._fetchone(
                    """
                    SELECT COUNT(*) AS total,
                           SUM(CASE WHEN t.discord_user_id IS NULL THEN 1 ELSE 0 END) AS missing
                    FROM roster_entries r
                    LEFT JOIN tier_submissions t
                      ON t.match_id = r.match_id AND t.discord_user_id = r.discord_user_id
                    WHERE r.match_id = ? AND r.status = ?
                    """,
                    (match_id, RosterStatus.CONFIRMED.value),
                )
                total = int(counts["total"] or 0)
                missing = int(counts["missing"] or 0)
                if total == 0 or missing != 0:
                    await self.connection.rollback()
                    return
                await self._insert_notification(
                    match_id=match_id,
                    kind=NotificationKind.TIER_COMPLETE,
                    channel_id=int(session["admin_channel_id"]),
                    payload={},
                    dedupe_key=f"match:{match_id}:tier-complete",
                    now=now,
                )
                await self.connection.commit()
            except Exception:
                await self.connection.rollback()
                raise

    async def reset_sending_notifications(self) -> None:
        await self._execute_write(
            "UPDATE notification_outbox SET status = ? WHERE status = ?",
            (NotificationStatus.PENDING.value, NotificationStatus.SENDING.value),
        )

    async def claim_notifications(self, now: datetime, limit: int = 10) -> list[NotificationRecord]:
        async with self._write_lock:
            await self.connection.execute("BEGIN IMMEDIATE")
            try:
                rows = await self._fetchall(
                    """
                    SELECT n.* FROM notification_outbox n
                    JOIN match_sessions m ON m.id = n.match_id
                    WHERE n.status = ? AND n.next_attempt_at_utc <= ?
                      AND m.status IN (?, ?, ?)
                      AND m.starts_at_utc > ?
                    ORDER BY n.id LIMIT ?
                    """,
                    (
                        NotificationStatus.PENDING.value,
                        _to_db(now),
                        *ACTIVE_STATUSES,
                        _to_db(now),
                        limit,
                    ),
                )
                ids = [int(row["id"]) for row in rows]
                if ids:
                    placeholders = ",".join("?" for _ in ids)
                    await self.connection.execute(
                        f"UPDATE notification_outbox SET status = ?, attempts = attempts + 1 "
                        f"WHERE id IN ({placeholders})",
                        (NotificationStatus.SENDING.value, *ids),
                    )
                await self.connection.commit()
                return [self._notification_from_row(row, attempts_delta=1) for row in rows]
            except Exception:
                await self.connection.rollback()
                raise

    async def mark_notification_sent(
        self,
        notification: NotificationRecord,
        discord_message_id: int | None,
        sent_at: datetime,
    ) -> None:
        async with self._write_lock:
            await self.connection.execute("BEGIN IMMEDIATE")
            try:
                await self.connection.execute(
                    """
                    UPDATE notification_outbox
                    SET status = ?, discord_message_id = ?, sent_at_utc = ?, last_error = NULL
                    WHERE id = ? AND status = ?
                    """,
                    (
                        NotificationStatus.SENT.value,
                        discord_message_id,
                        _to_db(sent_at),
                        notification.id,
                        NotificationStatus.SENDING.value,
                    ),
                )
                timestamp_columns = {
                    NotificationKind.RECRUITMENT_COMPLETE:
                        "recruitment_completed_notified_at_utc",
                    NotificationKind.TIER_MISSING_REMINDER:
                        "tier_missing_reminder_notified_at_utc",
                    NotificationKind.LOBBY_REMINDER: "lobby_notified_at_utc",
                    NotificationKind.TIER_COMPLETE: "tier_complete_notified_at_utc",
                }
                column = timestamp_columns.get(notification.kind)
                if column is not None:
                    await self.connection.execute(
                        f"UPDATE match_sessions "
                        f"SET {column} = COALESCE({column}, ?), updated_at_utc = ? "
                        "WHERE id = ?",
                        (_to_db(sent_at), _to_db(sent_at), notification.match_id),
                    )
                elif notification.kind is NotificationKind.TIER_ANCHOR:
                    if discord_message_id is None:
                        raise ValueError("tier anchor notification requires a message ID")
                    await self.connection.execute(
                        """
                        UPDATE match_sessions
                        SET tier_anchor_message_id = ?, updated_at_utc = ?
                        WHERE id = ?
                        """,
                        (
                            discord_message_id,
                            _to_db(sent_at),
                            notification.match_id,
                        ),
                    )
                else:
                    await self.connection.execute(
                        """
                        UPDATE match_sessions
                        SET updated_at_utc = ?
                        WHERE id = ?
                        """,
                        (_to_db(sent_at), notification.match_id),
                    )

                if notification.kind is NotificationKind.RECRUITMENT_COMPLETE:
                    session = await self._fetchone(
                        """
                        SELECT tier_channel_id, tier_anchor_message_id
                        FROM match_sessions WHERE id = ?
                        """,
                        (notification.match_id,),
                    )
                    if session is not None and session["tier_anchor_message_id"] is None:
                        await self._insert_notification(
                            match_id=notification.match_id,
                            kind=NotificationKind.TIER_ANCHOR,
                            channel_id=int(session["tier_channel_id"]),
                            payload={"recreated": False},
                            dedupe_key=f"match:{notification.match_id}:tier-anchor:initial",
                            now=sent_at,
                        )
                elif (
                    notification.kind is NotificationKind.TIER_ANCHOR
                    and bool(notification.payload.get("recreated"))
                ):
                    session = await self._fetchone(
                        "SELECT admin_channel_id FROM match_sessions WHERE id = ?",
                        (notification.match_id,),
                    )
                    if session is not None:
                        await self._insert_notification(
                            match_id=notification.match_id,
                            kind=NotificationKind.TIER_ANCHOR_RECREATED,
                            channel_id=int(session["admin_channel_id"]),
                            payload={
                                "tier_anchor_message_id": discord_message_id,
                                "deleted_anchor_message_id": notification.payload.get(
                                    "deleted_anchor_message_id"
                                ),
                            },
                            dedupe_key=(
                                f"match:{notification.match_id}:"
                                f"tier-anchor-recreated:{notification.id}"
                            ),
                            now=sent_at,
                        )
                await self.connection.commit()
            except Exception:
                await self.connection.rollback()
                raise

    async def mark_notification_failed(
        self, notification: NotificationRecord, error: str, now: datetime, max_attempts: int
    ) -> None:
        terminal = notification.attempts >= max_attempts
        status = NotificationStatus.FAILED if terminal else NotificationStatus.PENDING
        delay = min(2 ** max(notification.attempts, 1), 60)
        await self._execute_write(
            """
            UPDATE notification_outbox
            SET status = ?, last_error = ?, next_attempt_at_utc = ?
            WHERE id = ? AND status = ?
            """,
            (
                status.value,
                error[:1000],
                _to_db(now + timedelta(seconds=delay)),
                notification.id,
                NotificationStatus.SENDING.value,
            ),
        )

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

    async def notification_count(self, match_id: str, kind: NotificationKind) -> int:
        row = await self._fetchone(
            "SELECT COUNT(*) AS count FROM notification_outbox WHERE match_id = ? AND kind = ?",
            (match_id, kind.value),
        )
        return int(row["count"])

    async def _insert_notification(
        self,
        *,
        match_id: str,
        kind: NotificationKind,
        channel_id: int,
        payload: dict[str, Any],
        dedupe_key: str,
        now: datetime,
    ) -> None:
        await self.connection.execute(
            """
            INSERT OR IGNORE INTO notification_outbox (
                match_id, kind, channel_id, payload_json, dedupe_key,
                status, attempts, next_attempt_at_utc, created_at_utc
            ) VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?)
            """,
            (
                match_id,
                kind.value,
                channel_id,
                json.dumps(payload, ensure_ascii=False),
                dedupe_key,
                NotificationStatus.PENDING.value,
                _to_db(now),
                _to_db(now),
            ),
        )

    async def _match_channel(self, match_id: str, column: str) -> int:
        if column not in {"announcement_channel_id", "tier_channel_id", "admin_channel_id"}:
            raise ValueError("unsupported channel column")
        row = await self._fetchone(f"SELECT {column} FROM match_sessions WHERE id = ?", (match_id,))
        if row is None:
            raise LookupError(match_id)
        return int(row[column])

    async def _execute_write(self, sql: str, parameters: tuple[Any, ...]) -> None:
        async with self._write_lock:
            await self.connection.execute(sql, parameters)
            await self.connection.commit()

    async def _fetchone(
        self, sql: str, parameters: tuple[Any, ...] = ()
    ) -> aiosqlite.Row | None:
        async with self.connection.execute(sql, parameters) as cursor:
            return await cursor.fetchone()

    async def _fetchall(
        self, sql: str, parameters: tuple[Any, ...] = ()
    ) -> list[aiosqlite.Row]:
        async with self.connection.execute(sql, parameters) as cursor:
            return list(await cursor.fetchall())

    @staticmethod
    def _session_from_row(row: aiosqlite.Row) -> MatchSession:
        return MatchSession(
            id=str(row["id"]),
            match_code=str(row["match_code"]),
            guild_id=int(row["guild_id"]),
            manager_user_id=int(row["manager_user_id"]),
            command_channel_id=int(row["command_channel_id"]),
            announcement_channel_id=int(row["announcement_channel_id"]),
            announcement_message_id=int(row["announcement_message_id"])
            if row["announcement_message_id"] is not None
            else None,
            tier_channel_id=int(row["tier_channel_id"]),
            tier_anchor_message_id=int(row["tier_anchor_message_id"])
            if row["tier_anchor_message_id"] is not None
            else None,
            admin_channel_id=int(row["admin_channel_id"]),
            mode=str(row["mode"]) if row["mode"] is not None else None,
            participant_limit=int(row["participant_limit"]),
            status=MatchStatus(row["status"]),
            starts_at=_from_db(row["starts_at_utc"]),  # type: ignore[arg-type]
            tier_deadline_at=_from_db(row["tier_deadline_at_utc"]),  # type: ignore[arg-type]
            lobby_at=_from_db(row["lobby_at_utc"]),  # type: ignore[arg-type]
            full_reached_at=_from_db(row["full_reached_at_utc"]),
            recruitment_completed_notified_at=_from_db(
                row["recruitment_completed_notified_at_utc"]
            ),
            tier_complete_notified_at=_from_db(row["tier_complete_notified_at_utc"]),
            tier_missing_reminder_notified_at=_from_db(
                row["tier_missing_reminder_notified_at_utc"]
            ),
            lobby_notified_at=_from_db(row["lobby_notified_at_utc"]),
            last_missing_tier_reminder_at=_from_db(
                row["last_missing_tier_reminder_at_utc"]
            ),
            next_arrival_seq=int(row["next_arrival_seq"]),
            created_at=_from_db(row["created_at_utc"]),  # type: ignore[arg-type]
            updated_at=_from_db(row["updated_at_utc"]),  # type: ignore[arg-type]
            source_request_id=str(row["source_request_id"])
            if row["source_request_id"] is not None
            else None,
            source_request_type=str(row["source_request_type"])
            if row["source_request_type"] is not None
            else None,
        )

    @staticmethod
    def _roster_from_row(row: aiosqlite.Row) -> RosterEntry:
        return RosterEntry(
            match_id=str(row["match_id"]),
            discord_user_id=int(row["discord_user_id"]),
            discord_display_name=str(row["discord_display_name"]),
            reaction_order=int(row["reaction_order"]),
            status=RosterStatus(row["status"]),
            reacted_at=_from_db(row["reacted_at_utc"]),  # type: ignore[arg-type]
            removed_at=_from_db(row["removed_at_utc"]),
            waitlist_reason=WaitlistReason(row["waitlist_reason"])
            if row["waitlist_reason"] is not None
            else None,
            conflict_match_id=str(row["conflict_match_id"])
            if row["conflict_match_id"] is not None
            else None,
        )

    @staticmethod
    def _notification_from_row(
        row: aiosqlite.Row, *, attempts_delta: int = 0
    ) -> NotificationRecord:
        return NotificationRecord(
            id=int(row["id"]),
            match_id=str(row["match_id"]),
            kind=NotificationKind(row["kind"]),
            channel_id=int(row["channel_id"]),
            payload=json.loads(row["payload_json"]),
            dedupe_key=str(row["dedupe_key"]),
            status=NotificationStatus.SENDING
            if attempts_delta
            else NotificationStatus(row["status"]),
            attempts=int(row["attempts"]) + attempts_delta,
            next_attempt_at=_from_db(row["next_attempt_at_utc"]),  # type: ignore[arg-type]
        )
