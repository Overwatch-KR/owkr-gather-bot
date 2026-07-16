from __future__ import annotations

import tempfile
import unittest
import sqlite3
from datetime import timedelta
from pathlib import Path

from src.domain.models import (
    MatchStatus,
    NotificationKind,
    ReactionAction,
    ReactionMutation,
    RosterEntry,
    RosterStatus,
    TierDeleteMutation,
    TierMessageBinding,
    TierSubmission,
    TierUpsertMutation,
)
from src.infrastructure.sqlite_repository import SQLiteMatchRepository

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
        await self.repository.create_match(session)
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

    async def test_equal_activity_uses_larger_message_id_as_tie_break(self) -> None:
        opened = make_session().created_at + timedelta(minutes=5)
        session = make_session(
            status=MatchStatus.FULL,
            completion_notified_at=opened,
            full_reached_at=opened - timedelta(minutes=1),
        )
        await self.repository.create_match(session)
        await self.add_roster_entry(session.id, 1, RosterStatus.CONFIRMED, 1, session.created_at)

        await self.repository.apply_mutations(
            [
                TierUpsertMutation(
                    match_id=session.id,
                    discord_user_id=1,
                    discord_display_name="레몬",
                    raw_tier_message="lower id",
                    discord_message_id=600,
                    activity_at=opened,
                    collected_at=opened,
                ),
                TierUpsertMutation(
                    match_id=session.id,
                    discord_user_id=1,
                    discord_display_name="레몬",
                    raw_tier_message="higher id",
                    discord_message_id=601,
                    activity_at=opened,
                    collected_at=opened,
                ),
            ]
        )

        submission = await self.repository.get_tier_submission(session.id, 1)
        self.assertEqual(submission.raw_tier_message, "higher id")
        self.assertEqual(submission.discord_message_id, 601)

    async def test_completion_notification_is_created_once(self) -> None:
        session = make_session()
        await self.repository.create_match(session)
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
        await self.repository.create_match(session)
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

    async def test_lobby_notification_can_complete_without_a_discord_message(
        self,
    ) -> None:
        full_at = make_session().created_at + timedelta(minutes=1)
        session = make_session(
            status=MatchStatus.FULL,
            full_reached_at=full_at,
        )
        await self.repository.create_match(session)
        await self.add_roster_entry(
            session.id,
            1,
            RosterStatus.CONFIRMED,
            1,
            full_at,
            completion_ids=(1,),
        )
        await self.repository.enqueue_lobby_notification(
            session.id,
            session.lobby_at,
        )
        notifications = await self.repository.claim_notifications(session.lobby_at)
        reminder = next(
            item
            for item in notifications
            if item.kind is NotificationKind.LOBBY_REMINDER
        )

        await self.repository.mark_notification_sent(
            reminder,
            None,
            session.lobby_at,
        )
        await self.repository.enqueue_lobby_notification(
            session.id,
            session.lobby_at,
        )

        restored = await self.repository.get_match(session.id)
        self.assertEqual(restored.lobby_notified_at, session.lobby_at)
        self.assertEqual(
            await self.repository.notification_count(
                session.id,
                NotificationKind.LOBBY_REMINDER,
            ),
            1,
        )

    async def test_tier_anchor_recreation_keeps_existing_bindings(self) -> None:
        session = make_session()
        await self.repository.create_match(session)
        full_at = session.created_at + timedelta(minutes=1)
        await self.add_roster_entry(
            session.id,
            1,
            RosterStatus.CONFIRMED,
            1,
            full_at,
            completion_ids=(1,),
        )
        completion = (await self.repository.claim_notifications(full_at))[0]
        opened = full_at + timedelta(seconds=1)
        await self.repository.mark_notification_sent(completion, 600, opened)
        anchor_notification = next(
            item
            for item in await self.repository.claim_notifications(opened)
            if item.kind is NotificationKind.TIER_ANCHOR
        )
        await self.repository.mark_notification_sent(
            anchor_notification,
            700,
            opened,
        )
        self.assertEqual(
            (await self.repository.get_match(session.id)).tier_anchor_message_id,
            700,
        )
        accepted = await self.repository.upsert_tier_message(
            TierMessageBinding(
                discord_message_id=800,
                match_id=session.id,
                discord_user_id=1,
                bound_at=opened,
            ),
            TierSubmission(
                match_id=session.id,
                discord_user_id=1,
                discord_display_name="레몬",
                raw_tier_message="lemon#1234\n마4 / 마4 / 마4",
                discord_message_id=800,
                activity_at=opened,
                collected_at=opened,
            ),
        )
        self.assertTrue(accepted)

        recreated = await self.repository.request_tier_anchor_recreation(
            700,
            opened + timedelta(seconds=1),
        )
        self.assertEqual(recreated.id, session.id)
        self.assertIsNotNone(await self.repository.get_tier_binding(800))
        self.assertIsNotNone(await self.repository.get_tier_submission(session.id, 1))
        replacement = next(
            item
            for item in await self.repository.claim_notifications(
                opened + timedelta(seconds=1)
            )
            if item.kind is NotificationKind.TIER_ANCHOR
        )
        self.assertTrue(replacement.payload["recreated"])
        await self.repository.mark_notification_sent(
            replacement,
            701,
            opened + timedelta(seconds=2),
        )
        admin_notice = next(
            item
            for item in await self.repository.claim_notifications(
                opened + timedelta(seconds=2)
            )
            if item.kind is NotificationKind.TIER_ANCHOR_RECREATED
        )
        self.assertEqual(admin_notice.payload["tier_anchor_message_id"], 701)

    async def test_web_export_has_exact_fields_and_excludes_waitlist(self) -> None:
        opened = make_session().created_at + timedelta(minutes=5)
        session = make_session(
            status=MatchStatus.FULL,
            completion_notified_at=opened,
            full_reached_at=opened - timedelta(minutes=1),
        )
        await self.repository.create_match(session)
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

    async def test_web_export_excludes_withdrawn_and_missing_tier_users(self) -> None:
        opened = make_session().created_at + timedelta(minutes=5)
        session = make_session(
            status=MatchStatus.FULL,
            completion_notified_at=opened,
            full_reached_at=opened - timedelta(minutes=1),
        )
        await self.repository.create_match(session)
        for user_id in (1, 2, 3):
            await self.add_roster_entry(
                session.id, user_id, RosterStatus.CONFIRMED, user_id, session.created_at
            )
        for user_id in (1, 2):
            await self.repository.apply_mutations(
                [
                    TierUpsertMutation(
                        match_id=session.id,
                        discord_user_id=user_id,
                        discord_display_name=f"user-{user_id}",
                        raw_tier_message=f"tier-{user_id}",
                        discord_message_id=700 + user_id,
                        activity_at=opened,
                        collected_at=opened,
                    )
                ]
            )
        await self.repository.apply_mutations(
            [
                ReactionMutation(
                    match_id=session.id,
                    discord_user_id=2,
                    action=ReactionAction.REMOVE,
                    received_at=opened + timedelta(seconds=1),
                    arrival_seq=4,
                    outcome="WITHDRAWN",
                    next_arrival_seq=5,
                    match_status=MatchStatus.FULL,
                    roster_entry=RosterEntry(
                        match_id=session.id,
                        discord_user_id=2,
                        discord_display_name="user-2",
                        reaction_order=2,
                        status=RosterStatus.WITHDRAWN,
                        reacted_at=session.created_at,
                        removed_at=opened + timedelta(seconds=1),
                    ),
                    full_reached_at=session.full_reached_at,
                )
            ]
        )

        exported = await self.repository.export_web_tiers(session.id)
        self.assertEqual([item.discord_user_id for item in exported], ["1"])

    async def test_restart_restores_active_session_notification_and_cooldown(self) -> None:
        opened = make_session().created_at + timedelta(minutes=5)
        session = make_session(
            status=MatchStatus.FULL,
            completion_notified_at=opened,
            full_reached_at=opened - timedelta(minutes=1),
        )
        await self.repository.create_match(session)
        await self.add_roster_entry(session.id, 1, RosterStatus.CONFIRMED, 1, session.created_at)
        claimed, missing = await self.repository.claim_missing_tier_reminder(
            session.id, opened + timedelta(minutes=1), 300
        )
        self.assertTrue(claimed)
        self.assertEqual([item.discord_user_id for item in missing], [1])

        await self.repository.close()
        self.repository = SQLiteMatchRepository(self.database_path)
        await self.repository.initialize(self.migration_path)
        restored_matches = await self.repository.get_active_matches(session.guild_id)
        restored = next(
            item for item in restored_matches if item.id == session.id
        )
        self.assertIsNotNone(restored)
        self.assertEqual(restored.recruitment_completed_notified_at, opened)
        claimed_again, _ = await self.repository.claim_missing_tier_reminder(
            session.id, opened + timedelta(minutes=2), 300
        )
        self.assertFalse(claimed_again)
        claimed_after_cooldown, missing_after_cooldown = (
            await self.repository.claim_missing_tier_reminder(
                session.id, opened + timedelta(minutes=6), 300
            )
        )
        self.assertTrue(claimed_after_cooldown)
        self.assertEqual([item.discord_user_id for item in missing_after_cooldown], [1])

    async def test_new_session_keeps_previous_automatic_work(self) -> None:
        first = make_session(match_id="first", match_code="A7K2")
        second = make_session(
            match_id="second",
            match_code="M4Q8",
            now=first.created_at + timedelta(minutes=1),
        )
        second.announcement_message_id = None
        await self.repository.create_match(first)
        await self.repository.create_match(second)
        old = await self.repository.get_match("first")
        active = await self.repository.get_active_matches(first.guild_id)
        self.assertEqual(old.status, MatchStatus.RECRUITING)
        self.assertEqual({item.id for item in active}, {"first", "second"})

    async def test_notification_dedupe_keys_are_scoped_by_match_id(self) -> None:
        first = make_session(
            match_id="first",
            match_code="A7K2",
            status=MatchStatus.FULL,
            full_reached_at=make_session().created_at,
        )
        second = make_session(
            match_id="second",
            match_code="M4Q8",
            now=first.created_at + timedelta(seconds=1),
            status=MatchStatus.FULL,
            full_reached_at=first.created_at,
        )
        second.announcement_message_id = 1001
        await self.repository.create_match(first)
        await self.repository.create_match(second)
        await self.add_roster_entry(
            first.id,
            1,
            RosterStatus.CONFIRMED,
            1,
            first.created_at,
        )
        await self.add_roster_entry(
            second.id,
            2,
            RosterStatus.CONFIRMED,
            1,
            second.created_at,
        )

        await self.repository.enqueue_lobby_notification(first.id, first.lobby_at)
        await self.repository.enqueue_lobby_notification(second.id, second.lobby_at)
        notifications = await self.repository.claim_notifications(
            max(first.lobby_at, second.lobby_at)
        )

        lobby = [
            item for item in notifications
            if item.kind is NotificationKind.LOBBY_REMINDER
        ]
        self.assertEqual(len(lobby), 2)
        self.assertEqual(len({item.dedupe_key for item in lobby}), 2)
        self.assertTrue(
            all(item.match_id in item.dedupe_key for item in lobby)
        )

    async def test_existing_single_session_database_is_migrated_without_data_loss(
        self,
    ) -> None:
        legacy_path = Path(self.tempdir.name) / "legacy.sqlite3"
        connection = sqlite3.connect(legacy_path)
        connection.executescript(self.migration_path.read_text(encoding="utf-8"))
        session = make_session()
        connection.execute(
            """
            INSERT INTO match_sessions (
                id, guild_id, manager_user_id, command_channel_id,
                announcement_channel_id, announcement_message_id,
                tier_channel_id, admin_channel_id, mode, participant_limit,
                status, starts_at_utc, tier_deadline_at_utc, lobby_at_utc,
                full_reached_at_utc, recruitment_completed_notified_at_utc,
                tier_complete_notified_at_utc,
                tier_missing_reminder_notified_at_utc, lobby_notified_at_utc,
                last_missing_tier_reminder_at_utc, next_arrival_seq,
                created_at_utc, updated_at_utc
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                session.id,
                session.guild_id,
                session.manager_user_id,
                session.command_channel_id,
                session.announcement_channel_id,
                session.announcement_message_id,
                session.tier_channel_id,
                session.admin_channel_id,
                session.mode,
                session.participant_limit,
                session.status.value,
                session.starts_at.isoformat(),
                session.tier_deadline_at.isoformat(),
                session.lobby_at.isoformat(),
                None,
                session.created_at.isoformat(),
                None,
                None,
                None,
                None,
                2,
                session.created_at.isoformat(),
                session.updated_at.isoformat(),
            ),
        )
        connection.execute(
            """
            INSERT INTO roster_entries (
                match_id, discord_user_id, discord_display_name,
                reaction_order, status, reacted_at_utc, removed_at_utc
            ) VALUES (?, ?, ?, ?, ?, ?, NULL)
            """,
            (
                session.id,
                1,
                "legacy-user",
                1,
                RosterStatus.CONFIRMED.value,
                session.created_at.isoformat(),
            ),
        )
        connection.execute(
            """
            INSERT INTO tier_submissions (
                match_id, discord_user_id, discord_display_name,
                raw_tier_message, discord_message_id,
                activity_at_utc, collected_at_utc
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                session.id,
                1,
                "legacy-user",
                "legacy#1234\n마4 / 마4 / 마4",
                900,
                session.created_at.isoformat(),
                session.created_at.isoformat(),
            ),
        )
        connection.commit()
        connection.close()

        migrated = SQLiteMatchRepository(legacy_path)
        await migrated.initialize(self.migration_path)
        try:
            restored = await migrated.get_match(session.id)
            self.assertIsNotNone(restored.match_code)
            self.assertEqual(
                (await migrated.load_roster(session.id))[0].discord_display_name,
                "legacy-user",
            )
            binding = await migrated.get_tier_binding(900)
            self.assertEqual(binding.match_id, session.id)
            self.assertEqual(
                (await migrated.get_tier_submission(session.id, 1)).raw_tier_message,
                "legacy#1234\n마4 / 마4 / 마4",
            )
        finally:
            await migrated.close()
