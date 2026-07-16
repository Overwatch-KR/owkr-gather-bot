from __future__ import annotations

import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

from src.application.tier_collector import TierCollector, TierRouteStatus
from src.domain.models import (
    MatchStatus,
    ReactionAction,
    ReactionMutation,
    RosterEntry,
    RosterStatus,
)
from src.infrastructure.sqlite_repository import SQLiteMatchRepository

from tests.helpers import MutableClock, make_session


class TierCollectorTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.repository = SQLiteMatchRepository(
            Path(self.tempdir.name) / "test.sqlite3"
        )
        await self.repository.initialize(
            Path(__file__).resolve().parents[1] / "migrations" / "001_initial.sql"
        )
        now = make_session().created_at
        self.clock = MutableClock(now + timedelta(minutes=10))
        self.collector = TierCollector(self.repository, self.clock)
        self.session = make_session(
            match_id="match-a",
            match_code="A7K2",
            status=MatchStatus.FULL,
            completion_notified_at=now + timedelta(minutes=5),
            full_reached_at=now + timedelta(minutes=4),
        )
        self.session.tier_anchor_message_id = 700
        await self.repository.create_match(self.session)
        await self.add_roster(self.session.id, 1, RosterStatus.CONFIRMED, 1)
        await self.add_roster(self.session.id, 11, RosterStatus.WAITLISTED, 11)

    async def asyncTearDown(self) -> None:
        await self.repository.close()
        self.tempdir.cleanup()

    async def add_roster(
        self,
        match_id: str,
        user_id: int,
        status: RosterStatus,
        order: int,
    ) -> None:
        await self.repository.apply_mutations(
            [
                ReactionMutation(
                    match_id=match_id,
                    discord_user_id=user_id,
                    action=ReactionAction.ADD,
                    received_at=self.clock.now(),
                    arrival_seq=order,
                    outcome=status.value,
                    next_arrival_seq=order + 1,
                    match_status=MatchStatus.FULL,
                    roster_entry=RosterEntry(
                        match_id=match_id,
                        discord_user_id=user_id,
                        discord_display_name=f"user-{user_id}",
                        reaction_order=order,
                        status=status,
                        reacted_at=self.clock.now(),
                    ),
                )
            ]
        )

    async def submit(
        self,
        user_id: int,
        activity_at,
        *,
        message_id: int = 500,
        content: str = "battle#1234\n마4 / 마4 / 마4",
        reply_to_message_id: int | None = None,
    ):
        return await self.collector.submit_activity(
            guild_id=self.session.guild_id,
            channel_id=self.session.tier_channel_id,
            discord_user_id=user_id,
            discord_display_name=f"user-{user_id}",
            raw_content=content,
            discord_message_id=message_id,
            activity_at=activity_at,
            reply_to_message_id=reply_to_message_id,
        )

    async def test_before_completion_is_not_accepted(self) -> None:
        result = await self.submit(
            1,
            self.session.recruitment_completed_notified_at
            - timedelta(microseconds=1),
            reply_to_message_id=700,
        )
        self.assertEqual(result.status, TierRouteStatus.CLOSED)
        self.assertIsNone(await self.repository.get_tier_submission(self.session.id, 1))

    async def test_anchor_reply_routes_to_exact_session(self) -> None:
        result = await self.submit(
            1,
            self.session.recruitment_completed_notified_at,
            reply_to_message_id=700,
        )
        self.assertEqual(result.status, TierRouteStatus.ACCEPTED)
        submission = await self.repository.get_tier_submission(self.session.id, 1)
        self.assertEqual(submission.discord_message_id, 500)

    async def test_single_candidate_general_message_is_auto_routed(self) -> None:
        result = await self.submit(
            1,
            self.session.recruitment_completed_notified_at,
        )
        self.assertEqual(result.status, TierRouteStatus.ACCEPTED)
        self.assertEqual(result.session.id, self.session.id)

    async def test_general_message_requires_weak_tier_shape(self) -> None:
        result = await self.submit(
            1,
            self.session.recruitment_completed_notified_at,
            content="그냥 대화입니다",
        )
        self.assertEqual(result.status, TierRouteStatus.INVALID_FORMAT)

    async def test_multiple_candidates_are_not_stored(self) -> None:
        second = make_session(
            match_id="match-b",
            match_code="M4Q8",
            now=self.session.created_at,
            status=MatchStatus.FULL,
            completion_notified_at=self.session.recruitment_completed_notified_at,
            full_reached_at=self.session.full_reached_at,
        )
        second.announcement_message_id = 1001
        second.tier_anchor_message_id = 701
        await self.repository.create_match(second)
        await self.add_roster(second.id, 1, RosterStatus.WAITLISTED, 1)

        result = await self.submit(
            1,
            self.session.recruitment_completed_notified_at,
        )

        self.assertEqual(result.status, TierRouteStatus.AMBIGUOUS)
        self.assertEqual({item.id for item in result.candidates}, {"match-a", "match-b"})
        self.assertIsNone(await self.repository.get_tier_binding(500))

    async def test_non_participant_anchor_reply_is_rejected(self) -> None:
        result = await self.submit(
            99,
            self.session.recruitment_completed_notified_at,
            reply_to_message_id=700,
        )
        self.assertEqual(result.status, TierRouteStatus.NOT_PARTICIPANT)

    async def test_deadline_upper_bound_is_exclusive(self) -> None:
        accepted = await self.submit(
            1,
            self.session.tier_deadline_at - timedelta(microseconds=1),
            reply_to_message_id=700,
        )
        rejected = await self.submit(
            1,
            self.session.tier_deadline_at,
            message_id=501,
            reply_to_message_id=700,
        )
        self.assertEqual(accepted.status, TierRouteStatus.ACCEPTED)
        self.assertEqual(rejected.status, TierRouteStatus.CLOSED)

    async def test_bound_message_edit_stays_with_original_session(self) -> None:
        opened = self.session.recruitment_completed_notified_at
        await self.submit(1, opened, reply_to_message_id=700)
        second = make_session(
            match_id="match-b",
            match_code="M4Q8",
            now=self.session.created_at,
            status=MatchStatus.FULL,
            completion_notified_at=opened,
            full_reached_at=self.session.full_reached_at,
        )
        second.announcement_message_id = 1001
        second.tier_anchor_message_id = 701
        await self.repository.create_match(second)
        await self.add_roster(second.id, 1, RosterStatus.WAITLISTED, 1)

        result = await self.submit(
            1,
            opened + timedelta(minutes=1),
            content="edited#1234\n마3 / 마3 / 마3",
            reply_to_message_id=701,
        )

        self.assertEqual(result.session.id, self.session.id)
        self.assertEqual(
            (await self.repository.get_tier_submission(self.session.id, 1)).raw_tier_message,
            "edited#1234\n마3 / 마3 / 마3",
        )
        self.assertIsNone(await self.repository.get_tier_submission(second.id, 1))

    async def test_delete_and_empty_edit_clear_only_current_submission(self) -> None:
        opened = self.session.recruitment_completed_notified_at
        await self.submit(1, opened, reply_to_message_id=700)
        deleted = await self.collector.submit_delete(
            guild_id=self.session.guild_id,
            channel_id=self.session.tier_channel_id,
            discord_message_id=500,
            received_at=opened + timedelta(minutes=1),
        )
        self.assertEqual(deleted.status, TierRouteStatus.ACCEPTED)
        self.assertIsNone(await self.repository.get_tier_submission(self.session.id, 1))
        rewritten = await self.submit(
            1,
            opened + timedelta(minutes=2),
            message_id=501,
            reply_to_message_id=700,
        )
        self.assertEqual(rewritten.status, TierRouteStatus.ACCEPTED)
        emptied = await self.submit(
            1,
            opened + timedelta(minutes=3),
            message_id=501,
            content="",
        )
        self.assertEqual(emptied.status, TierRouteStatus.ACCEPTED)
        self.assertIsNone(await self.repository.get_tier_submission(self.session.id, 1))

    async def test_deleting_older_bound_message_does_not_clear_latest(self) -> None:
        opened = self.session.recruitment_completed_notified_at
        await self.submit(1, opened, message_id=500, reply_to_message_id=700)
        await self.submit(
            1,
            opened + timedelta(minutes=1),
            message_id=501,
            reply_to_message_id=700,
        )

        await self.collector.submit_delete(
            guild_id=self.session.guild_id,
            channel_id=self.session.tier_channel_id,
            discord_message_id=500,
            received_at=opened + timedelta(minutes=2),
        )

        submission = await self.repository.get_tier_submission(self.session.id, 1)
        self.assertEqual(submission.discord_message_id, 501)

    async def test_waitlisted_tier_is_stored(self) -> None:
        result = await self.submit(
            11,
            self.session.recruitment_completed_notified_at,
            reply_to_message_id=700,
        )
        self.assertEqual(result.status, TierRouteStatus.ACCEPTED)
        self.assertIsNotNone(await self.repository.get_tier_submission(self.session.id, 11))
