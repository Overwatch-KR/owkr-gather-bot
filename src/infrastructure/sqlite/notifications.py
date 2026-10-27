"""알림 예약·claim·전송 결과 SQL. 과거 알림 종류와 마이그레이션 호환을 유지한다."""

from __future__ import annotations

import json
import logging
from datetime import (
    datetime,
    timedelta,
)
from typing import (
    Any,
)

from src.domain.models import (
    NotificationKind,
    NotificationRecord,
    NotificationStatus,
    RosterStatus,
    TierParticipantStatus,
)

from .base import ACTIVE_STATUSES, SQLiteStore, _from_db, _to_db

logger = logging.getLogger(__name__)


class NotificationStorage(SQLiteStore):
    """알림 예약·claim·전송 결과 SQL. 과거 알림 종류와 마이그레이션 호환을 유지한다."""

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

    async def enqueue_tier_complete_if_ready(
        self, match_id: str, now: datetime
    ) -> None:
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

    async def claim_notifications(
        self, now: datetime, limit: int = 10
    ) -> list[NotificationRecord]:
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
                return [
                    self._notification_from_row(row, attempts_delta=1) for row in rows
                ]
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
                    NotificationKind.RECRUITMENT_COMPLETE: "recruitment_completed_notified_at_utc",
                    NotificationKind.TIER_MISSING_REMINDER: "tier_missing_reminder_notified_at_utc",
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
                        raise ValueError(
                            "tier anchor notification requires a message ID"
                        )
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
                    if (
                        session is not None
                        and session["tier_anchor_message_id"] is None
                    ):
                        await self._insert_notification(
                            match_id=notification.match_id,
                            kind=NotificationKind.TIER_ANCHOR,
                            channel_id=int(session["tier_channel_id"]),
                            payload={"recreated": False},
                            dedupe_key=f"match:{notification.match_id}:tier-anchor:initial",
                            now=sent_at,
                        )
                elif notification.kind is NotificationKind.TIER_ANCHOR and bool(
                    notification.payload.get("recreated")
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
        self,
        notification: NotificationRecord,
        error: str,
        now: datetime,
        max_attempts: int,
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
        if column not in {
            "announcement_channel_id",
            "tier_channel_id",
            "admin_channel_id",
        }:
            raise ValueError("unsupported channel column")
        row = await self._fetchone(
            f"SELECT {column} FROM match_sessions WHERE id = ?", (match_id,)
        )
        if row is None:
            raise LookupError(match_id)
        return int(row[column])
