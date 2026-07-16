from __future__ import annotations

import unittest
from datetime import timedelta

from owkr_gather_bot.application.tier_collector import TierCollector
from owkr_gather_bot.application.session_actor import SessionActor
from owkr_gather_bot.domain.models import RosterEntry, RosterStatus, TierDeleteMutation, TierUpsertMutation

from tests.helpers import MutableClock, RecordingWriter, make_session


class FakeCoordinator:
    def __init__(self, actor: SessionActor) -> None:
        self.active_actor = actor


class TierCollectorTest(unittest.TestCase):
    def setUp(self) -> None:
        now = make_session().created_at
        self.session = make_session(completion_notified_at=now + timedelta(minutes=5))
        roster = [
            RosterEntry(
                match_id=self.session.id,
                discord_user_id=1,
                discord_display_name="confirmed",
                reaction_order=1,
                status=RosterStatus.CONFIRMED,
                reacted_at=now,
            ),
            RosterEntry(
                match_id=self.session.id,
                discord_user_id=11,
                discord_display_name="waitlisted",
                reaction_order=11,
                status=RosterStatus.WAITLISTED,
                reacted_at=now,
            ),
        ]
        self.writer = RecordingWriter()
        self.actor = SessionActor(self.session, roster, self.writer)  # type: ignore[arg-type]
        self.clock = MutableClock(now + timedelta(minutes=10))
        self.collector = TierCollector(
            FakeCoordinator(self.actor),  # type: ignore[arg-type]
            self.writer,  # type: ignore[arg-type]
            self.clock,
        )

    def submit(self, user_id: int, activity_at, message_id: int = 500) -> bool:
        return self.collector.submit_activity(
            guild_id=self.session.guild_id,
            channel_id=self.session.tier_channel_id,
            discord_user_id=user_id,
            discord_display_name=f"user-{user_id}",
            raw_content="battle#1234\n마4 / 마4 / 마4",
            discord_message_id=message_id,
            activity_at=activity_at,
        )

    def test_before_completion_is_not_accepted(self) -> None:
        self.assertFalse(self.submit(1, self.session.recruitment_completed_notified_at - timedelta(microseconds=1)))
        self.assertEqual(self.writer.mutations, [])

    def test_completion_lower_bound_is_inclusive(self) -> None:
        self.assertTrue(self.submit(1, self.session.recruitment_completed_notified_at))
        self.assertIsInstance(self.writer.mutations[-1], TierUpsertMutation)

    def test_deadline_upper_bound_is_exclusive(self) -> None:
        self.assertTrue(self.submit(1, self.session.tier_deadline_at - timedelta(microseconds=1)))
        self.assertFalse(self.submit(1, self.session.tier_deadline_at, message_id=501))

    def test_waitlisted_tier_is_stored_without_any_notification(self) -> None:
        self.assertTrue(self.submit(11, self.session.recruitment_completed_notified_at))
        mutation = self.writer.mutations[-1]
        self.assertIsInstance(mutation, TierUpsertMutation)
        self.assertEqual(mutation.discord_user_id, 11)

    def test_delete_is_queued_before_start(self) -> None:
        accepted = self.collector.submit_delete(
            guild_id=self.session.guild_id,
            channel_id=self.session.tier_channel_id,
            discord_message_id=500,
            received_at=self.session.tier_deadline_at,
        )
        self.assertTrue(accepted)
        self.assertIsInstance(self.writer.mutations[-1], TierDeleteMutation)

    def test_tier_activity_after_start_is_ignored(self) -> None:
        self.assertFalse(self.submit(1, self.session.starts_at))
        self.assertFalse(
            self.collector.submit_delete(
                guild_id=self.session.guild_id,
                channel_id=self.session.tier_channel_id,
                discord_message_id=500,
                received_at=self.session.starts_at,
            )
        )
