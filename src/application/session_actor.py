from __future__ import annotations

import asyncio
import logging
from dataclasses import replace
from typing import Awaitable, Callable

from src.domain.models import (
    MatchSession,
    MatchStatus,
    ReactionAction,
    ReactionEvent,
    ReactionMutation,
    RosterEntry,
    RosterStatus,
    WaitlistReason,
)
from src.infrastructure.persistence_writer import PersistenceWriter


logger = logging.getLogger(__name__)


class _Stop:
    pass


class SessionActor:
    def __init__(
        self,
        session: MatchSession,
        roster: list[RosterEntry],
        writer: PersistenceWriter,
        reserve_confirmation: (
            Callable[[MatchSession, int], Awaitable[str | None]] | None
        ) = None,
        release_confirmation: (
            Callable[[MatchSession, int], Awaitable[None]] | None
        ) = None,
    ) -> None:
        self.session = session
        self._entries = {entry.discord_user_id: entry for entry in roster}
        self._writer = writer
        self._reserve_confirmation = (
            reserve_confirmation or self._allow_confirmation
        )
        self._release_confirmation = (
            release_confirmation or self._ignore_confirmation_release
        )
        self._queue: asyncio.Queue[ReactionEvent | _Stop] = asyncio.Queue(maxsize=2000)
        self._task: asyncio.Task[None] | None = None
        self._accepting = True
        self._substitute_recruitments: dict[int, int | None] = {}

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name=f"session-actor:{self.session.id}")

    def ingest(self, event: ReactionEvent) -> bool:
        if (
            not self._accepting
            or event.match_id != self.session.id
            or event.received_at >= self.session.starts_at
        ):
            return False
        self._queue.put_nowait(event)
        return True

    async def drain(self) -> None:
        await self._queue.join()

    async def stop(self, *, discard: bool = False) -> None:
        self._accepting = False
        if self._task is None:
            return
        if discard:
            while True:
                try:
                    self._queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
                else:
                    self._queue.task_done()
        else:
            await self.drain()
        await self._queue.put(_Stop())
        await self._task
        self._task = None

    def current_confirmed(self) -> list[RosterEntry]:
        return sorted(
            (entry for entry in self._entries.values() if entry.status is RosterStatus.CONFIRMED),
            key=lambda entry: entry.reaction_order,
        )

    def current_waitlist(self) -> list[RosterEntry]:
        return sorted(
            (entry for entry in self._entries.values() if entry.status is RosterStatus.WAITLISTED),
            key=lambda entry: entry.reaction_order,
        )

    def has_active_user(self, user_id: int) -> bool:
        entry = self._entries.get(user_id)
        return entry is not None and entry.is_active

    def active_user_count(self) -> int:
        return sum(1 for entry in self._entries.values() if entry.is_active)

    def register_substitute_recruitment(
        self,
        discord_message_id: int,
        recruited_user_id: int | None = None,
    ) -> None:
        self._substitute_recruitments[discord_message_id] = recruited_user_id

    def unregister_substitute_recruitment(self, discord_message_id: int) -> None:
        self._substitute_recruitments.pop(discord_message_id, None)

    def is_substitute_recruitment_filled(self, discord_message_id: int) -> bool:
        return self._substitute_recruitments.get(discord_message_id) is not None

    def display_name_for(self, user_id: int) -> str | None:
        entry = self._entries.get(user_id)
        return entry.discord_display_name if entry else None

    def mark_notification_sent(self, kind: str, sent_at) -> None:
        if kind == "RECRUITMENT_COMPLETE":
            self.session.recruitment_completed_notified_at = sent_at
        elif kind == "LOBBY_REMINDER":
            self.session.lobby_notified_at = sent_at
        elif kind == "TIER_COMPLETE":
            self.session.tier_complete_notified_at = sent_at
        elif kind == "TIER_MISSING_REMINDER":
            self.session.tier_missing_reminder_notified_at = sent_at

    async def _run(self) -> None:
        while True:
            item = await self._queue.get()
            try:
                if isinstance(item, _Stop):
                    return
                await self._process(item)
            except Exception:
                logger.exception(
                    "session actor event failed match_id=%s",
                    self.session.id,
                )
            finally:
                self._queue.task_done()

    async def _process(self, event: ReactionEvent) -> None:
        if event.received_at >= self.session.starts_at:
            return

        substitute_message_id = event.substitute_recruitment_message_id
        if substitute_message_id is not None:
            if (
                self.session.full_reached_at is None
                or substitute_message_id not in self._substitute_recruitments
            ):
                return
            recruited_user_id = self._substitute_recruitments[
                substitute_message_id
            ]
            if recruited_user_id is not None:
                if (
                    event.action is ReactionAction.ADD
                    or event.discord_user_id != recruited_user_id
                ):
                    return
            else:
                current = self._entries.get(event.discord_user_id)
                if (
                    event.action is ReactionAction.REMOVE
                    or (current is not None and current.is_active)
                ):
                    return

        arrival_seq = self.session.next_arrival_seq
        self.session.next_arrival_seq += 1
        current = self._entries.get(event.discord_user_id)
        roster_entry: RosterEntry | None = None
        completion_user_ids: tuple[int, ...] = ()
        outcome: str

        if event.action is ReactionAction.ADD:
            if current is not None and current.is_active:
                outcome = "DUPLICATE_ADD"
            else:
                can_confirm = (
                    self.session.full_reached_at is None
                    and len(self.current_confirmed()) < self.session.participant_limit
                )
                conflict_match_id: str | None = None
                if can_confirm:
                    conflict_match_id = await self._reserve_confirmation(
                        self.session,
                        event.discord_user_id,
                    )
                roster_status = (
                    RosterStatus.CONFIRMED
                    if can_confirm and conflict_match_id is None
                    else RosterStatus.WAITLISTED
                )
                waitlist_reason = None
                if roster_status is RosterStatus.WAITLISTED:
                    waitlist_reason = (
                        WaitlistReason.SCHEDULE_CONFLICT
                        if conflict_match_id is not None
                        else WaitlistReason.CAPACITY
                    )
                roster_entry = RosterEntry(
                    match_id=self.session.id,
                    discord_user_id=event.discord_user_id,
                    discord_display_name=event.discord_display_name,
                    reaction_order=arrival_seq,
                    status=roster_status,
                    reacted_at=event.received_at,
                    waitlist_reason=waitlist_reason,
                    conflict_match_id=conflict_match_id,
                )
                self._entries[event.discord_user_id] = roster_entry
                outcome = roster_status.value

                if (
                    roster_status is RosterStatus.CONFIRMED
                    and self.session.full_reached_at is None
                    and len(self.current_confirmed()) == self.session.participant_limit
                ):
                    self.session.full_reached_at = event.received_at
                    self.session.status = MatchStatus.FULL
                    completion_user_ids = tuple(
                        entry.discord_user_id for entry in self.current_confirmed()
                    )
                    logger.info(
                        "recruitment reached participant limit match_id=%s participant_count=%s",
                        self.session.id,
                        len(completion_user_ids),
                    )
        else:
            if current is None or not current.is_active:
                outcome = "DUPLICATE_REMOVE"
            else:
                if current.status is RosterStatus.CONFIRMED:
                    await self._release_confirmation(
                        self.session,
                        event.discord_user_id,
                    )
                roster_entry = replace(
                    current,
                    status=RosterStatus.WITHDRAWN,
                    removed_at=event.received_at,
                    waitlist_reason=None,
                    conflict_match_id=None,
                )
                self._entries[event.discord_user_id] = roster_entry
                outcome = "WITHDRAWN"

        substitute_recruited_user_id: int | None = None
        if (
            substitute_message_id is not None
            and event.action is ReactionAction.ADD
            and roster_entry is not None
            and roster_entry.status is RosterStatus.WAITLISTED
        ):
            self._substitute_recruitments[substitute_message_id] = (
                event.discord_user_id
            )
            substitute_recruited_user_id = event.discord_user_id

        self.session.updated_at = event.received_at
        mutation = ReactionMutation(
            match_id=self.session.id,
            discord_user_id=event.discord_user_id,
            action=event.action,
            received_at=event.received_at,
            arrival_seq=arrival_seq,
            outcome=outcome,
            next_arrival_seq=self.session.next_arrival_seq,
            match_status=self.session.status,
            roster_entry=roster_entry,
            full_reached_at=self.session.full_reached_at,
            completion_user_ids=completion_user_ids,
            substitute_recruitment_message_id=(
                substitute_message_id
                if substitute_recruited_user_id is not None
                else None
            ),
            substitute_recruited_user_id=substitute_recruited_user_id,
        )
        self._writer.submit(mutation)
        logger.debug(
            "reaction processed match_id=%s user_id=%s action=%s arrival_seq=%s outcome=%s",
            self.session.id,
            event.discord_user_id,
            event.action.value,
            arrival_seq,
            outcome,
        )

    @staticmethod
    async def _allow_confirmation(
        session: MatchSession,
        discord_user_id: int,
    ) -> str | None:
        return None

    @staticmethod
    async def _ignore_confirmation_release(
        session: MatchSession,
        discord_user_id: int,
    ) -> None:
        return None
