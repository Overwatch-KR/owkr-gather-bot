from __future__ import annotations

import unittest
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord

from owkr_gather_bot.adapters.discord_adapter import (
    GatherCog,
    ManagementCommandCheckFailure,
    RecruitmentCompleteAdvancedTemplateModal,
    RecruitmentCompleteCopyModal,
    _management_authorization_error,
    _normalized_command_payload,
)
from owkr_gather_bot.application.recruitment_complete_editor import (
    RecruitmentCompleteCopy,
    load_recruitment_complete_copy,
    recruitment_complete_copy_path,
)
from owkr_gather_bot.application.rendering import NotificationRenderer

from tests.helpers import make_config, make_session


class DiscordApplicationCommandTest(unittest.IsolatedAsyncioTestCase):
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
                "티어미작성알림",
                "내전상태",
                "내전취소",
                "모집완료문구",
                "모집완료미리보기",
                "모집완료고급편집",
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

    async def test_simple_copy_modal_hides_template_variables(self) -> None:
        cog = object.__new__(GatherCog)
        copy = RecruitmentCompleteCopy(
            tier_format_example="배틀태그\n뿅뿅이#31243\n그마5 / 그마2 / 그마5"
        )
        modal = RecruitmentCompleteCopyModal(cog, copy)

        self.assertEqual(modal.title, "모집 완료 공지 문구")
        self.assertEqual(modal.start_heading_input.default, "내전 시작")
        self.assertEqual(modal.tier_heading_input.default, "티어 작성")
        self.assertEqual(
            modal.tier_format_example_input.default,
            "배틀태그\n뿅뿅이#31243\n그마5 / 그마2 / 그마5",
        )
        all_defaults = "\n".join(
            str(child.component.default or "")
            for child in modal.children
            if isinstance(child, discord.ui.Label)
        )
        self.assertNotIn("{{", all_defaults)
        self.assertNotIn("{#", all_defaults)
        label = modal.children[0]
        self.assertIsInstance(label, discord.ui.Label)
        self.assertEqual(label.text, "시작 제목")
        self.assertEqual(label.description, "시작 시간은 봇이 자동으로 넣습니다.")

    async def test_advanced_modal_prefills_raw_template(self) -> None:
        cog = object.__new__(GatherCog)
        modal = RecruitmentCompleteAdvancedTemplateModal(
            cog,
            "{{ user }} 현재 문구",
        )

        self.assertEqual(modal.title, "모집 완료 고급 편집")
        self.assertEqual(modal.template_input.default, "{{ user }} 현재 문구")
        self.assertEqual(modal.template_input.max_length, 4000)
        self.assertEqual(modal.template_input.style, discord.TextStyle.paragraph)
        label = modal.children[0]
        self.assertIsInstance(label, discord.ui.Label)
        self.assertEqual(label.text, "개발자용 템플릿")

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
            if command.name == "모집완료문구"
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

    async def test_template_save_replaces_the_active_file(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "recruitment_complete.txt"
            path.write_text("old\n", encoding="utf-8")
            cog = object.__new__(GatherCog)
            cog._recruitment_complete_template_path = path

            cog.save_recruitment_complete_template("{{ user }} new")

            self.assertEqual(path.read_text(encoding="utf-8"), "{{ user }} new\n")
            self.assertFalse(path.with_suffix(".txt.tmp").exists())

    async def test_simple_copy_save_updates_editor_data_and_template(self) -> None:
        with TemporaryDirectory() as directory:
            template_path = Path(directory) / "recruitment_complete.txt"
            cog = object.__new__(GatherCog)
            cog._config = make_config()
            cog._recruitment_complete_template_path = template_path
            cog._recruitment_complete_copy_path = recruitment_complete_copy_path(
                template_path
            )
            copy = RecruitmentCompleteCopy(
                start_heading="오늘의 내전",
                tier_heading="티어 적기",
                tier_instruction="아래 양식으로 적어 주세요.",
                tier_format_example="배틀태그\n브5 / 브5 / 브5",
                extra_notice="늦으면 관리자에게 알려 주세요.",
            )

            cog.save_recruitment_complete_copy(copy)

            saved = load_recruitment_complete_copy(
                cog._recruitment_complete_copy_path
            )
            self.assertEqual(saved, copy)
            source = template_path.read_text(encoding="utf-8")
            self.assertIn("{{ starts_at }}", source)
            self.assertNotIn("{{ user }}", source)
            self.assertNotIn("{#", source)
            rendered = NotificationRenderer(
                make_config(),
                template_path,
            ).render_recruitment_complete(make_session(), (1, 2))
            self.assertIn("오늘의 내전", rendered.content)
            self.assertIn("티어 적기", rendered.content)
            self.assertIn("늦으면 관리자에게 알려 주세요.", rendered.content)
            self.assertNotIn("<@1>", rendered.content)
            self.assertNotIn("{{", rendered.content)

    async def test_seed_reaction_is_restored_when_no_participants_remain(self) -> None:
        actor = MagicMock()
        actor.session.announcement_message_id = 1000
        actor.drain = AsyncMock()
        actor.active_user_count.return_value = 0
        coordinator = MagicMock()
        coordinator.active_actor = actor
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

    async def test_seed_reaction_is_removed_while_participants_exist(self) -> None:
        actor = MagicMock()
        actor.session.announcement_message_id = 1000
        actor.drain = AsyncMock()
        actor.active_user_count.return_value = 1
        coordinator = MagicMock()
        coordinator.active_actor = actor
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

    async def test_cancel_cleanup_deletes_the_bot_announcement(self) -> None:
        message = MagicMock(spec=discord.Message)
        message.delete = AsyncMock()
        channel = MagicMock(spec=discord.TextChannel)
        channel.fetch_message = AsyncMock(return_value=message)
        bot = MagicMock()
        bot.get_channel.return_value = channel

        cog = object.__new__(GatherCog)
        cog.bot = bot
        deleted = await cog._delete_announcement_message(make_session())

        self.assertTrue(deleted)
        channel.fetch_message.assert_awaited_once_with(1000)
        message.delete.assert_awaited_once_with()

    async def test_missing_announcement_is_already_clean(self) -> None:
        bot = MagicMock()
        bot.get_channel.return_value = MagicMock(spec=discord.TextChannel)
        channel = bot.get_channel.return_value
        channel.fetch_message = AsyncMock(side_effect=discord.NotFound(MagicMock(), "missing"))

        cog = object.__new__(GatherCog)
        cog.bot = bot

        self.assertTrue(await cog._delete_announcement_message(make_session()))


if __name__ == "__main__":
    unittest.main()
