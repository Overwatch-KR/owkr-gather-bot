"""Exercise tier erasure across real SQLite backup bundles without Discord or live data."""
from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from scripts.backup import create_backup
from scripts.retention import prune_backups
from src.adapters.safety_adapter import SafetyCog, TierErasureConfirmation
from src.application.safety import SafetyService
from src.application.tier_collector import TierCollector, TierRouteStatus
from src.domain.models import (
    MatchStatus, ReactionAction, ReactionMutation, RosterEntry, RosterStatus,
    TierMessageBinding, TierSubmission, TierUpsertMutation,
)
from src.infrastructure.sqlite_repository import SQLiteMatchRepository
from src.domain.warnings import WarningReason
from tests.helpers import MutableClock, make_config, make_session


class ErasureRecoveryTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.live = self.root / 'live'
        (self.live / 'config').mkdir(parents=True)
        (self.live / 'config/config.yaml').write_text('guild_id: 100\n')
        (self.live / 'templates').mkdir()
        self.path = self.live / 'data/owkr-gather-bot.sqlite3'
        self.migration = Path(__file__).resolve().parents[1] / 'migrations/001_initial.sql'
        self.repo = await self.open_repo(self.path)
        self.clock = MutableClock(make_session().created_at)
        self.sessions = []
        for index, guild_id in enumerate((100, 100, 999)):
            session = replace(
                make_session(match_id=f'match-{index}', match_code=f'T{index}K2',
                             status=MatchStatus.FULL, completion_notified_at=self.clock.now()),
                guild_id=guild_id, announcement_message_id=1000 + index,
                tier_anchor_message_id=9000 + index,
            )
            self.sessions.append(session)
            await self.repo.create_match(session)
            for order, user_id in enumerate((300, 301), start=1):
                await self.repo.apply_mutations([ReactionMutation(
                    session.id, user_id, ReactionAction.ADD, self.clock.now(), order,
                    'CONFIRMED', order + 1, session.status,
                    RosterEntry(session.id, user_id, 'synthetic', order,
                                RosterStatus.CONFIRMED, self.clock.now()),
                )])
                message_id = 20000 + index * 10 + order
                self.assertTrue(await self.repo.upsert_tier_message(
                    TierMessageBinding(message_id, session.id, user_id, self.clock.now()),
                    TierSubmission(session.id, user_id, 'synthetic', 'SYNTHETIC-TIER',
                                   message_id, self.clock.now(), self.clock.now()),
                ))

        await SafetyService(self.repo, self.clock, 100).record(
            300, WarningReason.NO_SHOW, 200, 500,
        )

    async def asyncTearDown(self):
        await self.repo.close()
        self.temp.cleanup()

    async def open_repo(self, path):
        repo = SQLiteMatchRepository(path)
        await repo.initialize(self.migration)
        return repo

    async def assert_erased_and_scoped(self, repo):
        for session in self.sessions[:2]:
            self.assertIsNone(await repo.get_tier_submission(session.id, 300))
            self.assertTrue(await repo.tier_collection_disabled(session.id, 300))
            self.assertEqual(len(await repo.load_roster(session.id)), 2)
            self.assertIsNotNone(await repo.get_tier_submission(session.id, 301))
            result = await TierCollector(repo, self.clock).submit_activity(
                guild_id=100, channel_id=session.tier_channel_id, discord_user_id=300,
                discord_display_name='synthetic', raw_content='SHOULD-NOT-RETURN',
                discord_message_id=40000, activity_at=self.clock.now(),
                reply_to_message_id=session.tier_anchor_message_id,
            )
            self.assertEqual(result.status, TierRouteStatus.COLLECTION_DISABLED)
        self.assertIsNotNone(await repo.get_tier_submission(self.sessions[2].id, 300))
        self.assertEqual((await repo.warning_summary(100, 300)).total, 1)
        for message_id in (20001, 20011):
            self.assertIsNone(await repo.get_tier_binding(message_id))
        self.assertEqual([tuple(row) for row in await repo._fetchall('PRAGMA integrity_check')], [('ok',)])
        self.assertEqual(await repo._fetchall('PRAGMA foreign_key_check'), [])

    async def test_confirmation_and_post_request_backup_survive_restart(self):
        service = SafetyService(self.repo, self.clock, 100)
        cog = SafetyCog(MagicMock(), make_config(), service)
        view = TierErasureConfirmation(cog, 300)
        interaction = SimpleNamespace(
            guild_id=100, user=SimpleNamespace(id=300),
            response=SimpleNamespace(defer=AsyncMock()),
            edit_original_response=AsyncMock(),
        )
        await view.erase.callback(interaction)
        self.assertTrue(interaction.response.defer.await_args.kwargs['ephemeral'])
        self.assertIn('기존 백업과 로그는 이 버튼으로 삭제되지 않습니다',
                      interaction.edit_original_response.await_args.kwargs['content'])
        await self.assert_erased_and_scoped(self.repo)
        await self.repo.close()
        backup = create_backup(self.live, self.root / 'after-request')
        self.repo = await self.open_repo(self.path)
        restored = await self.open_repo(backup / 'data/owkr-gather-bot.sqlite3')
        try:
            await self.assert_erased_and_scoped(restored)
        finally:
            await restored.close()

    async def test_pre_request_vm_and_mac_copies_require_replay_before_start(self):
        await self.repo.close()
        vm = create_backup(self.live, self.root / 'vm-backups')
        mac = self.root / 'mac-backups' / vm.name
        shutil.copytree(vm, mac)
        self.repo = await self.open_repo(self.path)
        await SafetyService(self.repo, self.clock, 100).erase_tiers(300)
        await self.assert_erased_and_scoped(self.repo)
        for index, bundle in enumerate((vm, mac)):
            source = bundle / 'data/owkr-gather-bot.sqlite3'
            checksum = hashlib.sha256(source.read_bytes()).hexdigest()
            restored_path = self.root / f'restore-{index}/owkr-gather-bot.sqlite3'
            restored_path.parent.mkdir()
            shutil.copyfile(source, restored_path)
            restored = await self.open_repo(restored_path)
            try:
                # An old backup is intentionally stale; startup alone does not replay requests.
                self.assertIsNotNone(await restored.get_tier_submission('match-0', 300))
                self.assertFalse(await restored.tier_collection_disabled('match-0', 300))
                service = SafetyService(restored, self.clock, 100)
                await service.erase_tiers(300)
                await service.erase_tiers(300)
                await restored.apply_mutations([TierUpsertMutation(
                    'match-0', 300, 'synthetic', 'SHOULD-NOT-RETURN',
                    20001, self.clock.now(), self.clock.now(),
                )])
            finally:
                await restored.close()
            restored = await self.open_repo(restored_path)
            try:
                await self.assert_erased_and_scoped(restored)
            finally:
                await restored.close()
            self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(), checksum)
            with sqlite3.connect(f'{source.as_uri()}?mode=ro', uri=True) as db:
                self.assertEqual(db.execute(
                    'SELECT COUNT(*) FROM tier_submissions WHERE discord_user_id = 300'
                ).fetchone()[0], 3)

    async def test_expired_pre_request_copies_are_removed_only_after_seven_days(self):
        await self.repo.close()
        vm = create_backup(self.live, self.root / 'vm-backups')
        mac = self.root / 'mac-backups' / vm.name
        shutil.copytree(vm, mac)
        self.repo = await self.open_repo(self.path)
        await SafetyService(self.repo, self.clock, 100).erase_tiers(300)
        created = datetime.fromisoformat(json.loads((vm / 'manifest.json').read_text())['created_at'])
        for bundle in (vm, mac):
            self.assertEqual(prune_backups(
                bundle.parent, now=created + timedelta(days=7, seconds=-1), apply=True,
            ), [])
            self.assertTrue(bundle.exists())
            self.assertEqual(prune_backups(
                bundle.parent, now=created + timedelta(days=7, seconds=1),
            ), [bundle])
            self.assertTrue(bundle.exists())
            self.assertEqual(prune_backups(
                bundle.parent, now=created + timedelta(days=7, seconds=1), apply=True,
            ), [bundle])
            self.assertFalse(bundle.exists())
        await self.assert_erased_and_scoped(self.repo)
