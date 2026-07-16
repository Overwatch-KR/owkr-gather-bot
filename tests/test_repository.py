from __future__ import annotations

import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

from owkr_gather_bot.domain.models import (
    MatchStatus,
    NotificationKind,
    ReactionAction,
    ReactionMutation,
    RosterEntry,
    RosterStatus,
    TierDeleteMutation,
    TierUpsertMutation,
)
from owkr_gather_bot.infrastructure.sqlite_repository import SQLiteMatchRepository

from tests.helpers import make_session


class SQLiteRepositoryTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.database_path = Path(self.tempdir.name) / "test.sqlite3"
        self.migration_path = Path(__file__).resolve().parents[1] / "migrations" / "001_initial.sql"
        self.repository = SQLiteMatchRepository(self.database_path)
        await self.repository.initialize(self.migration_path)

    async def asyncTearDown(self) -> None:
        await self.repository.close()
        self.tempdir.cleanup()

    async def add_roster_entry(
        self,
        match_id: str,
        user_id: int,
        status: RosterStatus,
        order: int,
        when,
        *,
        completion_ids: tuple[int, ...] = (),
    ) -> None:
        await self.repository.apply_mutations(
            [
                ReactionMutation(
                    match_id=match_id,
                    discord_user_id=user_id,
                    action=ReactionAction.ADD,
                    received_at=when,
                    arrival_seq=order,
                    outcome=status.value,
                    next_arrival_seq=order + 1,
                    match_status=MatchStatus.FULL if completion_ids else MatchStatus.RECRUITING,
                    roster_entry=RosterEntry(
                        match_id=match_id,
                        discord_user_id=user_id,
                        discord_display_name=f"user-{user_id}",
                        reaction_order=order,
                        status=status,
                        reacted_at=when,
                    ),
                    full_reached_at=when if completion_ids else None,
                    completion_user_ids=completion_ids,
                )
            ]
        )

    async def test_latest_qualifying_tier_is_kept_and_delete_clears_it(self) -> None:
        session = make_session(
            status=MatchStatus.FULL,
            completion_notified_at=make_session().created_at + timedelta(minutes=5),
            full_reached_at=make_session().created_at + timedelta(minutes=4),
        )
        await self.repository.create_replacing_active(session)
        await self.add_roster_entry(
            session.id, 1, RosterStatus.CONFIRMED, 1, session.created_at
        )
        opened = session.recruitment_completed_notified_at
        assert opened is not None

        before_open = TierUpsertMutation(
            match_id=session.id,
            discord_user_id=1,
            discord_display_name="user-1",
            raw_tier_message="old",
            discord_message_id=100,
            activity_at=opened - timedelta(seconds=1),
            collected_at=opened,
        )
        first = TierUpsertMutation(
            match_id=session.id,
            discord_user_id=1,
            discord_display_name="user-1",
            raw_tier_message="first",
            discord_message_id=200,
            activity_at=opened,
            collected_at=opened,
        )
        edited_old_message = TierUpsertMutation(
            match_id=session.id,
            discord_user_id=1,
            discord_display_name="user-1",
            raw_tier_message="latest edit",
            discord_message_id=150,
            activity_at=opened + timedelta(minutes=1),
            collected_at=opened + timedelta(minutes=1),
        )
        await self.repository.apply_mutations([before_open, first, edited_old_message])

        submission = await self.repository.get_tier_submission(session.id, 1)
        self.assertIsNotNone(submission)
        self.assertEqual(submission.raw_tier_message, "latest edit")
        self.assertEqual(submission.discord_message_id, 150)

        await self.repository.apply_mutations(
            [TierDeleteMutation(match_id=session.id, discord_message_id=150)]
        )
        self.assertIsNone(await self.repository.get_tier_submission(session.id, 1))

        rewritten = TierUpsertMutation(
            match_id=session.id,
            discord_user_id=1,
            discord_display_name="user-1",
            raw_tier_message="rewritten",
            discord_message_id=300,
            activity_at=opened + timedelta(minutes=2),
            collected_at=opened + timedelta(minutes=2),
        )
        await self.repository.apply_mutations([rewritten])
        submission = await self.repository.get_tier_submission(session.id, 1)
        self.assertEqual(submission.raw_tier_message, "rewritten")

    async def test_completion_notification_is_created_once(self) -> None:
        session = make_session()
        await self.repository.create_replacing_active(session)
        completed_ids = tuple(range(1, 11))
        await self.add_roster_entry(
            session.id,
            10,
            RosterStatus.CONFIRMED,
            10,
            session.created_at + timedelta(seconds=10),
            completion_ids=completed_ids,
        )
        await self.repository.apply_mutations(
            [
                ReactionMutation(
                    match_id=session.id,
                    discord_user_id=99,
                    action=ReactionAction.ADD,
                    received_at=session.created_at + timedelta(seconds=11),
                    arrival_seq=11,
                    outcome="WAITLISTED",
                    next_arrival_seq=12,
                    match_status=MatchStatus.FULL,
                    completion_user_ids=completed_ids,
                )
            ]
        )
        self.assertEqual(
            await self.repository.notification_count(
                session.id, NotificationKind.RECRUITMENT_COMPLETE
            ),
            1,
        )

    async def test_successful_completion_send_opens_tier_collection(self) -> None:
        session = make_session()
        await self.repository.create_replacing_active(session)
        full_at = session.created_at + timedelta(minutes=1)
        await self.add_roster_entry(
            session.id,
            1,
            RosterStatus.CONFIRMED,
            1,
            full_at,
            completion_ids=(1,),
        )
        before_send = TierUpsertMutation(
            match_id=session.id,
            discord_user_id=1,
            discord_display_name="레몬",
            raw_tier_message="too early",
            discord_message_id=400,
            activity_at=full_at,
            collected_at=full_at,
        )
        await self.repository.apply_mutations([before_send])
        self.assertIsNone(await self.repository.get_tier_submission(session.id, 1))

        notifications = await self.repository.claim_notifications(full_at)
        self.assertEqual(len(notifications), 1)
        sent_at = full_at + timedelta(seconds=2)
        await self.repository.mark_notification_sent(notifications[0], 9999, sent_at)
        restored = await self.repository.get_match(session.id)
        self.assertEqual(restored.recruitment_completed_notified_at, sent_at)

        at_lower_bound = TierUpsertMutation(
            match_id=session.id,
            discord_user_id=1,
            discord_display_name="레몬",
            raw_tier_message="accepted",
            discord_message_id=401,
            activity_at=sent_at,
            collected_at=sent_at,
        )
        await self.repository.apply_mutations([at_lower_bound])
        submission = await self.repository.get_tier_submission(session.id, 1)
        self.assertEqual(submission.raw_tier_message, "accepted")

    async def test_web_export_has_exact_fields_and_excludes_waitlist(self) -> None:
        opened = make_session().created_at + timedelta(minutes=5)
        session = make_session(
            status=MatchStatus.FULL,
            completion_notified_at=opened,
            full_reached_at=opened - timedelta(minutes=1),
        )
        await self.repository.create_replacing_active(session)
        await self.add_roster_entry(session.id, 1, RosterStatus.CONFIRMED, 17, session.created_at)
        await self.add_roster_entry(session.id, 11, RosterStatus.WAITLISTED, 18, session.created_at)
        for user_id, message_id in ((1, 500), (11, 501)):
            await self.repository.apply_mutations(
                [
                    TierUpsertMutation(
                        match_id=session.id,
                        discord_user_id=user_id,
                        discord_display_name="레몬" if user_id == 1 else "대기자",
                        raw_tier_message="lemon#32146\n마4 / 마4! / 마4",
                        discord_message_id=message_id,
                        activity_at=opened,
                        collected_at=opened,
                    )
                ]
            )

        exported = await self.repository.export_web_tiers(session.id)
        self.assertEqual(len(exported), 1)
        payload = exported[0].to_dict()
        self.assertEqual(
            payload,
            {
                "discordUserId": "1",
                "discordDisplayName": "레몬",
                "rawTierMessage": "lemon#32146\n마4 / 마4! / 마4",
                "reactionOrder": 17,
            },
        )
        self.assertEqual(
            set(payload),
            {"discordUserId", "discordDisplayName", "rawTierMessage", "reactionOrder"},
        )

    async def test_restart_restores_active_session_notification_and_cooldown(self) -> None:
        opened = make_session().created_at + timedelta(minutes=5)
        session = make_session(
            status=MatchStatus.FULL,
            completion_notified_at=opened,
            full_reached_at=opened - timedelta(minutes=1),
        )
        await self.repository.create_replacing_active(session)
        await self.add_roster_entry(session.id, 1, RosterStatus.CONFIRMED, 1, session.created_at)
        claimed, missing = await self.repository.claim_missing_tier_reminder(
            session.id, opened + timedelta(minutes=1), 300
        )
        self.assertTrue(claimed)
        self.assertEqual([item.discord_user_id for item in missing], [1])

        await self.repository.close()
        self.repository = SQLiteMatchRepository(self.database_path)
        await self.repository.initialize(self.migration_path)
        restored = await self.repository.get_active_match(session.guild_id)
        self.assertIsNotNone(restored)
        self.assertEqual(restored.recruitment_completed_notified_at, opened)
        claimed_again, _ = await self.repository.claim_missing_tier_reminder(
            session.id, opened + timedelta(minutes=2), 300
        )
        self.assertFalse(claimed_again)

    async def test_new_session_cancels_previous_automatic_work(self) -> None:
        first = make_session(match_id="first")
        second = make_session(match_id="second", now=first.created_at + timedelta(minutes=1))
        second.announcement_message_id = None
        await self.repository.create_replacing_active(first)
        previous = await self.repository.create_replacing_active(second)
        self.assertEqual(previous, ["first"])
        old = await self.repository.get_match("first")
        active = await self.repository.get_active_match(first.guild_id)
        self.assertEqual(old.status, MatchStatus.CANCELED)
        self.assertEqual(active.id, "second")
