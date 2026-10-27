"""저장 큐 배치를 하나의 트랜잭션으로 적용한다. 반응과 알림 예약을 함께 커밋한다."""

from __future__ import annotations

import logging
from typing import (
    Sequence,
)

from src.domain.models import (
    NotificationKind,
    PersistenceMutation,
    ReactionMutation,
    RosterStatus,
    SubstituteRecruitmentStatus,
    TierDeleteMutation,
    TierUpsertMutation,
)

from .base import (
    ACTIVE_STATUSES,
    SQLiteStore,
    _to_db,
)

logger = logging.getLogger(__name__)


class MutationStorage(SQLiteStore):
    """저장 큐 배치를 하나의 트랜잭션으로 적용한다. 반응과 알림 예약을 함께 커밋한다."""

    async def apply_mutations(self, mutations: Sequence[PersistenceMutation]) -> None:
        if not mutations:
            return
        async with self._write_lock:
            await self.connection.execute("BEGIN IMMEDIATE")
            try:
                match_ids = tuple(
                    dict.fromkeys(mutation.match_id for mutation in mutations)
                )
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
                        if await self._is_tier_activity_allowed(
                            session,
                            mutation.discord_user_id,
                            mutation.activity_at,
                        ):
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
                channel_id=await self._match_channel(
                    mutation.match_id, "announcement_channel_id"
                ),
                payload={"user_ids": list(mutation.completion_user_ids)},
                dedupe_key=f"match:{mutation.match_id}:recruitment-complete",
                now=mutation.received_at,
            )
        if (
            mutation.substitute_recruitment_message_id is not None
            and mutation.substitute_recruited_user_id is not None
        ):
            cursor = await self.connection.execute(
                """
                UPDATE substitute_recruitments
                SET status = ?, recruited_user_id = ?, filled_at_utc = ?
                WHERE discord_message_id = ? AND match_id = ? AND status = ?
                """,
                (
                    SubstituteRecruitmentStatus.FILLED.value,
                    mutation.substitute_recruited_user_id,
                    _to_db(mutation.received_at),
                    mutation.substitute_recruitment_message_id,
                    mutation.match_id,
                    SubstituteRecruitmentStatus.OPEN.value,
                ),
            )
            if cursor.rowcount:
                await self._insert_notification(
                    match_id=mutation.match_id,
                    kind=NotificationKind.SUBSTITUTE_RECRUITED,
                    channel_id=await self._match_channel(
                        mutation.match_id,
                        "announcement_channel_id",
                    ),
                    payload={
                        "user_ids": [mutation.substitute_recruited_user_id],
                        "substitute_recruitment_message_id": (
                            mutation.substitute_recruitment_message_id
                        ),
                    },
                    dedupe_key=(
                        f"match:{mutation.match_id}:substitute-recruited:"
                        f"{mutation.substitute_recruitment_message_id}"
                    ),
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
