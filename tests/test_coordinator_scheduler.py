from __future__ import annotations

import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

from owkr_gather_bot.application.coordinator import CreateMatchRequest, SessionCoordinator
from owkr_gather_bot.application.scheduler import MatchScheduler
from owkr_gather_bot.domain.models import (
    MatchStatus,
    NotificationKind,
    ReactionAction,
    ReactionEvent,
    ReactionMutation,
    RosterEntry,
    RosterStatus,
    TierUpsertMutation,
)
from owkr_gather_bot.infrastructure.sqlite_repository import SQLiteMatchRepository
from owkr_gather_bot.parsing.match_command import ParsedMatchCommand

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

    async def test_new_match_stops_previous_actor(self) -> None:
        first = await self.coordinator.create_replacing_active(
            CreateMatchRequest(200, 101, self.parsed(2))
        )
        await self.coordinator.activate(first, 1001)
        old_actor = self.coordinator.active_actor
        self.assertIsNotNone(old_actor)

        second = await self.coordinator.create_replacing_active(
            CreateMatchRequest(200, 101, self.parsed(3))
        )
        accepted = old_actor.ingest(
            ReactionEvent(
                match_id=first.id,
                discord_user_id=1,
                discord_display_name="old-user",
                action=ReactionAction.ADD,
                received_at=self.clock.now(),
            )
        )
        self.assertFalse(accepted)
        self.assertEqual(second.status, MatchStatus.CREATED)
        self.assertEqual((await self.repository.get_match(first.id)).status, MatchStatus.CANCELED)

    async def test_lobby_notification_is_deduplicated_and_start_stops_session(self) -> None:
        full_at = self.clock.now() + timedelta(minutes=1)
        session = make_session(
            status=MatchStatus.FULL,
            full_reached_at=full_at,
            completion_notified_at=full_at + timedelta(seconds=1),
        )
        await self.repository.create_replacing_active(session)
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
        self.assertIsNone(self.coordinator.active_actor)
        latest = await self.repository.get_latest_match(session.guild_id)
        self.assertEqual(latest.status, MatchStatus.STARTED)

    async def test_tier_complete_notification_is_created_once(self) -> None:
        opened = self.clock.now() + timedelta(minutes=1)
        session = make_session(
            status=MatchStatus.FULL,
            full_reached_at=opened - timedelta(seconds=1),
            completion_notified_at=opened,
        )
        await self.repository.create_replacing_active(session)
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
