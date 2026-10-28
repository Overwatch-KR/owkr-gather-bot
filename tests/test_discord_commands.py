from __future__ import annotations

import unittest
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
import asyncio

import discord

from src.adapters.discord_adapter import (
    CancelMatchConfirmationView,
    CreateMatchConfirmationView,
    DiscordNotificationTransport,
    EditMatchModal,
    GatherCog,
    ManagementCommandCheckFailure,
    _management_authorization_error,
    _normalized_command_payload,
    match_status_label,
)
from src.application.coordinator import LobbyCapacityExceeded
from src.application.rendering import (
    RenderedNotification,
)
from src.application.tier_collector import TierRouteResult, TierRouteStatus
from src.adapters.discord.transport import DiscordRecruitmentPublisher
from src.domain.models import RosterStatus
from tests.helpers import make_config, make_session


class DiscordApplicationCommandTest(unittest.IsolatedAsyncioTestCase):
    async def test_recruitment_publisher_limits_mentions_to_the_configured_role(self):
        config = make_config()
        config = replace(config, defaults=replace(config.defaults, recruitment_role_id=777))
        channel = MagicMock(spec=discord.TextChannel)
        message = MagicMock(spec=discord.Message)
        message.id = 9001
        message.add_reaction = AsyncMock()
        channel.send = AsyncMock(return_value=message)
        bot = MagicMock()
        bot.get_channel.return_value = channel
        renderer = MagicMock()
        renderer.render.return_value = "@everyone <@888> <@&999>"
        published = await DiscordRecruitmentPublisher(bot, config, renderer).publish(make_session())
        await published.add_seed_reaction()
        allowed = channel.send.await_args.kwargs["allowed_mentions"].to_dict()
        self.assertEqual([int(role) for role in allowed["roles"]], [777])
        self.assertNotIn("everyone", allowed.get("parse", []))
        self.assertNotIn("users", allowed.get("parse", []))
        self.assertEqual(allowed.get("users", []), [])
        self.assertEqual(published.message_id, 9001)
        message.add_reaction.assert_awaited_once_with("✅")

    async def test_unloading_cog_stops_reaction_reconciliation(self):
        cog = object.__new__(GatherCog)
        task = asyncio.create_task(asyncio.Event().wait())
        cog._seed_reaction_tasks = {1000: task}
        cog._seed_reaction_versions = {1000: 1}
        await cog.cog_unload()
        self.assertTrue(task.cancelled())
        self.assertEqual(cog._seed_reaction_tasks, {})
        self.assertEqual(cog._seed_reaction_versions, {})

    async def test_lobby_reminder_mentions_only_users_outside_voice_channel(
        self,
    ) -> None:
        announcement = MagicMock(spec=discord.TextChannel)
        sent_message = SimpleNamespace(id=9001)
        announcement.send = AsyncMock(return_value=sent_message)
        bot = MagicMock()
        bot.intents.voice_states = True
        bot.get_channel.return_value = announcement
        transport = DiscordNotificationTransport(bot)
        transport._voice_channel_member_ids = AsyncMock(return_value={1})
        rendered = RenderedNotification(
            content="앞\n<@1> <@2>\n뒤",
            allowed_user_ids=(1, 2),
            voice_channel_id=105,
            content_before_mentions="앞\n",
            content_after_mentions="\n뒤",
        )

        message_id = await transport.send(102, rendered)

        self.assertEqual(message_id, 9001)
        announcement.send.assert_awaited_once()
        args, kwargs = announcement.send.await_args
        self.assertEqual(args[0], "앞\n<@2>\n뒤")
        allowed = kwargs["allowed_mentions"].to_dict()
        self.assertEqual(
            [int(user_id) for user_id in allowed["users"]],
            [2],
        )
        self.assertNotIn("everyone", allowed.get("parse", []))
        self.assertNotIn("roles", allowed.get("parse", []))

    async def test_lobby_reminder_sends_nothing_when_everyone_is_present(
        self,
    ) -> None:
        announcement = MagicMock(spec=discord.TextChannel)
        announcement.send = AsyncMock()
        bot = MagicMock()
        bot.intents.voice_states = True
        bot.get_channel.return_value = announcement
        transport = DiscordNotificationTransport(bot)
        transport._voice_channel_member_ids = AsyncMock(return_value={1, 2})
        rendered = RenderedNotification(
            content="앞\n<@1> <@2>\n뒤",
            allowed_user_ids=(1, 2),
            voice_channel_id=105,
            content_before_mentions="앞\n",
            content_after_mentions="\n뒤",
        )

        message_id = await transport.send(102, rendered)

        self.assertIsNone(message_id)
        announcement.send.assert_not_awaited()

    def test_command_payload_normalization_ignores_discord_managed_defaults(self) -> None:
        local = {
            "name": "내전",
            "description": "설명",
            "type": 1,
            "options": [
                {
                    "type": 3,
                    "name": "모드",
                    "description": "선택",
                    "required": False,
                    "autocomplete": True,
                }
            ],
            "dm_permission": False,
            "contexts": None,
        }
        remote = {
            "id": "123",
            "application_id": "456",
            "version": "789",
            "guild_id": "100",
            "name": "내전",
            "description": "설명",
            "type": 1,
            "options": [
                {
                    "type": 3,
                    "name": "모드",
                    "description": "선택",
                    "autocomplete": True,
                }
            ],
        }

        self.assertEqual(
            _normalized_command_payload(local),
            _normalized_command_payload(remote),
        )

    def test_expected_slash_commands_and_match_options_are_registered(self) -> None:
        commands = {command.name: command for command in GatherCog.__cog_app_commands__}

        self.assertEqual(
            set(commands),
            {
                "내전",
                "티어현황",
                "내전대타",
                "내전취소",
            },
        )
        for command in commands.values():
            self.assertIn(
                "management_command_check",
                [getattr(check, "__name__", "") for check in command.checks],
            )
        match_parameters = {parameter.name: parameter for parameter in commands["내전"].parameters}
        self.assertTrue(match_parameters["시간"].required)
        self.assertFalse(match_parameters["모드"].required)
        self.assertTrue(match_parameters["시간"].autocomplete)
        self.assertTrue(match_parameters["모드"].autocomplete)
        self.assertFalse(commands["티어현황"].parameters[0].required)
        substitute_parameters = {
            parameter.name: parameter
            for parameter in commands["내전대타"].parameters
        }
        self.assertEqual(set(substitute_parameters), {"내전"})
        self.assertTrue(substitute_parameters["내전"].required)
        self.assertTrue(commands["내전취소"].parameters[0].required)
        for name in ("티어현황", "내전대타", "내전취소"):
            self.assertTrue(commands[name].parameters[0].autocomplete)


    async def test_confirmation_views_use_plain_manager_labels(self) -> None:
        interaction = MagicMock(spec=discord.Interaction)
        interaction.id = 999
        interaction.channel_id = 101
        interaction.user.id = 200
        session = make_session()
        parsed = SimpleNamespace(
            starts_at=session.starts_at,
            tier_deadline_at=session.tier_deadline_at,
            lobby_at=session.lobby_at,
            mode=session.mode,
        )
        cog = object.__new__(GatherCog)

        create_view = CreateMatchConfirmationView(cog, interaction, parsed)
        cancel_view = CancelMatchConfirmationView(cog, interaction, session)

        self.assertEqual(
            [child.label for child in create_view.children],
            ["생성", "시간·모드·취소 시각 수정", "취소"],
        )
        self.assertEqual(
            [child.label for child in cancel_view.children],
            ["내전 취소", "돌아가기"],
        )
        self.assertEqual(match_status_label(session.status), "모집 중")

    async def test_match_edit_modal_prefills_and_refreshes_confirmation(
        self,
    ) -> None:
        original = MagicMock(spec=discord.Interaction)
        original.user.id = 200
        original.edit_original_response = AsyncMock()
        session = make_session()
        parsed = SimpleNamespace(
            starts_at=session.starts_at,
            tier_deadline_at=session.tier_deadline_at,
            lobby_at=session.lobby_at,
            mode="일반 내전", recruitment_cancel_at=None,
        )
        updated = SimpleNamespace(
            starts_at=session.starts_at + timedelta(hours=1),
            tier_deadline_at=session.tier_deadline_at
            + timedelta(hours=1),
            lobby_at=session.lobby_at + timedelta(hours=1),
            mode="6ㄷ6클래식", recruitment_cancel_at=None,
        )
        cog = object.__new__(GatherCog)
        cog._config = make_config()
        cog._parser = SimpleNamespace(parse=MagicMock(return_value=updated),
            with_cancellation_time=MagicMock(return_value=updated))
        cog._clock = SimpleNamespace(now=lambda: session.created_at)
        cog._coordinator = SimpleNamespace(
            lobby_assignment_for=lambda starts_at: (105, "대기실 1번")
        )
        view = CreateMatchConfirmationView(cog, original, parsed)
        modal = EditMatchModal(view)
        modal.time_input._value = "오후 9시"
        modal.mode_input._value = "6ㄷ6클래식"
        interaction = SimpleNamespace(
            user=SimpleNamespace(id=200),
            response=SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock()),
        )

        await modal.on_submit(interaction)

        cog._parser.parse.assert_called_once_with(
            "오후 9시 6ㄷ6클래식",
            now=session.created_at,
            default_mode=None,
        )
        self.assertIs(view.parsed, updated)
        original.edit_original_response.assert_awaited_once()
        refreshed = original.edit_original_response.await_args.kwargs["content"]
        self.assertIn("6ㄷ6클래식", refreshed)

    async def test_create_confirmation_creates_match_and_shows_announcement_link(
        self,
    ) -> None:
        original = MagicMock(spec=discord.Interaction)
        original.id = 999
        original.channel_id = 101
        original.user.id = 200
        session = make_session()
        parsed = SimpleNamespace(
            starts_at=session.starts_at,
            tier_deadline_at=session.tier_deadline_at,
            lobby_at=session.lobby_at,
            mode=session.mode,
        )
        cog = object.__new__(GatherCog)
        cog.create_match = AsyncMock(return_value=session)
        view = CreateMatchConfirmationView(cog, original, parsed)
        interaction = SimpleNamespace(
            user=SimpleNamespace(id=200),
            response=SimpleNamespace(defer=AsyncMock()),
            edit_original_response=AsyncMock(),
        )

        await view.children[0].callback(interaction)

        cog.create_match.assert_awaited_once()
        self.assertEqual(
            cog.create_match.await_args.kwargs["source_request_id"],
            "999",
        )
        content = interaction.edit_original_response.await_args.kwargs["content"]
        self.assertIn("모집 공지 보기", content)
        self.assertIn("/100/102/1000", content)
        self.assertNotIn(session.id, content)

    async def test_create_confirmation_explains_lobby_capacity(self) -> None:
        original = MagicMock(spec=discord.Interaction)
        original.id = 999
        original.channel_id = 101
        original.user.id = 200
        session = make_session()
        parsed = SimpleNamespace(
            starts_at=session.starts_at,
            tier_deadline_at=session.tier_deadline_at,
            lobby_at=session.lobby_at,
            mode=session.mode,
        )
        cog = object.__new__(GatherCog)
        cog.create_match = AsyncMock(side_effect=LobbyCapacityExceeded())
        view = CreateMatchConfirmationView(cog, original, parsed)
        interaction = SimpleNamespace(
            user=SimpleNamespace(id=200),
            response=SimpleNamespace(defer=AsyncMock()),
            edit_original_response=AsyncMock(),
        )

        await view.children[0].callback(interaction)

        content = interaction.edit_original_response.await_args.kwargs["content"]
        self.assertIn("대기실이 모두 예약", content)
        self.assertIn("시간을 변경", content)

    async def test_full_lobbies_still_allow_editing_creation_preview(self) -> None:
        session = make_session()
        cog = object.__new__(GatherCog)
        cog._config = make_config()
        cog._coordinator = SimpleNamespace(
            lobby_assignment_for=MagicMock(side_effect=LobbyCapacityExceeded())
        )
        parsed = SimpleNamespace(
            starts_at=session.starts_at, tier_deadline_at=session.tier_deadline_at,
            lobby_at=session.lobby_at, mode=session.mode, recruitment_cancel_at=None,
        )
        view = CreateMatchConfirmationView(cog, SimpleNamespace(user=SimpleNamespace(id=200)), parsed)
        self.assertIn("현재 빈 대기실 없음", view.content())
        self.assertIn("시간을 변경", view.content())
        self.assertIn("시간·모드·취소 시각 수정", [child.label for child in view.children])

    async def test_cancel_confirmation_retains_start_time_after_cleanup(
        self,
    ) -> None:
        original = MagicMock(spec=discord.Interaction)
        original.user.id = 200
        original.delete_original_response = AsyncMock()
        session = make_session()
        cog = object.__new__(GatherCog)
        cog._coordinator = SimpleNamespace(
            cancel_match=AsyncMock(return_value=session)
        )
        cog._replace_announcement_with_cancellation = AsyncMock(return_value=(True, True))
        cog._delete_tier_anchor_message = AsyncMock(return_value=True)
        view = CancelMatchConfirmationView(cog, original, session)
        interaction = SimpleNamespace(
            user=SimpleNamespace(id=200),
            response=SimpleNamespace(defer=AsyncMock()),
            edit_original_response=AsyncMock(),
        )

        await view.children[0].callback(interaction)

        cog._coordinator.cancel_match.assert_awaited_once_with(session.id)
        original.delete_original_response.assert_not_awaited()
        result = interaction.edit_original_response.await_args.kwargs
        self.assertIn("__**오후 8시**__", result["content"])
        self.assertIn("내전이 취소되었습니다", result["content"])
        self.assertIsNone(result["view"])
        self.assertEqual(result["allowed_mentions"].to_dict()["parse"], [])
        await view.children[0].callback(interaction)
        cog._coordinator.cancel_match.assert_awaited_once_with(session.id)

    async def test_cancel_notice_failure_keeps_cancellation_and_guides_manager(self) -> None:
        session = make_session()
        cog = object.__new__(GatherCog)
        cog._coordinator = SimpleNamespace(cancel_match=AsyncMock(return_value=session))
        cog._replace_announcement_with_cancellation = AsyncMock(return_value=(False, False))
        cog._delete_tier_anchor_message = AsyncMock(return_value=False)
        original = SimpleNamespace(user=SimpleNamespace(id=200))
        view = CancelMatchConfirmationView(cog, original, session)
        interaction = SimpleNamespace(
            user=SimpleNamespace(id=200),
            response=SimpleNamespace(defer=AsyncMock()),
            edit_original_response=AsyncMock(),
        )
        await view.children[0].callback(interaction)
        content = interaction.edit_original_response.await_args.kwargs["content"]
        self.assertIn("내전이 취소되었습니다", content)
        self.assertIn("공지 채널에 직접 안내", content)
        self.assertIn("남은 메시지를 직접 정리", content)
        cog._coordinator.cancel_match.assert_awaited_once_with(session.id)

    async def test_cancel_notice_failure_does_not_leave_original_reactions(self) -> None:
        message = MagicMock(spec=discord.Message)
        message.delete = AsyncMock()
        channel = MagicMock(spec=discord.TextChannel)
        channel.fetch_message = AsyncMock(return_value=message)
        channel.send = AsyncMock(side_effect=discord.Forbidden(MagicMock(), "forbidden"))
        cog = object.__new__(GatherCog)
        cog.bot = MagicMock()
        cog.bot.get_channel.return_value = channel
        self.assertEqual(await cog._replace_announcement_with_cancellation(make_session()), (False, True))
        message.delete.assert_awaited_once()

    async def test_failed_announcement_deletion_still_sends_cancellation(self) -> None:
        message = MagicMock(spec=discord.Message)
        message.delete = AsyncMock(side_effect=discord.Forbidden(MagicMock(), "forbidden"))
        channel = MagicMock(spec=discord.TextChannel)
        channel.fetch_message = AsyncMock(return_value=message)
        channel.send = AsyncMock()
        cog = object.__new__(GatherCog)
        cog.bot = MagicMock()
        cog.bot.get_channel.return_value = channel
        self.assertEqual(await cog._replace_announcement_with_cancellation(make_session()), (True, False))
        channel.send.assert_awaited_once()

    async def test_match_autocomplete_returns_short_code_instead_of_uuid(
        self,
    ) -> None:
        session = make_session(match_id="internal-uuid", match_code="A7K2")
        cog = object.__new__(GatherCog)
        cog._authorized = lambda interaction: True
        cog._coordinator = SimpleNamespace(active_sessions=(session,))
        cog._match_label = lambda selected: "7/16 오후 5시 · 상만 · 일반 내전 · A7K2"

        choices = await cog._match_autocomplete(SimpleNamespace(), "")

        self.assertEqual(choices[0].value, "A7K2")
        self.assertNotIn("internal-uuid", choices[0].name)

    def test_match_status_heading_only_shows_start_time(self) -> None:
        session = make_session(match_id="internal-uuid", match_code="A7K2")

        label = GatherCog._match_status_time_label(session)

        self.assertEqual(label, "오후 8시")
        self.assertNotIn("internal-uuid", label)
        self.assertNotIn("A7K2", label)
        self.assertNotIn("일반 내전", label)

    def test_management_check_accepts_configured_role_in_command_channel(self) -> None:
        config = replace(
            make_config(),
            admin_role_ids=frozenset({300}),
        )
        interaction = SimpleNamespace(
            guild_id=100,
            channel_id=101,
            user=SimpleNamespace(
                id=201,
                roles=[SimpleNamespace(id=300)],
            ),
        )

        self.assertIsNone(_management_authorization_error(config, interaction))

    def test_management_check_rejects_other_channels(self) -> None:
        config = make_config()
        interaction = SimpleNamespace(
            guild_id=100,
            channel_id=999,
            user=SimpleNamespace(id=200, roles=[]),
        )

        self.assertEqual(
            _management_authorization_error(config, interaction),
            "내전 관리 명령은 <#101> 채널에서만 사용할 수 있습니다.",
        )

    async def test_registered_management_check_rejects_unconfigured_user(self) -> None:
        command = next(
            command
            for command in GatherCog.__cog_app_commands__
            if command.name == "내전"
        )
        check = next(
            check
            for check in command.checks
            if getattr(check, "__name__", "") == "management_command_check"
        )
        interaction = SimpleNamespace(
            client=SimpleNamespace(app_config=make_config()),
            guild_id=100,
            channel_id=101,
            user=SimpleNamespace(id=999, roles=[]),
        )

        with self.assertRaises(ManagementCommandCheckFailure):
            await check(interaction)


    async def test_seed_reaction_is_restored_when_no_participants_remain(self) -> None:
        actor = MagicMock()
        actor.session.announcement_message_id = 1000
        actor.drain = AsyncMock()
        actor.active_user_count.return_value = 0
        coordinator = MagicMock()
        coordinator.actor_for_announcement.return_value = actor
        message = MagicMock(spec=discord.Message)
        message.add_reaction = AsyncMock()
        message.remove_reaction = AsyncMock()
        channel = MagicMock(spec=discord.TextChannel)
        channel.fetch_message = AsyncMock(return_value=message)
        bot = MagicMock()
        bot.user = discord.Object(id=999)
        bot.get_channel.return_value = channel
        cog = object.__new__(GatherCog)
        cog.bot = bot
        cog._coordinator = coordinator
        cog._seed_reaction_versions = {1000: 1}
        cog._seed_reaction_tasks = {}

        await cog._reconcile_bot_seed_reaction(102, 1000)

        message.add_reaction.assert_awaited_once_with("✅")
        message.remove_reaction.assert_not_awaited()

    async def test_tier_status_combines_roster_and_tier_without_public_mentions(self) -> None:
        session = make_session(completion_notified_at=make_session().created_at)
        cog = object.__new__(GatherCog)
        cog._select_active_match = AsyncMock(return_value=session)
        cog._match_label = lambda _: "오늘 내전"
        actor = MagicMock()
        actor.drain = AsyncMock()
        cog._coordinator = MagicMock()
        cog._coordinator.actor_for_match.return_value = actor
        cog._writer = SimpleNamespace(flush=AsyncMock())
        cog._repository = SimpleNamespace(
            load_roster=AsyncMock(return_value=[
                SimpleNamespace(status=RosterStatus.CONFIRMED, conflict_match_id=None),
                SimpleNamespace(status=RosterStatus.WAITLISTED, conflict_match_id="legacy", discord_display_name="대기자"),
            ]),
            get_tier_status=AsyncMock(return_value=[
                SimpleNamespace(discord_display_name="완료자", has_tier=True),
                SimpleNamespace(discord_display_name="미작성자", has_tier=False),
            ]),
        )
        interaction = SimpleNamespace(response=SimpleNamespace(send_message=AsyncMock()))
        await GatherCog.tier_status.callback(cog, interaction)
        content = interaction.response.send_message.await_args.args[0]
        self.assertIn("참가 · 1/10명", content)
        self.assertIn("대기 · 1명", content)
        self.assertIn("완료 · 완료자", content)
        self.assertIn("미작성 · 미작성자", content)
        self.assertIn("다른 내전과 충돌", content)
        self.assertTrue(interaction.response.send_message.await_args.kwargs['ephemeral'])
        self.assertEqual(interaction.response.send_message.await_args.kwargs['allowed_mentions'].to_dict()['parse'], [])

    async def test_tier_status_shows_roster_before_tier_collection_opens(self) -> None:
        session = make_session()
        cog = object.__new__(GatherCog)
        cog._select_active_match = AsyncMock(return_value=session)
        cog._match_label = lambda _: "오늘 내전"
        cog._coordinator = MagicMock()
        cog._coordinator.actor_for_match.return_value = None
        cog._writer = SimpleNamespace(flush=AsyncMock())
        cog._repository = SimpleNamespace(load_roster=AsyncMock(return_value=[]), get_tier_status=AsyncMock())
        interaction = SimpleNamespace(response=SimpleNamespace(send_message=AsyncMock()))
        await GatherCog.tier_status.callback(cog, interaction)
        content = interaction.response.send_message.await_args.args[0]
        self.assertIn("참가 · 0/10명", content)
        self.assertIn("모집 완료 공지가 전송된 뒤", content)
        cog._repository.get_tier_status.assert_not_awaited()

    async def test_seed_reaction_is_removed_while_participants_exist(self) -> None:
        actor = MagicMock()
        actor.session.announcement_message_id = 1000
        actor.drain = AsyncMock()
        actor.active_user_count.return_value = 1
        coordinator = MagicMock()
        coordinator.actor_for_announcement.return_value = actor
        message = MagicMock(spec=discord.Message)
        message.add_reaction = AsyncMock()
        message.remove_reaction = AsyncMock()
        channel = MagicMock(spec=discord.TextChannel)
        channel.fetch_message = AsyncMock(return_value=message)
        bot = MagicMock()
        bot.user = discord.Object(id=999)
        bot.get_channel.return_value = channel
        cog = object.__new__(GatherCog)
        cog.bot = bot
        cog._coordinator = coordinator
        cog._seed_reaction_versions = {1000: 1}
        cog._seed_reaction_tasks = {}

        await cog._reconcile_bot_seed_reaction(102, 1000)

        message.add_reaction.assert_not_awaited()
        message.remove_reaction.assert_awaited_once_with("✅", bot.user)

    async def test_bulk_delete_recreates_anchor_and_deletes_bound_tier_only(
        self,
    ) -> None:
        session = make_session()
        session.tier_anchor_message_id = 700
        repository = MagicMock()
        async def recreate(message_id, now):
            return session if message_id == 700 else None

        repository.request_tier_anchor_recreation = AsyncMock(side_effect=recreate)
        coordinator = MagicMock()
        collector = MagicMock()
        collector.submit_delete = AsyncMock(
            return_value=TierRouteResult(
                TierRouteStatus.ACCEPTED,
                session,
            )
        )
        cog = object.__new__(GatherCog)
        cog._config = make_config()
        cog._repository = repository
        cog._coordinator = coordinator
        cog._tier_collector = collector
        cog._clock = SimpleNamespace(now=lambda: session.created_at)
        payload = SimpleNamespace(
            guild_id=session.guild_id,
            channel_id=session.tier_channel_id,
            message_ids={700, 800},
        )

        await cog.on_raw_bulk_message_delete(payload)

        self.assertEqual(repository.request_tier_anchor_recreation.await_count, 2)
        coordinator.clear_tier_anchor_route.assert_called_once_with(700)
        collector.submit_delete.assert_awaited_once()
        self.assertEqual(
            collector.submit_delete.await_args.kwargs["discord_message_id"],
            800,
        )

    async def test_cancel_replaces_announcement_with_time_and_no_mentions(self) -> None:
        message = MagicMock(spec=discord.Message)
        message.delete = AsyncMock()
        message.edit = AsyncMock()
        channel = MagicMock(spec=discord.TextChannel)
        channel.fetch_message = AsyncMock(return_value=message)
        channel.send = AsyncMock()
        bot = MagicMock()
        bot.get_channel.return_value = channel

        cog = object.__new__(GatherCog)
        cog.bot = bot
        session = make_session()
        session.lobby_name = "대기실2"
        deleted = await cog._replace_announcement_with_cancellation(session)

        self.assertEqual(deleted, (True, True))
        channel.fetch_message.assert_awaited_once_with(1000)
        message.delete.assert_awaited_once_with()
        message.edit.assert_not_awaited()
        payload = channel.send.await_args.kwargs
        self.assertIn("__**오후 8시**__", payload["content"])
        self.assertIn("내전이 취소되었습니다", payload["content"])
        self.assertIn("대기실2", payload["content"])
        self.assertNotIn(make_session().match_code, payload["content"])
        self.assertEqual(payload["allowed_mentions"].to_dict()["parse"], [])

    async def test_missing_announcement_still_sends_cancellation(self) -> None:
        bot = MagicMock()
        bot.get_channel.return_value = MagicMock(spec=discord.TextChannel)
        channel = bot.get_channel.return_value
        channel.fetch_message = AsyncMock(side_effect=discord.NotFound(MagicMock(), "missing"))
        channel.send = AsyncMock()

        cog = object.__new__(GatherCog)
        cog.bot = bot

        self.assertEqual(await cog._replace_announcement_with_cancellation(make_session()), (True, True))


if __name__ == "__main__":
    unittest.main()
