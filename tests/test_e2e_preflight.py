from __future__ import annotations

import unittest
from dataclasses import replace

import discord

from src.e2e_preflight import (
    CheckLevel,
    permission_check,
    slash_command_checks,
    static_config_checks,
)
from tests.helpers import make_config


class E2EPreflightTest(unittest.TestCase):
    def test_static_config_warns_when_command_and_announcement_channels_match(
        self,
    ) -> None:
        config = make_config()
        config = replace(
            config,
            channels=replace(
                config.channels,
                announcement=config.channels.command,
            ),
        )

        results = static_config_checks(config)

        channel_result = next(
            result for result in results if result.label == "공지 채널 분리"
        )
        self.assertEqual(channel_result.level, CheckLevel.WARN)

    def test_slash_command_check_reports_complete_command_set(self) -> None:
        names = {
            "내전",
            "티어현황",
            "내전상태",
            "내전대타",
            "내전취소",
            "공지문구",
        }

        results = slash_command_checks(names)

        self.assertEqual(results[0].level, CheckLevel.PASS)

    def test_slash_command_check_reports_missing_command(self) -> None:
        results = slash_command_checks({"내전"})

        self.assertEqual(results[0].level, CheckLevel.FAIL)
        self.assertIn("/내전취소", results[0].detail)

    def test_permission_check_reports_missing_permissions(self) -> None:
        permissions = discord.Permissions.none()
        permissions.view_channel = True

        result = permission_check("모집 공지 채널", permissions)

        self.assertEqual(result.level, CheckLevel.FAIL)
        self.assertIn("send_messages", result.detail)
        self.assertIn("add_reactions", result.detail)
