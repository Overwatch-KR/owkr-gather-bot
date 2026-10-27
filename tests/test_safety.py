from __future__ import annotations

import asyncio
import tempfile
import unittest
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord

from src.adapters.safety_adapter import (
    SafetyCog, TierErasureConfirmation, WarningConfirmation, warning_authorization_error,
)
from src.application.safety import SafetyService
from src.application.tier_collector import TierCollector, TierRouteStatus
from src.domain.models import (
    MatchStatus, ReactionAction, ReactionMutation, RosterEntry, RosterStatus,
    TierMessageBinding, TierSubmission, TierUpsertMutation,
)
from src.domain.warnings import WarningReason
from src.infrastructure.sqlite_repository import SQLiteMatchRepository
from tests.helpers import MutableClock, make_config, make_session


class SafetyTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'test.sqlite3'
        self.migration = Path(__file__).resolve().parents[1] / 'migrations/001_initial.sql'
        self.repo = SQLiteMatchRepository(self.path)
        await self.repo.initialize(self.migration)
        self.now = make_session().created_at
        self.clock = MutableClock(self.now)
        self.service = SafetyService(self.repo, self.clock, 100)
        self.config = replace(make_config(), channels=replace(make_config().channels, warning=107))

    async def asyncTearDown(self):
        await self.repo.close()
        self.temp.cleanup()

    def interaction(self, user_id=200, channel_id=107):
        return SimpleNamespace(
            id=10001, guild_id=100, channel_id=channel_id,
            user=SimpleNamespace(id=user_id, roles=[]),
            response=SimpleNamespace(send_message=AsyncMock(), defer=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()), edit_original_response=AsyncMock(),
        )

    async def make_tier(self):
        session = make_session(status=MatchStatus.FULL, completion_notified_at=self.now)
        await self.repo.create_match(session)
        entry = RosterEntry(session.id, 300, 'participant', 1, RosterStatus.CONFIRMED, self.now)
        await self.repo.apply_mutations([ReactionMutation(
            match_id=session.id, discord_user_id=300, action=ReactionAction.ADD,
            received_at=self.now, arrival_seq=1, outcome='CONFIRMED', next_arrival_seq=2,
            match_status=MatchStatus.FULL, roster_entry=entry,
        )])
        binding = TierMessageBinding(2000, session.id, 300, self.now)
        submission = TierSubmission(session.id, 300, 'participant', 'private tier', 2000, self.now, self.now)
        self.assertTrue(await self.repo.upsert_tier_message(binding, submission))
        return session, binding, submission

    async def test_warning_retry_concurrency_categories_revoke_and_restart(self):
        await asyncio.gather(*[
            self.service.record(300, WarningReason.NO_SHOW, 200, 500) for _ in range(5)
        ])
        await self.service.record(300, WarningReason.BAD_MANNERS, 200, 501)
        summary = await self.service.summary(300)
        self.assertEqual((summary.no_show, summary.bad_manners, summary.total), (1, 1, 2))
        warning_id = await self.repo.latest_warning(100, 300, WarningReason.NO_SHOW)
        self.assertFalse(await self.repo.revoke_warning(999, 300, warning_id, self.now))
        self.assertTrue(await self.service.revoke(300, warning_id))
        self.assertFalse(await self.service.revoke(300, warning_id))
        # Retried recording of a revoked request never resurrects the warning.
        await self.service.record(300, WarningReason.NO_SHOW, 200, 500)
        await self.repo.close()
        self.repo = SQLiteMatchRepository(self.path)
        await self.repo.initialize(self.migration)
        self.assertEqual((await self.repo.warning_summary(100, 300)).total, 1)

    async def test_warning_no_time_expiry_but_departure_and_rejoin_clear(self):
        await self.service.record(300, WarningReason.NO_SHOW, 200, 1)
        await self.service.record(301, WarningReason.BAD_MANNERS, 200, 2)
        self.clock.value += timedelta(days=1000)
        await self.service.purge_closed()
        self.assertEqual((await self.service.summary(300)).total, 1)
        await self.repo.purge_previous_membership(100, 300, self.now + timedelta(days=1))
        self.assertEqual((await self.service.summary(300)).total, 0)
        self.assertEqual((await self.service.summary(301)).total, 1)
        await self.service.forget_departed(301)
        await self.service.forget_departed(301)
        self.assertEqual(await self.repo.warning_user_ids(100), [])

    async def test_terminal_cleanup_cascades_and_preserves_other_guild_and_live_match(self):
        session, _, _ = await self.make_tier()
        other = replace(make_session(match_id='live', match_code='B7K2'), announcement_message_id=1001)
        await self.repo.create_match(other)
        await self.repo.connection.execute(
            'UPDATE roster_entries SET conflict_match_id = ? WHERE match_id = ?',
            (other.id, session.id),
        )
        await self.repo.connection.commit()
        foreign = replace(make_session(match_id='foreign', match_code='C7K2'),
                          guild_id=999, announcement_message_id=1002, status=MatchStatus.CANCELED)
        await self.repo.create_match(foreign)
        await self.repo.cancel_match(other.id, self.now)
        self.assertEqual(await self.service.purge_closed(), 1)
        self.assertIsNotNone(await self.repo.get_match(session.id))
        roster = await self.repo.load_roster(session.id)
        self.assertIsNone(roster[0].conflict_match_id)
        await self.repo.mark_started(session.id, session.starts_at)
        self.assertEqual(await self.service.purge_closed(), 1)
        self.assertEqual(await self.service.purge_closed(), 0)
        for table in ('roster_entries', 'reaction_events', 'tier_submissions',
                      'tier_message_bindings', 'notification_outbox', 'tier_collection_opt_outs'):
            rows = await self.repo._fetchall(f'SELECT * FROM {table} WHERE match_id = ?', (session.id,))
            self.assertEqual(rows, [])
        self.assertIsNotNone(await self.repo.get_match('foreign'))
        self.assertEqual(await self.repo._fetchall('PRAGMA foreign_key_check'), [])

    async def test_tier_erasure_blocks_inflight_upserts_and_restart_history(self):
        session, binding, submission = await self.make_tier()
        await self.service.erase_tiers(300)
        await self.service.erase_tiers(300)
        self.assertIsNone(await self.repo.get_tier_submission(session.id, 300))
        self.assertIsNone(await self.repo.get_tier_binding(2000))
        self.assertEqual(len(await self.repo.load_roster(session.id)), 1)
        self.assertFalse(await self.repo.upsert_tier_message(binding, submission))
        await self.repo.apply_mutations([TierUpsertMutation(
            session.id, 300, 'participant', 'must not return', 2000, self.now, self.now
        )])
        await self.repo.close()
        self.repo = SQLiteMatchRepository(self.path)
        await self.repo.initialize(self.migration)
        self.assertFalse(await self.repo.upsert_tier_message(binding, submission))
        self.assertIsNone(await self.repo.get_tier_submission(session.id, 300))

    async def test_erasure_feedback_and_other_participants_are_preserved(self):
        session, _, _ = await self.make_tier()
        await self.repo.connection.execute(
            'UPDATE match_sessions SET tier_anchor_message_id = ? WHERE id = ?',
            (9000, session.id),
        )
        await self.repo.connection.commit()
        await self.service.erase_tiers(301)
        self.assertIsNotNone(await self.repo.get_tier_submission(session.id, 300))
        await self.service.erase_tiers(300)
        result = await TierCollector(self.repo, self.clock).submit_activity(
            guild_id=100, channel_id=103, discord_user_id=300,
            discord_display_name='participant', raw_content='new tier',
            discord_message_id=2001, activity_at=self.now, reply_to_message_id=9000,
        )
        self.assertEqual(result.status, TierRouteStatus.COLLECTION_DISABLED)

    async def test_maintenance_purges_terminal_matches_without_any_warnings(self):
        session = make_session(status=MatchStatus.CANCELED)
        await self.repo.create_match(session)
        cog = SafetyCog(MagicMock(), self.config, self.service)
        await cog.maintenance_once(check_members=False)
        self.assertIsNone(await self.repo.get_match(session.id))

    async def test_privacy_registration_and_intents_need_no_member_access(self):
        from src.adapters.discord_adapter import GatherBot

        bot = GatherBot(
            config=self.config, repository=self.repo, writer=MagicMock(),
            coordinator=MagicMock(), tier_collector=MagicMock(), scheduler=MagicMock(),
            clock=self.clock, template_path=Path('templates/recruitment.txt'),
            recruitment_complete_template_path=Path('templates/recruitment_complete.txt'),
            migration_path=self.migration,
        )
        self.assertFalse(bot.intents.members)
        self.assertFalse(bot.intents.presences)
        self.assertTrue(bot.intents.message_content)
        commands = {command.name for command in SafetyCog.__cog_app_commands__}
        self.assertEqual(commands, {'경고', '개인정보'})
        interaction = self.interaction(user_id=300, channel_id=103)
        cog = SafetyCog(bot, self.config, self.service)
        await cog.privacy_command.callback(cog, interaction)
        self.assertTrue(interaction.response.send_message.await_args.kwargs['ephemeral'])
        self.assertIn('준비 중', interaction.response.send_message.await_args.args[0])

    async def test_membership_errors_preserve_history_and_unknown_member_deletes(self):
        await self.service.record(300, WarningReason.NO_SHOW, 200, 1)
        bot = SimpleNamespace(get_guild=lambda _: SimpleNamespace(fetch_member=AsyncMock()))
        guild = SimpleNamespace(fetch_member=AsyncMock())
        bot.get_guild = lambda _: guild
        cog = SafetyCog(bot, self.config, self.service)
        response = SimpleNamespace(status=404, reason='Not Found')
        for error in (discord.NotFound(response, {'code': 10004, 'message': 'Unknown Guild'}),
                      discord.Forbidden(SimpleNamespace(status=403, reason='Forbidden'), 'denied')):
            guild.fetch_member.side_effect = error
            await cog.maintenance_once(check_members=True)
            self.assertEqual((await self.service.summary(300)).total, 1)
        guild.fetch_member.side_effect = discord.NotFound(response, {'code': 10007, 'message': 'Unknown Member'})
        await cog.maintenance_once(check_members=True)
        self.assertEqual((await self.service.summary(300)).total, 0)

    async def test_authorization_and_confirmation_recheck(self):
        interaction = self.interaction()
        self.assertIsNone(warning_authorization_error(self.config, interaction))
        self.assertIsNotNone(warning_authorization_error(make_config(), interaction))
        self.assertIsNotNone(warning_authorization_error(self.config, self.interaction(channel_id=101)))
        self.assertIsNotNone(warning_authorization_error(self.config, self.interaction(user_id=999)))
        interaction.guild_id = 999
        self.assertIsNotNone(warning_authorization_error(self.config, interaction))
        cog = SafetyCog(MagicMock(), self.config, self.service)
        view = WarningConfirmation(cog, self.interaction(), SimpleNamespace(id=300, joined_at=self.now),
                                   '기록', WarningReason.NO_SHOW, None)
        self.assertFalse(await view.interaction_check(self.interaction(user_id=999)))
        self.assertFalse(await view.interaction_check(self.interaction(channel_id=101)))
        self.assertEqual((await self.service.summary(300)).total, 0)
        cog.current_member = AsyncMock(return_value=SimpleNamespace(id=300, joined_at=self.now))
        await asyncio.gather(view.confirm.callback(self.interaction()), view.confirm.callback(self.interaction()))
        self.assertEqual((await self.service.summary(300)).total, 1)

    async def test_rejoined_target_rejects_old_confirmation(self):
        cog = SafetyCog(MagicMock(), self.config, self.service)
        cog.current_member = AsyncMock(return_value=SimpleNamespace(id=300, joined_at=self.now + timedelta(seconds=1)))
        view = WarningConfirmation(cog, self.interaction(), SimpleNamespace(id=300, joined_at=self.now),
                                   '기록', WarningReason.NO_SHOW, None)
        await view.confirm.callback(self.interaction())
        self.assertEqual((await self.service.summary(300)).total, 0)

    async def test_private_warning_feedback_and_tier_erasure_ownership(self):
        cog = SafetyCog(MagicMock(), self.config, self.service)
        cog.current_member = AsyncMock(return_value=SimpleNamespace(id=300, joined_at=self.now))
        interaction = self.interaction()
        await cog.warning_command.callback(cog, interaction, SimpleNamespace(id=300, bot=False), '기록', '노쇼')
        kwargs = interaction.followup.send.await_args.kwargs
        self.assertTrue(kwargs['ephemeral'])
        self.assertEqual(kwargs['allowed_mentions'].to_dict()['parse'], [])
        self.assertEqual((await self.service.summary(300)).total, 0)
        view = TierErasureConfirmation(cog, 300)
        self.assertFalse(await view.interaction_check(self.interaction(user_id=301)))
        self.assertTrue(await view.interaction_check(self.interaction(user_id=300)))
