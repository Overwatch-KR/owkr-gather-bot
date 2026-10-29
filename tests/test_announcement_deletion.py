from __future__ import annotations

import asyncio
import unittest
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord

from src.adapters.discord.cancellation import DiscordCancellationTransport
from src.adapters.discord.commands import GatherCog
from src.application.tier_collector import TierRouteResult, TierRouteStatus
from src.domain.models import CancellationJob, MatchStatus
from tests.helpers import make_config, make_session
from tests.test_auto_cancellation import CancellationFixture


def missing():
    return discord.NotFound(SimpleNamespace(status=404, reason='Not Found'), 'missing')


def forbidden():
    return discord.Forbidden(SimpleNamespace(status=403, reason='Forbidden'), 'forbidden')


class AnnouncementDeletionTest(CancellationFixture):
    def cog(self):
        cog = object.__new__(GatherCog)
        cog._config = make_config()
        cog._repository = self.repo
        cog._coordinator = self.coordinator
        cog._clock = self.clock
        cog._tier_collector = SimpleNamespace(submit_delete=AsyncMock(return_value=SimpleNamespace(accepted=False)))
        cog._announcement_reconcile_lock = asyncio.Lock()
        return cog

    async def test_delete_cancels_only_original_and_duplicates_are_harmless(self):
        first = await self.create(deadline=False)
        second = await self.create(1002, deadline=False)
        cog = self.cog()
        for _ in range(2):
            await cog.on_raw_message_delete(SimpleNamespace(guild_id=100, channel_id=102, message_id=1001))
        self.assertEqual(first.status, MatchStatus.CANCELED)
        self.assertEqual(second.status, MatchStatus.RECRUITING)
        self.assertEqual([job.match_id for job in await self.repo.pending_cancellations(100)], [first.id])
        self.assertIsNotNone(self.coordinator.actor_for_announcement(1002))

    async def test_bulk_delete_and_substitute_or_wrong_channel_do_not_cancel_other_sessions(self):
        first = await self.create(deadline=False)
        second = await self.create(1002, deadline=False)
        self.coordinator._substitute_match_ids[2002] = second.id
        cog = self.cog()
        await cog.on_raw_message_delete(SimpleNamespace(guild_id=999, channel_id=102, message_id=1002))
        await cog.on_raw_message_delete(SimpleNamespace(guild_id=100, channel_id=999, message_id=1002))
        await cog.on_raw_bulk_message_delete(SimpleNamespace(guild_id=100, channel_id=102, message_ids={1001, 2002, 99999}))
        self.assertEqual(first.status, MatchStatus.CANCELED)
        self.assertEqual(second.status, MatchStatus.RECRUITING)
        self.assertEqual(len(await self.repo.pending_cancellations(100)), 1)

    async def test_start_boundary_ignores_announcement_deletion(self):
        session = await self.create(deadline=False)
        self.clock.value = session.starts_at
        await self.cog().on_raw_message_delete(SimpleNamespace(guild_id=100, channel_id=102, message_id=1001))
        self.assertEqual(await self.repo.pending_cancellations(100), [])
        await self.scheduler.tick()
        self.assertEqual(session.status, MatchStatus.STARTED)

    async def test_offline_deletion_reconciles_after_restart_but_permission_error_keeps_match(self):
        first = await self.create(deadline=False)
        second = await self.create(1002, deadline=False)
        await self.close_runtime()
        await self.open_runtime()
        cog = self.cog()
        # UUID tie-breaks can change recovery order; failures belong to messages, not calls.
        errors = {first.announcement_message_id: missing(), second.announcement_message_id: forbidden()}

        async def fetch_message(message_id):
            self.assertIn(message_id, errors)
            raise errors[message_id]

        channel = SimpleNamespace(fetch_message=AsyncMock(side_effect=fetch_message))
        cog.bot = SimpleNamespace(get_channel=lambda channel_id: channel)
        await cog.on_ready()
        self.assertEqual((await self.repo.get_match(first.id)).status, MatchStatus.CANCELED)
        self.assertEqual((await self.repo.get_match(second.id)).status, MatchStatus.RECRUITING)
        channel.fetch_message.side_effect = None
        await cog.on_resumed()
        self.assertEqual(len(await self.repo.pending_cancellations(100)), 1)

    async def test_tier_anchor_delete_keeps_recreation_and_does_not_cancel_match(self):
        session = await self.create(deadline=False)
        cog = self.cog()
        cog._repository = SimpleNamespace(request_tier_anchor_recreation=AsyncMock(return_value=session))
        self.coordinator._tier_anchor_match_ids[3001] = session.id
        await cog.on_raw_message_delete(SimpleNamespace(guild_id=100, channel_id=103, message_id=3001))
        cog._repository.request_tier_anchor_recreation.assert_awaited_once()
        cog._tier_collector.submit_delete.assert_not_awaited()
        self.assertEqual(session.status, MatchStatus.RECRUITING)


class CancellationTransportTest(unittest.IsolatedAsyncioTestCase):
    async def test_cleanup_missing_messages_and_notification_never_pings_or_seeds(self):
        session = make_session()
        job = CancellationJob(session.id, 100, 102, 1001, 103, 3001, session.starts_at, '@everyone', (2001,))
        message = SimpleNamespace(delete=AsyncMock())
        channel = SimpleNamespace(fetch_message=AsyncMock(side_effect=[missing(), message, message]), send=AsyncMock())
        bot = SimpleNamespace(wait_until_ready=AsyncMock(), get_channel=lambda channel_id: channel)
        transport = DiscordCancellationTransport(bot)
        self.assertTrue(await transport.cleanup(job))
        self.assertEqual(message.delete.await_count, 2)
        self.assertTrue(await transport.notify(job))
        kwargs = channel.send.await_args.kwargs
        self.assertIn('__**오후 8시**__', kwargs['content'])
        self.assertNotIn(session.match_code, kwargs['content'])
        self.assertEqual(kwargs['allowed_mentions'].to_dict().get('parse', []), [])

    async def test_cleanup_permission_error_is_retryable(self):
        session = make_session()
        job = CancellationJob(session.id, 100, 102, 1001, 103, None, session.starts_at, None)
        channel = SimpleNamespace(fetch_message=AsyncMock(side_effect=forbidden()))
        bot = SimpleNamespace(wait_until_ready=AsyncMock(), get_channel=lambda channel_id: channel)
        self.assertFalse(await DiscordCancellationTransport(bot).cleanup(job))
