"""생성 실패 보상과 종료 직전 접수분의 영속화를 실제 SQLite로 확인한다."""

import asyncio
import unittest
from contextlib import nullcontext
from datetime import datetime, timezone
from itertools import count
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from src.application.coordinator import DuplicateSourceRequest, LobbyCapacityExceeded, SessionCoordinator
from src.application.recruitment import RecruitmentService
from src.application.runtime import ApplicationRuntime
from src.application.safety import SafetyService
from src.application.scheduler import MatchScheduler
from src.application.tier_collector import TierCollector
from src.bootstrap import build_bot
from src.config import RuntimeConfig
from src.domain.models import MatchStatus, ReactionAction, RosterStatus
from src.infrastructure.persistence_writer import PersistenceWriter
from src.infrastructure.sqlite_repository import SQLiteMatchRepository
from src.parsing.match_command import MatchCommandParser
from tests.helpers import MutableClock, make_config

MIGRATION = Path(__file__).resolve().parents[1] / "migrations/001_initial.sql"


class Announcement:
    def __init__(self, message_id, fail_seed):
        self.message_id = message_id
        self.fail_seed = fail_seed
        self.deleted = False

    async def add_seed_reaction(self):
        if self.fail_seed:
            raise RuntimeError("seed failed")

    async def delete_incomplete(self):
        self.deleted = True


class Publisher:
    _ids = count(1000)

    def __init__(self, failure=None):
        self.failure = failure
        self.sessions = []
        self.announcements = []

    async def publish(self, session):
        self.sessions.append(session)
        if self.failure == "publish":
            raise RuntimeError("publish failed")
        result = Announcement(next(self._ids), self.failure == "seed")
        self.announcements.append(result)
        return result


class RecruitmentRuntimeTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tempdir = TemporaryDirectory()
        self.database = Path(self.tempdir.name) / "test.sqlite3"
        self.config = make_config()
        self.clock = MutableClock(datetime(2026, 7, 16, 8, tzinfo=timezone.utc))
        self.runtime = self.make_runtime()
        await self.runtime.start_storage()

    def make_runtime(self):
        repo = SQLiteMatchRepository(self.database)
        writer = PersistenceWriter(repo, flush_interval_seconds=0.01)
        coordinator = SessionCoordinator(self.config, repo, writer, self.clock)
        return ApplicationRuntime(
            repository=repo,
            writer=writer,
            coordinator=coordinator,
            tier_collector=TierCollector(repo, self.clock),
            scheduler=MatchScheduler(coordinator, repo, writer, self.clock),
            safety=SafetyService(repo, self.clock, self.config.guild_id),
            migration_path=MIGRATION,
        )

    async def asyncTearDown(self):
        await self.runtime.stop()
        self.tempdir.cleanup()

    async def create(self, publisher, time="20:00", request="request-1"):
        return await RecruitmentService(self.runtime.coordinator, publisher).create(
            manager_user_id=200,
            command_channel_id=101,
            parsed=MatchCommandParser(
                tier_deadline_offset_minutes=30,
                lobby_offset_minutes=10,
            ).parse(time, now=self.clock.now()),
            source_request_id=request,
            source_request_type="INTERACTION",
        )

    async def test_duplicate_request_never_publishes_a_second_announcement(self):
        publisher = Publisher()
        session = await self.create(publisher)
        with self.assertRaises(DuplicateSourceRequest):
            await self.create(publisher)
        self.assertEqual(len(publisher.sessions), 1)
        message_id = publisher.announcements[0].message_id
        self.assertIs(
            self.runtime.coordinator.actor_for_announcement(message_id).session, session
        )
        stored = await self.runtime.repository.get_match(session.id)
        self.assertEqual(stored.announcement_message_id, message_id)

    async def test_concurrent_same_time_creates_only_two_announcements(self):
        publisher = Publisher()
        results = await asyncio.gather(
            *(self.create(publisher, request=f"parallel-{i}") for i in range(3)),
            return_exceptions=True,
        )
        sessions = [result for result in results if not isinstance(result, Exception)]
        self.assertEqual(len(sessions), 2)
        self.assertEqual(sum(isinstance(result, LobbyCapacityExceeded) for result in results), 1)
        self.assertEqual(len(publisher.announcements), 2)
        self.assertEqual({s.lobby_voice_channel_id for s in sessions}, {105, 106})
        # 같은 요청 재전송은 정원 검사보다 먼저 기존 결과로 연결한다.
        with self.assertRaises(DuplicateSourceRequest):
            await self.create(publisher, request="parallel-0")
        self.assertEqual(len(publisher.announcements), 2)

    async def test_same_time_restart_keeps_lobbies_rosters_and_routes_separate(self):
        first = await self.create(Publisher(), request="same-a")
        second = await self.create(Publisher(), request="same-b")
        for session, user_id in [(first, 301), (second, 302)]:
            self.runtime.coordinator.ingest_reaction(
                announcement_message_id=session.announcement_message_id,
                discord_user_id=user_id, discord_display_name=f"user-{user_id}",
                action=ReactionAction.ADD, received_at=self.clock.now(),
            )
        await self.runtime.stop()
        self.runtime = self.make_runtime()
        await self.runtime.start_storage()
        for session, user_id, lobby in [(first, 301, 105), (second, 302, 106)]:
            actor = self.runtime.coordinator.actor_for_announcement(session.announcement_message_id)
            self.assertEqual(actor.session.lobby_voice_channel_id, lobby)
            self.assertEqual([e.discord_user_id for e in actor.current_confirmed()], [user_id])
        await self.runtime.coordinator.cancel_match(first.id)
        self.assertIsNone(self.runtime.coordinator.actor_for_announcement(first.announcement_message_id))
        self.assertIsNotNone(self.runtime.coordinator.actor_for_announcement(second.announcement_message_id))
        replacement = await self.create(Publisher(), request="same-c")
        self.assertEqual(replacement.lobby_voice_channel_id, 105)

    async def test_failed_same_time_publication_releases_only_its_lobby(self):
        first = await self.create(Publisher(), request="kept")
        with self.assertRaisesRegex(RuntimeError, "seed failed"):
            await self.create(Publisher("seed"), request="failed")
        replacement = await self.create(Publisher(), request="replacement")
        self.assertEqual(replacement.lobby_voice_channel_id, 106)
        self.assertIsNotNone(self.runtime.coordinator.actor_for_match(first.id))

    async def test_failed_creation_cancels_only_its_session_and_cleans_sent_message(
        self,
    ):
        other = await self.create(Publisher(), "21:00", "other")
        for failure in ("publish", "activate", "seed"):
            with self.subTest(failure=failure):
                publisher = Publisher(failure)
                activation = (
                    patch.object(
                        self.runtime.coordinator,
                        "activate",
                        side_effect=RuntimeError("activate failed"),
                    )
                    if failure == "activate"
                    else nullcontext()
                )
                with activation:
                    with self.assertRaisesRegex(RuntimeError, f"{failure} failed"):
                        await self.create(publisher, request=failure)
                failed = await self.runtime.repository.get_match(
                    publisher.sessions[0].id
                )
                self.assertEqual(failed.status, MatchStatus.CANCELED)
                self.assertIsNone(self.runtime.coordinator.actor_for_match(failed.id))
                self.assertIsNotNone(self.runtime.coordinator.actor_for_match(other.id))
                self.assertIsNotNone(
                    self.runtime.coordinator.actor_for_announcement(
                        other.announcement_message_id
                    )
                )
                if failure != "publish":
                    self.assertTrue(publisher.announcements[0].deleted)

    async def test_shutdown_drains_burst_and_restart_restores_roster_and_routes(self):
        session = await self.create(Publisher())
        for user_id in range(300, 318):
            self.runtime.coordinator.ingest_reaction(
                announcement_message_id=session.announcement_message_id,
                discord_user_id=user_id,
                discord_display_name=f"user-{user_id}",
                action=ReactionAction.ADD,
                received_at=self.clock.now(),
            )
        # drain/flush를 직접 호출하지 않고 종료한다. 종료 순서가 잘못되면 일부가 사라진다.
        await self.runtime.stop()
        self.runtime = self.make_runtime()
        await self.runtime.start_storage()
        actor = self.runtime.coordinator.actor_for_announcement(
            session.announcement_message_id
        )
        self.assertEqual(actor.session.id, session.id)
        self.assertEqual(
            [e.discord_user_id for e in actor.current_confirmed()],
            list(range(300, 310)),
        )
        roster = await self.runtime.repository.load_roster(session.id)
        self.assertEqual(sum(e.status is RosterStatus.WAITLISTED for e in roster), 8)

    async def test_bootstrap_close_stops_catchup_before_closing_the_database(self):
        root = MIGRATION.parents[1]
        config = RuntimeConfig(
            token="unused-no-network",
            app=self.config,
            config_path=Path(self.tempdir.name) / "config.yaml",
            database_path=Path(self.tempdir.name) / "bootstrap.sqlite3",
            template_path=root / "templates/recruitment.txt",
            recruitment_complete_template_path=root
            / "templates/recruitment_complete.txt",
            log_level="INFO",
            dotenv_path=Path(self.tempdir.name) / ".env",
            dotenv_loaded=False,
            working_directory=root,
        )
        bot = build_bot(config)
        await bot.runtime.start_storage()
        started = asyncio.Event()
        canceled_while_db_open = []

        async def catchup():
            try:
                started.set()
                await asyncio.Event().wait()
            finally:
                # 보완 수집 정리가 DB보다 늦으면 connection 조회가 실패한다.
                canceled_while_db_open.append(
                    bot.runtime.repository.connection is not None
                )

        bot._catch_up_task = asyncio.create_task(catchup())
        await started.wait()
        await bot.close()
        self.assertEqual(canceled_while_db_open, [True])
        self.assertTrue(bot.is_closed())
