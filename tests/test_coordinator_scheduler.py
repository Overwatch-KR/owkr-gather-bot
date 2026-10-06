from __future__ import annotations

import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from src.application.coordinator import (
    CreateMatchRequest,
    DuplicateStartTime,
    DuplicateSourceRequest,
    SessionCoordinator,
)
from src.application.notification_worker import NotificationWorker
from src.application.rendering import NotificationRenderer
from src.application.scheduler import MatchScheduler
from src.domain.models import (
    MatchStatus,
    NotificationKind,
    ReactionAction,
    ReactionEvent,
    ReactionMutation,
    RosterEntry,
    RosterStatus,
    TierUpsertMutation,
)
from src.infrastructure.sqlite_repository import SQLiteMatchRepository
from src.parsing.match_command import ParsedMatchCommand

from tests.helpers import MutableClock, RecordingWriter, make_config, make_session


class CoordinatorSchedulerTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.database_path = Path(self.tempdir.name) / "test.sqlite3"
        migration = Path(__file__).resolve().parents[1] / "migrations" / "001_initial.sql"
        self.repository = SQLiteMatchRepository(self.database_path)
        await self.repository.initialize(migration)
        self.clock = MutableClock(make_session().created_at)
        self.writer = RecordingWriter()
        self.coordinator = SessionCoordinator(
            make_config(), self.repository, self.writer, self.clock  # type: ignore[arg-type]
        )

    async def asyncTearDown(self) -> None:
        await self.coordinator.stop()
        await self.repository.close()
        self.tempdir.cleanup()

    def parsed(self, offset_hours: int) -> ParsedMatchCommand:
        starts_at = self.clock.now() + timedelta(hours=offset_hours)
        return ParsedMatchCommand(
            starts_at=starts_at,
            tier_deadline_at=starts_at - timedelta(minutes=30),
            lobby_at=starts_at - timedelta(minutes=10),
            mode=None,
        )

    async def add_roster_entry(
        self,
        match_id: str,
        user_id: int,
        status: RosterStatus,
        order: int,
        when,
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
                    match_status=MatchStatus.FULL,
                    roster_entry=RosterEntry(
                        match_id=match_id,
                        discord_user_id=user_id,
                        discord_display_name=f"user-{user_id}",
                        reaction_order=order,
                        status=status,
                        reacted_at=when,
                    ),
                    full_reached_at=when,
                )
            ]
        )

    async def test_new_match_keeps_previous_actor(self) -> None:
        first = await self.coordinator.create_match(
            CreateMatchRequest(200, 101, self.parsed(2))
        )
        await self.coordinator.activate(first, 1001)
        old_actor = self.coordinator.actor_for_match(first.id)
        self.assertIsNotNone(old_actor)

        second = await self.coordinator.create_match(
            CreateMatchRequest(200, 101, self.parsed(3))
        )
        await self.coordinator.activate(second, 1002)
        accepted = old_actor.ingest(
            ReactionEvent(
                match_id=first.id,
                discord_user_id=1,
                discord_display_name="old-user",
                action=ReactionAction.ADD,
                received_at=self.clock.now(),
            )
        )
        self.assertTrue(accepted)
        await old_actor.drain()
        self.assertEqual(second.status, MatchStatus.RECRUITING)
        self.assertEqual((await self.repository.get_match(first.id)).status, MatchStatus.RECRUITING)
        self.assertEqual(len(self.coordinator.active_sessions), 2)

    async def test_later_created_match_within_one_hour_uses_second_lobby(
        self,
    ) -> None:
        first_parsed = self.parsed(2)
        first = await self.coordinator.create_match(
            CreateMatchRequest(200, 101, first_parsed)
        )
        await self.coordinator.activate(first, 1001)
        second_starts_at = first_parsed.starts_at + timedelta(minutes=59)
        second = await self.coordinator.create_match(
            CreateMatchRequest(
                200,
                101,
                ParsedMatchCommand(
                    starts_at=second_starts_at,
                    tier_deadline_at=second_starts_at - timedelta(minutes=30),
                    lobby_at=second_starts_at - timedelta(minutes=10),
                    mode=None,
                ),
            )
        )

        self.assertEqual(first.lobby_voice_channel_id, 105)
        self.assertEqual(first.lobby_name, "대기실 1번")
        self.assertEqual(second.lobby_voice_channel_id, 106)
        self.assertEqual(second.lobby_name, "대기실 2번")
        stored = await self.repository.get_match(second.id)
        self.assertEqual(stored.lobby_voice_channel_id, 106)
        self.assertEqual(stored.lobby_name, "대기실 2번")

    async def test_match_exactly_one_hour_apart_uses_primary_lobby(
        self,
    ) -> None:
        first_parsed = self.parsed(2)
        first = await self.coordinator.create_match(
            CreateMatchRequest(200, 101, first_parsed)
        )
        await self.coordinator.activate(first, 1001)
        second = await self.coordinator.create_match(
            CreateMatchRequest(200, 101, self.parsed(3))
        )

        self.assertEqual(second.lobby_voice_channel_id, 105)
        self.assertEqual(second.lobby_name, "대기실 1번")

    async def test_same_time_match_is_rejected(self) -> None:
        parsed = self.parsed(2)
        first = await self.coordinator.create_match(
            CreateMatchRequest(200, 101, parsed)
        )
        await self.coordinator.activate(first, 1100)
        self.assertEqual(first.participant_limit, 10)

        with self.assertRaises(DuplicateStartTime) as raised:
            await self.coordinator.create_match(
                CreateMatchRequest(201, 101, parsed)
            )

        self.assertEqual(raised.exception.session.id, first.id)
        self.assertEqual(len(self.coordinator.active_sessions), 1)

    async def test_canceling_one_match_keeps_other_actor_running(self) -> None:
        first = await self.coordinator.create_match(
            CreateMatchRequest(200, 101, self.parsed(2))
        )
        second = await self.coordinator.create_match(
            CreateMatchRequest(200, 101, self.parsed(3))
        )
        await self.coordinator.activate(first, 1301)
        await self.coordinator.activate(second, 1302)

        await self.coordinator.cancel_match(first.id)

        self.assertIsNone(self.coordinator.actor_for_match(first.id))
        second_actor = self.coordinator.actor_for_match(second.id)
        self.assertIsNotNone(second_actor)
        self.assertTrue(
            second_actor.ingest(
                ReactionEvent(
                    match_id=second.id,
                    discord_user_id=88,
                    discord_display_name="still-active",
                    action=ReactionAction.ADD,
                    received_at=self.clock.now(),
                )
            )
        )

    async def test_duplicate_source_request_returns_existing_match(self) -> None:
        request = CreateMatchRequest(
            200,
            101,
            self.parsed(2),
            source_request_id="999",
            source_request_type="MESSAGE",
        )
        first = await self.coordinator.create_match(request)

        with self.assertRaises(DuplicateSourceRequest) as raised:
            await self.coordinator.create_match(request)

        self.assertEqual(raised.exception.session.id, first.id)
        self.assertEqual(len(await self.repository.get_active_matches(100)), 1)

    async def test_restart_restores_all_active_actors_and_message_routes(self) -> None:
        first = make_session(
            match_id="restore-a",
            match_code="A7K2",
        )
        second = make_session(
            match_id="restore-b",
            match_code="M4Q8",
            now=first.created_at + timedelta(minutes=1),
        )
        second.announcement_message_id = 1001
        first.tier_anchor_message_id = 2000
        second.tier_anchor_message_id = 2001
        await self.repository.create_match(first)
        await self.repository.create_match(second)

        await self.coordinator.restore()

        self.assertEqual(
            {session.id for session in self.coordinator.active_sessions},
            {"restore-a", "restore-b"},
        )
        self.assertEqual(
            self.coordinator.actor_for_announcement(1000).session.id,
            "restore-a",
        )
        self.assertEqual(
            self.coordinator.actor_for_announcement(1001).session.id,
            "restore-b",
        )
        self.assertEqual(
            self.coordinator.session_for_tier_anchor(2000).id,
            "restore-a",
        )
        self.assertEqual(
            self.coordinator.session_for_tier_anchor(2001).id,
            "restore-b",
        )

    async def test_one_session_scheduler_failure_does_not_block_another(self) -> None:
        first = await self.coordinator.create_match(
            CreateMatchRequest(200, 101, self.parsed(2))
        )
        second = await self.coordinator.create_match(
            CreateMatchRequest(200, 101, self.parsed(3))
        )
        await self.coordinator.activate(first, 1401)
        await self.coordinator.activate(second, 1402)
        first.lobby_at = self.clock.now()
        second.lobby_at = self.clock.now()
        scheduler = MatchScheduler(
            self.coordinator,
            self.repository,
            self.writer,  # type: ignore[arg-type]
            self.clock,
        )
        enqueue = AsyncMock(side_effect=[RuntimeError("first failed"), None])

        with patch.object(
            self.repository,
            "enqueue_lobby_notification",
            enqueue,
        ):
            await scheduler.tick()

        self.assertEqual(enqueue.await_count, 2)

    async def test_starting_one_match_keeps_other_session_active(self) -> None:
        first = await self.coordinator.create_match(
            CreateMatchRequest(200, 101, self.parsed(2))
        )
        second = await self.coordinator.create_match(
            CreateMatchRequest(200, 101, self.parsed(3))
        )
        await self.coordinator.activate(first, 1501)
        await self.coordinator.activate(second, 1502)

        await self.coordinator.start_match(first.id)

        self.assertIsNone(self.coordinator.actor_for_match(first.id))
        self.assertIsNotNone(self.coordinator.actor_for_match(second.id))
        self.assertEqual(
            (await self.repository.get_match(first.id)).status,
            MatchStatus.STARTED,
        )
        self.assertEqual(
            (await self.repository.get_match(second.id)).status,
            MatchStatus.RECRUITING,
        )

    async def test_one_notification_failure_does_not_block_other_match(self) -> None:
        first = make_session(
            match_id="notify-a",
            match_code="A7K2",
        )
        second = make_session(
            match_id="notify-b",
            match_code="M4Q8",
            now=first.created_at + timedelta(seconds=1),
        )
        second.announcement_message_id = 1602
        await self.repository.create_match(first)
        await self.repository.create_match(second)
        await self.add_roster_entry(
            first.id,
            1,
            RosterStatus.CONFIRMED,
            1,
            self.clock.now(),
        )
        await self.add_roster_entry(
            second.id,
            2,
            RosterStatus.CONFIRMED,
            1,
            self.clock.now(),
        )
        await self.repository.apply_mutations(
            [
                ReactionMutation(
                    match_id=first.id,
                    discord_user_id=1,
                    action=ReactionAction.ADD,
                    received_at=self.clock.now(),
                    arrival_seq=2,
                    outcome="DUPLICATE_ADD",
                    next_arrival_seq=3,
                    match_status=MatchStatus.FULL,
                    completion_user_ids=(1,),
                ),
                ReactionMutation(
                    match_id=second.id,
                    discord_user_id=2,
                    action=ReactionAction.ADD,
                    received_at=self.clock.now(),
                    arrival_seq=2,
                    outcome="DUPLICATE_ADD",
                    next_arrival_seq=3,
                    match_status=MatchStatus.FULL,
                    completion_user_ids=(2,),
                ),
            ]
        )
        transport = MagicMock()
        transport.send = AsyncMock(
            side_effect=[RuntimeError("discord failed"), 9002]
        )
        worker = NotificationWorker(
            self.repository,
            self.coordinator,
            NotificationRenderer(
                make_config(),
                Path(__file__).resolve().parents[1]
                / "templates"
                / "recruitment_complete.txt",
            ),
            transport,
            self.clock,
        )

        processed = await worker.process_once()

        self.assertEqual(processed, 2)
        self.assertEqual(transport.send.await_count, 2)
        self.assertIsNone(
            (await self.repository.get_match(first.id)).recruitment_completed_notified_at
        )
        self.assertEqual(
            (await self.repository.get_match(second.id)).recruitment_completed_notified_at,
            self.clock.now(),
        )

    async def test_lobby_notification_is_deduplicated_and_start_stops_session(self) -> None:
        full_at = self.clock.now() + timedelta(minutes=1)
        session = make_session(
            status=MatchStatus.FULL,
            full_reached_at=full_at,
            completion_notified_at=full_at + timedelta(seconds=1),
        )
        await self.repository.create_match(session)
        await self.add_roster_entry(
            session.id, 1, RosterStatus.CONFIRMED, 1, self.clock.now()
        )
        self.clock.value = session.lobby_at
        await self.coordinator.restore()
        scheduler = MatchScheduler(
            self.coordinator,
            self.repository,
            self.writer,  # type: ignore[arg-type]
            self.clock,
            interval_seconds=60,
        )
        await scheduler.tick()
        await scheduler.tick()
        self.assertEqual(
            await self.repository.notification_count(session.id, NotificationKind.LOBBY_REMINDER),
            1,
        )

        self.clock.value = session.starts_at
        await scheduler.tick()
        started = await self.repository.get_match(session.id)
        self.assertEqual(started.status, MatchStatus.STARTED)
        self.assertIsNone(self.coordinator.actor_for_match(session.id))
        latest = await self.repository.get_latest_match(session.guild_id)
        self.assertEqual(latest.status, MatchStatus.STARTED)

    async def test_tier_complete_notification_is_created_once(self) -> None:
        opened = self.clock.now() + timedelta(minutes=1)
        session = make_session(
            status=MatchStatus.FULL,
            full_reached_at=opened - timedelta(seconds=1),
            completion_notified_at=opened,
        )
        await self.repository.create_match(session)
        await self.add_roster_entry(
            session.id, 1, RosterStatus.CONFIRMED, 1, self.clock.now()
        )
        await self.repository.apply_mutations(
            [
                TierUpsertMutation(
                    match_id=session.id,
                    discord_user_id=1,
                    discord_display_name="레몬",
                    raw_tier_message="lemon#32146\n마4 / 마4! / 마4",
                    discord_message_id=500,
                    activity_at=opened,
                    collected_at=opened,
                )
            ]
        )
        await self.repository.enqueue_tier_complete_if_ready(session.id, opened)
        await self.repository.enqueue_tier_complete_if_ready(session.id, opened)
        self.assertEqual(
            await self.repository.notification_count(session.id, NotificationKind.TIER_COMPLETE),
            1,
        )

    async def test_existing_tier_followup_notification_is_silently_completed(
        self,
    ) -> None:
        opened = self.clock.now() + timedelta(minutes=1)
        session = make_session(
            status=MatchStatus.FULL,
            full_reached_at=opened - timedelta(seconds=1),
            completion_notified_at=opened,
        )
        await self.repository.create_match(session)
        await self.add_roster_entry(
            session.id, 1, RosterStatus.CONFIRMED, 1, self.clock.now()
        )
        await self.repository.apply_mutations(
            [
                TierUpsertMutation(
                    match_id=session.id,
                    discord_user_id=1,
                    discord_display_name="레몬",
                    raw_tier_message="lemon#32146\n마4 / 마4! / 마4",
                    discord_message_id=500,
                    activity_at=opened,
                    collected_at=opened,
                )
            ]
        )
        await self.repository.enqueue_tier_complete_if_ready(session.id, opened)
        self.clock.value = opened
        transport = MagicMock()
        transport.send = AsyncMock()
        worker = NotificationWorker(
            self.repository,
            self.coordinator,
            NotificationRenderer(
                make_config(),
                Path(__file__).resolve().parents[1]
                / "templates"
                / "recruitment_complete.txt",
            ),
            transport,
            self.clock,
        )

        processed = await worker.process_once()

        self.assertEqual(processed, 1)
        transport.send.assert_not_awaited()
        restored = await self.repository.get_match(session.id)
        self.assertEqual(restored.tier_complete_notified_at, self.clock.now())

    async def test_scheduler_does_not_create_tier_followup_notifications(self) -> None:
        opened = self.clock.now() + timedelta(minutes=1)
        session = make_session(
            status=MatchStatus.FULL,
            full_reached_at=opened - timedelta(seconds=1),
            completion_notified_at=opened,
        )
        await self.repository.create_match(session)
        await self.add_roster_entry(
            session.id, 1, RosterStatus.CONFIRMED, 1, self.clock.now()
        )
        self.clock.value = session.tier_deadline_at
        await self.coordinator.restore()
        scheduler = MatchScheduler(
            self.coordinator,
            self.repository,
            self.writer,  # type: ignore[arg-type]
            self.clock,
            interval_seconds=60,
        )

        await scheduler.tick()
        await scheduler.tick()

        self.assertEqual(
            await self.repository.notification_count(
                session.id, NotificationKind.TIER_MISSING_REMINDER
            ),
            0,
        )
        self.assertEqual(
            await self.repository.notification_count(
                session.id, NotificationKind.TIER_COMPLETE
            ),
            0,
        )
