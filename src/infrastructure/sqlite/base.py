"""하나의 SQLite 연결·쓰기 잠금·마이그레이션·행 변환을 공유한다."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import (
    datetime,
    timezone,
)
from pathlib import Path
from typing import (
    Any,
)

import aiosqlite

from src.domain.match_code import deterministic_match_code
from src.domain.models import (
    MatchSession,
    MatchStatus,
    NotificationKind,
    NotificationRecord,
    NotificationStatus,
    RosterEntry,
    RosterStatus,
    SubstituteRecruitment,
    SubstituteRecruitmentStatus,
    WaitlistReason,
)

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


class SQLiteStore:
    """하나의 SQLite 연결·쓰기 잠금·마이그레이션·행 변환을 공유한다."""

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
        """기존 DB에 마이그레이션을 순서대로 적용한다. 과거 스키마 복구도 유지한다."""
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
        """과거 DB의 알림 CHECK 제약을 복구한다. 현재 미작성 알림을 다시 켜는 기능은 아니다."""
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
            lobby_voice_channel_id=(
                int(row["lobby_voice_channel_id"])
                if row["lobby_voice_channel_id"] is not None
                else None
            ),
            lobby_name=str(row["lobby_name"])
            if row["lobby_name"] is not None
            else None,
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
    def _substitute_recruitment_from_row(
        row: aiosqlite.Row,
    ) -> SubstituteRecruitment:
        return SubstituteRecruitment(
            match_id=str(row["match_id"]),
            discord_message_id=int(row["discord_message_id"]),
            status=SubstituteRecruitmentStatus(row["status"]),
            recruited_user_id=(
                int(row["recruited_user_id"])
                if row["recruited_user_id"] is not None
                else None
            ),
            created_at=_from_db(row["created_at_utc"]),  # type: ignore[arg-type]
            filled_at=_from_db(row["filled_at_utc"]),
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
