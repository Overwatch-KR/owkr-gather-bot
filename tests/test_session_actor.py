from __future__ import annotations

import asyncio
import unittest
from datetime import timedelta

from owkr_gather_bot.application.session_actor import SessionActor
from owkr_gather_bot.domain.models import ReactionAction, ReactionEvent, RosterStatus

from tests.helpers import RecordingWriter, make_session


class SessionActorTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.session = make_session()
        self.writer = RecordingWriter()
        self.actor = SessionActor(self.session, [], self.writer)  # type: ignore[arg-type]
        self.actor.start()

    async def asyncTearDown(self) -> None:
        await self.actor.stop(discard=True)

    def event(self, user_id: int, action: ReactionAction, offset_ms: int = 0) -> ReactionEvent:
        return ReactionEvent(
            match_id=self.session.id,
            discord_user_id=user_id,
            discord_display_name=f"user-{user_id}",
            action=action,
            received_at=self.session.created_at + timedelta(milliseconds=offset_ms),
        )

    async def test_eighteen_concurrent_adds_are_split_without_loss(self) -> None:
        async def submit(user_id: int) -> None:
            await asyncio.sleep(0)
            self.assertTrue(self.actor.ingest(self.event(user_id, ReactionAction.ADD, user_id)))

        await asyncio.gather(*(submit(user_id) for user_id in range(1, 19)))
        await self.actor.drain()

        confirmed = self.actor.current_confirmed()
        waitlist = self.actor.current_waitlist()
        self.assertEqual([entry.discord_user_id for entry in confirmed], list(range(1, 11)))
        self.assertEqual([entry.discord_user_id for entry in waitlist], list(range(11, 19)))
        self.assertEqual([entry.reaction_order for entry in confirmed + waitlist], list(range(1, 19)))

    async def test_completion_is_emitted_once(self) -> None:
        for user_id in range(1, 13):
            self.actor.ingest(self.event(user_id, ReactionAction.ADD, user_id))
        await self.actor.drain()
        completions = [
            mutation for mutation in self.writer.mutations if getattr(mutation, "completion_user_ids", ())
        ]
        self.assertEqual(len(completions), 1)
        self.assertEqual(completions[0].completion_user_ids, tuple(range(1, 11)))

    async def test_duplicate_add_and_remove_then_readd_gets_new_order(self) -> None:
        self.actor.ingest(self.event(1, ReactionAction.ADD, 1))
        self.actor.ingest(self.event(1, ReactionAction.ADD, 2))
        self.actor.ingest(self.event(1, ReactionAction.REMOVE, 3))
        self.actor.ingest(self.event(1, ReactionAction.ADD, 4))
        await self.actor.drain()
        entry = self.actor.current_confirmed()[0]
        self.assertEqual(entry.reaction_order, 4)
        self.assertEqual(
            [getattr(mutation, "outcome", "") for mutation in self.writer.mutations],
            ["CONFIRMED", "DUPLICATE_ADD", "WITHDRAWN", "CONFIRMED"],
        )

    async def test_active_user_count_returns_to_zero_after_last_remove(self) -> None:
        self.actor.ingest(self.event(1, ReactionAction.ADD, 1))
        await self.actor.drain()
        self.assertEqual(self.actor.active_user_count(), 1)

        self.actor.ingest(self.event(1, ReactionAction.REMOVE, 2))
        await self.actor.drain()
        self.assertEqual(self.actor.active_user_count(), 0)

    async def test_confirmed_removal_after_full_does_not_promote_waitlist(self) -> None:
        for user_id in range(1, 12):
            self.actor.ingest(self.event(user_id, ReactionAction.ADD, user_id))
        await self.actor.drain()
        self.actor.ingest(self.event(1, ReactionAction.REMOVE, 20))
        await self.actor.drain()

        self.assertEqual(len(self.actor.current_confirmed()), 9)
        self.assertEqual(len(self.actor.current_waitlist()), 1)
        self.assertEqual(self.actor.current_waitlist()[0].discord_user_id, 11)
        self.assertEqual(self.actor.current_waitlist()[0].status, RosterStatus.WAITLISTED)

    async def test_readd_after_full_is_waitlisted(self) -> None:
        for user_id in range(1, 11):
            self.actor.ingest(self.event(user_id, ReactionAction.ADD, user_id))
        await self.actor.drain()
        self.actor.ingest(self.event(1, ReactionAction.REMOVE, 20))
        self.actor.ingest(self.event(1, ReactionAction.ADD, 21))
        await self.actor.drain()
        self.assertEqual(self.actor.current_waitlist()[0].discord_user_id, 1)
        self.assertEqual(self.actor.current_waitlist()[0].reaction_order, 12)

    async def test_events_at_or_after_start_are_ignored(self) -> None:
        event = ReactionEvent(
            match_id=self.session.id,
            discord_user_id=1,
            discord_display_name="user-1",
            action=ReactionAction.ADD,
            received_at=self.session.starts_at,
        )
        self.assertFalse(self.actor.ingest(event))
        await self.actor.drain()
        self.assertEqual(self.writer.mutations, [])
