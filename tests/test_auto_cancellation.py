from __future__ import annotations

import asyncio
import tempfile
import unittest
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from unittest.mock import AsyncMock

from src.application.cancellation import CancellationWorker
from src.application.coordinator import CreateMatchRequest, SessionCoordinator
from src.application.scheduler import MatchScheduler
from src.domain.models import MatchStatus, ReactionAction
from src.infrastructure.persistence_writer import PersistenceWriter
from src.infrastructure.sqlite_repository import SQLiteMatchRepository
from src.parsing.match_command import CommandParseError, MatchCommandParser, ParsedMatchCommand
from tests.helpers import MutableClock, make_config, make_session


class CancellationFixture(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.path = Path(self.tempdir.name) / 'test.db'
        self.migration = Path('migrations/001_initial.sql')
        self.clock = MutableClock(make_session().created_at)
        await self.open_runtime()

    async def open_runtime(self):
        self.repo = SQLiteMatchRepository(self.path)
        await self.repo.initialize(self.migration)
        self.writer = PersistenceWriter(self.repo)
        self.writer.start()
        self.coordinator = SessionCoordinator(make_config(), self.repo, self.writer, self.clock)
        await self.coordinator.restore()
        self.scheduler = MatchScheduler(self.coordinator, self.repo, self.writer, self.clock)

    async def close_runtime(self):
        await self.coordinator.stop()
        await self.writer.stop()
        await self.repo.close()

    async def asyncTearDown(self):
        await self.close_runtime()
        self.tempdir.cleanup()

    async def create(self, message_id=1001, *, deadline=True):
        start = make_session().starts_at
        parsed = ParsedMatchCommand(start, start - timedelta(minutes=30), start - timedelta(minutes=10), None,
                                    self.clock.now() + timedelta(minutes=30) if deadline else None)
        session = await self.coordinator.create_match(CreateMatchRequest(200, 101, parsed))
        await self.coordinator.activate(session, message_id)
        return session

    def react(self, session, user_id, when, action=ReactionAction.ADD):
        return self.coordinator.ingest_reaction(
            announcement_message_id=session.announcement_message_id,
            discord_user_id=user_id, discord_display_name=str(user_id),
            action=action, received_at=when,
        )


class AutoCancellationTest(CancellationFixture):
    async def test_deadline_boundary_and_same_time_session_isolation(self):
        session = await self.create()
        other = await self.create(1002, deadline=False)
        for user in range(9):
            self.react(session, user, session.recruitment_cancel_at - timedelta(microseconds=1))
        self.react(session, 9, session.recruitment_cancel_at)  # 마감 시각은 포함하지 않음
        await self.coordinator.actor_for_match(session.id).drain()
        self.assertEqual(len(self.coordinator.actor_for_match(session.id).current_confirmed()), 9)
        self.clock.value = session.recruitment_cancel_at - timedelta(microseconds=1)
        await self.scheduler.tick()
        self.assertEqual(session.status, MatchStatus.RECRUITING)
        self.clock.value += timedelta(microseconds=1)
        await self.scheduler.tick()
        self.assertEqual(session.status, MatchStatus.CANCELED)
        self.assertEqual(other.status, MatchStatus.RECRUITING)
        self.assertIsNone(self.coordinator.actor_for_announcement(1001))
        self.assertIsNotNone(self.coordinator.actor_for_announcement(1002))
        self.assertEqual(len(await self.repo.pending_cancellations(100)), 1)
        await self.scheduler.tick()
        self.assertEqual(len(await self.repo.pending_cancellations(100)), 1)

    async def test_queued_tenth_before_deadline_prevents_cancel_even_after_withdrawal(self):
        session = await self.create()
        before = session.recruitment_cancel_at - timedelta(microseconds=1)
        for user in range(10):
            self.react(session, user, before)
        self.clock.value = session.recruitment_cancel_at
        await self.scheduler.tick()  # 취소 판정 전에 접수된 Actor 큐와 writer를 비워야 함
        self.assertEqual(session.status, MatchStatus.FULL)
        self.react(session, 0, self.clock.now(), ReactionAction.REMOVE)
        await self.coordinator.actor_for_match(session.id).drain()
        await self.scheduler.tick()
        self.assertEqual(len(self.coordinator.actor_for_match(session.id).current_confirmed()), 9)
        self.assertEqual(await self.repo.pending_cancellations(100), [])

    async def test_restart_after_start_still_cancels_overdue_unfilled_match(self):
        session = await self.create()
        await self.close_runtime()
        self.clock.value = session.starts_at + timedelta(minutes=1)
        await self.open_runtime()
        restored = self.coordinator.active_sessions[0]
        self.assertEqual(restored.recruitment_cancel_at, session.recruitment_cancel_at)
        await self.scheduler.tick()
        self.assertEqual((await self.repo.get_match(session.id)).status, MatchStatus.CANCELED)
        self.assertEqual(len(await self.repo.pending_cancellations(100)), 1)

    async def test_jobs_survive_session_purge_and_restart_and_notice_is_not_repeated(self):
        session = await self.create()
        results = await asyncio.gather(*(
            self.coordinator.cancel_match(session.id, queue_cleanup=True) for _ in range(3)
        ))
        self.assertEqual(sum(item is not None for item in results), 1)
        await self.repo.purge_closed_matches(100)
        self.assertIsNone(await self.repo.get_match(session.id))
        await self.close_runtime()
        await self.open_runtime()
        transport = AsyncMock()
        transport.cleanup.return_value = False
        transport.notify.return_value = True
        worker = CancellationWorker(self.repo, transport, 100)
        await worker.process_once()
        self.assertTrue((await self.repo.pending_cancellations(100))[0].notice_sent)
        transport.cleanup.return_value = True
        await worker.process_once()
        transport.notify.assert_awaited_once()
        self.assertEqual(await self.repo.pending_cancellations(100), [])

    async def test_no_deadline_keeps_legacy_behavior_and_creation_revalidates_deadline(self):
        session = await self.create(deadline=False)
        self.clock.value += timedelta(hours=1)
        await self.scheduler.tick()
        self.assertEqual(session.status, MatchStatus.RECRUITING)
        parsed = ParsedMatchCommand(session.starts_at, session.tier_deadline_at, session.lobby_at, None,
                                    self.clock.now())
        with self.assertRaises(CommandParseError):
            await self.coordinator.create_match(CreateMatchRequest(200, 101, parsed))

    async def test_existing_database_upgrade_and_interrupted_migration_retry(self):
        session = await self.create(deadline=False)
        await self.coordinator.stop()
        await self.writer.stop()
        await self.repo.connection.execute("DELETE FROM schema_migrations WHERE name = '007_recruitment_cancel.sql'")
        await self.repo.connection.execute("DROP TABLE cancellation_jobs")
        await self.repo.connection.execute("ALTER TABLE match_sessions DROP COLUMN recruitment_cancel_at_utc")
        await self.repo.connection.commit()
        await self.repo.close()
        await self.open_runtime()
        restored = self.coordinator.active_sessions[0]
        self.assertEqual(restored.id, session.id)
        self.assertIsNone(restored.recruitment_cancel_at)
        # 열/테이블 적용 후 마이그레이션 기록 전 중단을 재현한다.
        await self.repo.connection.execute("DELETE FROM schema_migrations WHERE name = '007_recruitment_cancel.sql'")
        await self.repo.connection.commit()
        await self.close_runtime()
        await self.open_runtime()
        self.assertEqual(self.coordinator.active_sessions[0].id, session.id)
        self.assertEqual((await self.repo._fetchone("PRAGMA integrity_check"))[0], 'ok')
        self.assertEqual(await self.repo._fetchall("PRAGMA foreign_key_check"), [])


class CancellationTimeParsingTest(unittest.TestCase):
    def test_optional_time_and_invalid_boundaries(self):
        now = make_session().created_at
        parser = MatchCommandParser(tier_deadline_offset_minutes=30, lobby_offset_minutes=10)
        parsed = parser.parse('오후 8시', now=now)
        self.assertIsNone(parser.with_cancellation_time(parsed, '', now=now).recruitment_cancel_at)
        for value in ('오후 7시 30분', '19:30'):
            self.assertEqual(parser.with_cancellation_time(parsed, value, now=now).recruitment_cancel_at.hour, 19)
        for value in ('17:00', '16:59', '20:00', '20:01', '24:00', '19:60', '19:30 모드'):
            with self.subTest(value=value), self.assertRaises(CommandParseError):
                parser.with_cancellation_time(parsed, value, now=now)
