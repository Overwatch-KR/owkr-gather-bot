"""종료 내전 정리·경고·개인정보 삭제 SQL. 기존 삭제와 복원 차단 순서를 유지한다."""

from __future__ import annotations

import logging
from datetime import (
    datetime,
)

from src.domain.warnings import WarningReason, WarningSummary

from .base import (
    SQLiteStore,
    _to_db,
)

logger = logging.getLogger(__name__)


class SafetyStorage(SQLiteStore):
    """종료 내전 정리·경고·개인정보 삭제 SQL. 기존 삭제와 복원 차단 순서를 유지한다."""

    async def purge_closed_matches(self, guild_id: int) -> int:
        # 종료 상태만 지운다. 진행 중인 Actor와 writer 변경의 대상은 보존한다.
        async with self._write_lock:
            await self.connection.execute("BEGIN IMMEDIATE")
            try:
                await self.connection.execute(
                    """UPDATE roster_entries SET conflict_match_id = NULL
                    WHERE conflict_match_id IN (
                        SELECT id FROM match_sessions
                        WHERE guild_id = ? AND status IN ('STARTED', 'CANCELED')
                    )""",
                    (guild_id,),
                )
                cursor = await self.connection.execute(
                    "DELETE FROM match_sessions WHERE guild_id = ? "
                    "AND status IN ('STARTED', 'CANCELED')",
                    (guild_id,),
                )
                await self.connection.commit()
                return cursor.rowcount
            except BaseException:
                await self.connection.rollback()
                raise

    async def warning_summary(self, guild_id: int, user_id: int) -> WarningSummary:
        rows = await self._fetchall(
            "SELECT reason, COUNT(*) AS count FROM warnings "
            "WHERE guild_id = ? AND user_id = ? AND revoked_at_utc IS NULL GROUP BY reason",
            (guild_id, user_id),
        )
        counts = {str(row["reason"]): int(row["count"]) for row in rows}
        return WarningSummary(counts.get("노쇼", 0), counts.get("비매너", 0))

    async def add_warning(
        self,
        guild_id: int,
        user_id: int,
        reason: WarningReason,
        manager_id: int,
        request_id: int,
        now: datetime,
    ) -> None:
        reason = WarningReason(reason)
        await self._execute_write(
            "INSERT INTO warnings (guild_id, user_id, reason, manager_user_id, "
            "request_id, created_at_utc) VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(request_id) DO NOTHING",
            (guild_id, user_id, reason.value, manager_id, request_id, _to_db(now)),
        )

    async def latest_warning(
        self, guild_id: int, user_id: int, reason: WarningReason
    ) -> int | None:
        row = await self._fetchone(
            "SELECT id FROM warnings WHERE guild_id = ? AND user_id = ? "
            "AND reason = ? AND revoked_at_utc IS NULL ORDER BY id DESC LIMIT 1",
            (guild_id, user_id, WarningReason(reason).value),
        )
        return int(row["id"]) if row else None

    async def revoke_warning(
        self, guild_id: int, user_id: int, warning_id: int, now: datetime
    ) -> bool:
        async with self._write_lock:
            cursor = await self.connection.execute(
                "UPDATE warnings SET revoked_at_utc = ? WHERE id = ? AND guild_id = ? "
                "AND user_id = ? AND revoked_at_utc IS NULL",
                (_to_db(now), warning_id, guild_id, user_id),
            )
            await self.connection.commit()
            return cursor.rowcount == 1

    async def warning_user_ids(self, guild_id: int) -> list[int]:
        rows = await self._fetchall(
            "SELECT DISTINCT user_id FROM warnings WHERE guild_id = ? ORDER BY user_id",
            (guild_id,),
        )
        return [int(row["user_id"]) for row in rows]

    async def delete_user_warnings(self, guild_id: int, user_id: int) -> None:
        await self._execute_write(
            "DELETE FROM warnings WHERE guild_id = ? AND user_id = ?",
            (guild_id, user_id),
        )

    async def erase_user_tiers(self, guild_id: int, user_id: int) -> None:
        async with self._write_lock:
            await self.connection.execute("BEGIN IMMEDIATE")
            try:
                # 재시작 보완 수집과 진행 중인 제출이 원문을 되살리지 않도록
                # 수집 중단 기록을 먼저 남긴 뒤 원문과 연결을 함께 지운다.
                await self.connection.execute(
                    """INSERT OR IGNORE INTO tier_collection_opt_outs (match_id, user_id)
                    SELECT m.id, ? FROM match_sessions m JOIN roster_entries r ON r.match_id = m.id
                    WHERE m.guild_id = ? AND r.discord_user_id = ?
                    AND m.status IN ('CREATED', 'RECRUITING', 'FULL')""",
                    (user_id, guild_id, user_id),
                )
                for table in ("tier_submissions", "tier_message_bindings"):
                    await self.connection.execute(
                        f"DELETE FROM {table} WHERE discord_user_id = ? AND match_id IN "
                        "(SELECT id FROM match_sessions WHERE guild_id = ?)",
                        (user_id, guild_id),
                    )
                await self.connection.commit()
            except BaseException:
                await self.connection.rollback()
                raise

    async def purge_previous_membership(
        self, guild_id: int, user_id: int, joined_at: datetime
    ) -> None:
        await self._execute_write(
            "DELETE FROM warnings WHERE guild_id = ? AND user_id = ? AND created_at_utc < ?",
            (guild_id, user_id, _to_db(joined_at)),
        )
