from __future__ import annotations

import asyncio
from dataclasses import replace

from owkr_gather_bot.domain.models import (
    MatchSession,
    MatchStatus,
    ReactionAction,
    ReactionEvent,
    ReactionMutation,
    RosterEntry,
    RosterStatus,
)
from owkr_gather_bot.infrastructure.persistence_writer import PersistenceWriter


class _Stop:
    pass


class SessionActor:
    def __init__(
        self,
        session: MatchSession,
        roster: list[RosterEntry],
        writer: PersistenceWriter,
    ) -> None:
        self.session = session
        self._entries = {entry.discord_user_id: entry for entry in roster}
        self._writer = writer
        self._queue: asyncio.Queue[ReactionEvent | _Stop] = asyncio.Queue(maxsize=2000)
        self._task: asyncio.Task[None] | None = None
        self._accepting = True

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

    async def _run(self) -> None:
        while True:
            item = await self._queue.get()
            try:
                if isinstance(item, _Stop):
                    return
                self._process(item)
            finally:
                self._queue.task_done()

    def _process(self, event: ReactionEvent) -> None:
        if event.received_at >= self.session.starts_at:
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
                roster_status = RosterStatus.CONFIRMED if can_confirm else RosterStatus.WAITLISTED
                roster_entry = RosterEntry(
                    match_id=self.session.id,
                    discord_user_id=event.discord_user_id,
                    discord_display_name=event.discord_display_name,
                    reaction_order=arrival_seq,
                    status=roster_status,
                    reacted_at=event.received_at,
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
        else:
            if current is None or not current.is_active:
                outcome = "DUPLICATE_REMOVE"
            else:
                roster_entry = replace(
                    current,
                    status=RosterStatus.WITHDRAWN,
                    removed_at=event.received_at,
                )
                self._entries[event.discord_user_id] = roster_entry
                outcome = "WITHDRAWN"

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
        )
        self._writer.submit(mutation)

