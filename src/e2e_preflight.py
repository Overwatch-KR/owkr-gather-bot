from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from enum import StrEnum
from typing import Iterable

import discord
from discord import app_commands

from src.config import AppConfig, ConfigurationError, load_runtime_config


EXPECTED_SLASH_COMMANDS = frozenset(
    {
        "내전",
        "티어현황",
        "내전상태",
        "내전대타",
        "내전취소",
        "공지문구",
    }
)


class CheckLevel(StrEnum):
    PASS = "PASS"
    WARN = "WARN"
    FAIL = "FAIL"


@dataclass(frozen=True, slots=True)
class CheckResult:
    level: CheckLevel
    label: str
    detail: str


CHANNEL_PERMISSION_REQUIREMENTS: dict[str, tuple[str, ...]] = {
    "명령 채널": (
        "view_channel",
        "send_messages",
        "read_message_history",
    ),
    "모집 공지 채널": (
        "view_channel",
        "send_messages",
        "read_message_history",
        "add_reactions",
        "embed_links",
    ),
    "티어 채널": (
        "view_channel",
        "send_messages",
        "read_message_history",
    ),
    "관리자 채널": (
        "view_channel",
        "send_messages",
    ),
    "대기실 음성 채널": ("view_channel",),
    "대기실 2 음성 채널": ("view_channel",),
}


def static_config_checks(config: AppConfig) -> list[CheckResult]:
    results = [
        CheckResult(
            CheckLevel.PASS,
            "테스트 모집 인원",
            f"{config.defaults.participant_limit}명으로 설정됨",
        ),
        CheckResult(
            CheckLevel.PASS,
            "관리자 권한",
            (
                f"사용자 {len(config.admin_user_ids)}명, "
                f"역할 {len(config.admin_role_ids)}개가 설정됨"
            ),
        ),
    ]
    if config.channels.command == config.channels.announcement:
        results.append(
            CheckResult(
                CheckLevel.WARN,
                "공지 채널 분리",
                "명령 채널과 모집 공지 채널이 같습니다. 기능은 동작하지만 공지 전용 운영은 아닙니다.",
            )
        )
    else:
        results.append(
            CheckResult(
                CheckLevel.PASS,
                "공지 채널 분리",
                "명령 채널과 모집 공지 채널이 분리되어 있습니다.",
            )
        )
    if config.defaults.participant_limit == 10:
        results.append(
            CheckResult(
                CheckLevel.WARN,
                "E2E 모집 인원",
                "현재 10명 설정입니다. 소규모 테스트라면 일시적으로 2명을 권장합니다.",
            )
        )
    return results


def slash_command_checks(remote_names: Iterable[str]) -> list[CheckResult]:
    remote = set(remote_names)
    missing = sorted(EXPECTED_SLASH_COMMANDS - remote)
    unexpected = sorted(remote - EXPECTED_SLASH_COMMANDS)
    results: list[CheckResult] = []
    if missing:
        results.append(
            CheckResult(
                CheckLevel.FAIL,
                "슬래시 명령 등록",
                f"서버에 없는 명령: {', '.join(f'/{name}' for name in missing)}",
            )
        )
    else:
        results.append(
            CheckResult(
                CheckLevel.PASS,
                "슬래시 명령 등록",
                f"필수 명령 {len(EXPECTED_SLASH_COMMANDS)}개가 모두 등록되어 있습니다.",
            )
        )
    if unexpected:
        results.append(
            CheckResult(
                CheckLevel.WARN,
                "추가 슬래시 명령",
                f"코드 목록 외 명령: {', '.join(f'/{name}' for name in unexpected)}",
            )
        )
    return results


def permission_check(
    label: str,
    permissions: discord.Permissions,
) -> CheckResult:
    missing = [
        permission
        for permission in CHANNEL_PERMISSION_REQUIREMENTS[label]
        if not getattr(permissions, permission, False)
    ]
    if missing:
        return CheckResult(
            CheckLevel.FAIL,
            f"{label} 권한",
            f"누락: {', '.join(missing)}",
        )
    return CheckResult(
        CheckLevel.PASS,
        f"{label} 권한",
        "필수 권한을 모두 확인했습니다.",
    )


class E2EPreflightClient(discord.Client):
    def __init__(self, config: AppConfig) -> None:
        intents = discord.Intents.none()
        intents.guilds = True
        intents.guild_messages = True
        intents.guild_reactions = True
        intents.message_content = True
        intents.voice_states = True
        super().__init__(intents=intents)
        self._config = config
        self.results = static_config_checks(config)
        self._completed = asyncio.Event()

    async def on_ready(self) -> None:
        try:
            await self._run_remote_checks()
        except Exception as exc:
            self.results.append(
                CheckResult(
                    CheckLevel.FAIL,
                    "Discord 연결 점검",
                    f"{exc.__class__.__name__}: {exc}",
                )
            )
        finally:
            self._completed.set()
            await self.close()

    async def _run_remote_checks(self) -> None:
        if self.user is None:
            raise RuntimeError("Discord bot user를 확인할 수 없습니다.")
        guild = self.get_guild(self._config.guild_id)
        if guild is None:
            self.results.append(
                CheckResult(
                    CheckLevel.FAIL,
                    "테스트 서버",
                    f"봇이 길드 {self._config.guild_id}에 참가하지 않았습니다.",
                )
            )
            return
        self.results.append(
            CheckResult(
                CheckLevel.PASS,
                "테스트 서버",
                f"{guild.name} ({guild.id}) 연결됨",
            )
        )

        channels = {channel.id: channel for channel in await guild.fetch_channels()}
        roles = {role.id: role for role in await guild.fetch_roles()}
        member = guild.me
        if member is None:
            member = await guild.fetch_member(self.user.id)

        channel_specs = [
            ("명령 채널", self._config.channels.command, discord.TextChannel),
            ("모집 공지 채널", self._config.channels.announcement, discord.TextChannel),
            ("티어 채널", self._config.channels.tier, discord.TextChannel),
            ("관리자 채널", self._config.channels.admin, discord.TextChannel),
            (
                "대기실 음성 채널",
                self._config.defaults.lobby_voice_channel_id,
                (discord.VoiceChannel, discord.StageChannel),
            ),
        ]
        if self._config.defaults.lobby_voice_channel_2_id is not None:
            channel_specs.append(
                (
                    "대기실 2 음성 채널",
                    self._config.defaults.lobby_voice_channel_2_id,
                    (discord.VoiceChannel, discord.StageChannel),
                )
            )
        for label, channel_id, expected_type in channel_specs:
            channel = channels.get(channel_id)
            if channel is None:
                self.results.append(
                    CheckResult(
                        CheckLevel.FAIL,
                        label,
                        f"채널 {channel_id}을 찾을 수 없습니다.",
                    )
                )
                continue
            if not isinstance(channel, expected_type):
                self.results.append(
                    CheckResult(
                        CheckLevel.FAIL,
                        label,
                        f"{channel.name}의 채널 종류가 올바르지 않습니다.",
                    )
                )
                continue
            self.results.append(
                CheckResult(
                    CheckLevel.PASS,
                    label,
                    f"#{channel.name} ({channel.id})",
                )
            )
            self.results.append(permission_check(label, channel.permissions_for(member)))

        configured_role_ids = set(self._config.admin_role_ids)
        recruitment_role_id = self._config.defaults.recruitment_role_id
        if recruitment_role_id is not None:
            configured_role_ids.add(recruitment_role_id)
        missing_roles = sorted(role_id for role_id in configured_role_ids if role_id not in roles)
        if missing_roles:
            self.results.append(
                CheckResult(
                    CheckLevel.FAIL,
                    "설정 역할",
                    f"서버에 없는 역할 ID: {', '.join(map(str, missing_roles))}",
                )
            )
        else:
            self.results.append(
                CheckResult(
                    CheckLevel.PASS,
                    "설정 역할",
                    f"관리자·모집 역할 {len(configured_role_ids)}개를 확인했습니다.",
                )
            )

        if recruitment_role_id is not None and recruitment_role_id in roles:
            recruitment_role = roles[recruitment_role_id]
            announcement = channels.get(self._config.channels.announcement)
            can_force_mention = (
                isinstance(announcement, discord.TextChannel)
                and announcement.permissions_for(member).mention_everyone
            )
            if recruitment_role.mentionable or can_force_mention:
                self.results.append(
                    CheckResult(
                        CheckLevel.PASS,
                        "모집 알림 역할 멘션",
                        f"@{recruitment_role.name} 역할을 실제로 멘션할 수 있습니다.",
                    )
                )
            else:
                self.results.append(
                    CheckResult(
                        CheckLevel.WARN,
                        "모집 알림 역할 멘션",
                        f"@{recruitment_role.name} 역할이 멘션 가능 상태가 아닙니다.",
                    )
                )

        tree = app_commands.CommandTree(self)
        remote_commands = await tree.fetch_commands(
            guild=discord.Object(id=self._config.guild_id)
        )
        self.results.extend(
            slash_command_checks(command.name for command in remote_commands)
        )

    async def wait_until_complete(self) -> None:
        await self._completed.wait()


def _print_results(results: Iterable[CheckResult]) -> bool:
    failed = False
    for result in results:
        print(f"[{result.level.value}] {result.label}: {result.detail}")
        failed = failed or result.level is CheckLevel.FAIL
    return failed


async def _run() -> int:
    try:
        runtime = load_runtime_config()
    except ConfigurationError as exc:
        print(f"[FAIL] 설정 로드: {exc}")
        return 1

    client = E2EPreflightClient(runtime.app)
    try:
        await client.start(runtime.token, reconnect=False)
    except discord.PrivilegedIntentsRequired:
        client.results.append(
            CheckResult(
                CheckLevel.FAIL,
                "Gateway Intents",
                "Discord Developer Portal에서 Message Content Intent를 활성화해야 합니다.",
            )
        )
    except discord.LoginFailure:
        client.results.append(
            CheckResult(
                CheckLevel.FAIL,
                "봇 토큰",
                "토큰이 유효하지 않습니다.",
            )
        )
    except discord.DiscordException as exc:
        client.results.append(
            CheckResult(
                CheckLevel.FAIL,
                "Discord 연결",
                f"{exc.__class__.__name__}: {exc}",
            )
        )
    failed = _print_results(client.results)
    return 1 if failed else 0


def main() -> None:
    logging.basicConfig(
        level=logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    raise SystemExit(asyncio.run(_run()))


if __name__ == "__main__":
    main()
