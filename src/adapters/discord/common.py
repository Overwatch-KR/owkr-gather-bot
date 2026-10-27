"""Discord 권한 검사·멘션 제한·표시 형식. 상태 변경이나 저장은 수행하지 않는다."""

from __future__ import annotations

from typing import Callable, Sequence, TypeVar

import discord
from discord import app_commands

from src.application.rendering import format_discord_timestamp
from src.config import AppConfig
from src.domain.models import MatchSession, MatchStatus

CommandType = TypeVar("CommandType")
MATCH_STATUS_LABELS = {
    MatchStatus.CREATED: "공지 준비 중",
    MatchStatus.RECRUITING: "모집 중",
    MatchStatus.FULL: "모집 완료",
    MatchStatus.STARTED: "시작됨",
    MatchStatus.CANCELED: "취소됨",
}


def match_status_label(status: MatchStatus) -> str:
    return MATCH_STATUS_LABELS[status]


def discord_message_url(guild_id: int, channel_id: int, message_id: int) -> str:
    return f"https://discord.com/channels/{guild_id}/{channel_id}/{message_id}"


class ManagementCommandCheckFailure(app_commands.CheckFailure):
    def __init__(self, user_message: str) -> None:
        self.user_message = user_message
        super().__init__(user_message)


def _interaction_role_ids(interaction: discord.Interaction) -> set[int]:
    return {
        int(role.id)
        for role in getattr(interaction.user, "roles", [])
        if getattr(role, "id", None) is not None
    }


def _management_authorization_error(
    config: AppConfig,
    interaction: discord.Interaction,
) -> str | None:
    if interaction.guild_id != config.guild_id:
        return "이 서버에서는 내전 관리 명령을 사용할 수 없습니다."
    if interaction.channel_id != config.channels.command:
        return f"내전 관리 명령은 <#{config.channels.command}> 채널에서만 사용할 수 있습니다."
    if not config.is_manager(interaction.user.id, _interaction_role_ids(interaction)):
        return "내전 관리자 또는 스태프 역할이 필요합니다."
    return None


def management_command_only() -> Callable[[CommandType], CommandType]:
    async def predicate(interaction: discord.Interaction) -> bool:
        config = getattr(interaction.client, "app_config", None)
        if not isinstance(config, AppConfig):
            raise ManagementCommandCheckFailure(
                "봇의 관리자 권한 설정을 확인할 수 없습니다."
            )
        error = _management_authorization_error(config, interaction)
        if error is not None:
            raise ManagementCommandCheckFailure(error)
        return True

    predicate.__name__ = "management_command_check"
    return app_commands.check(predicate)


def _normalized_command_payload(payload: dict) -> dict:
    ignored_keys = {
        "id",
        "application_id",
        "version",
        "guild_id",
        "dm_permission",
        "contexts",
        "integration_types",
    }
    normalized: dict = {}
    for key, value in payload.items():
        if key in ignored_keys or value is None:
            continue
        if isinstance(value, dict):
            value = _normalized_command_payload(value)
        elif isinstance(value, list):
            value = [
                _normalized_command_payload(item) if isinstance(item, dict) else item
                for item in value
            ]
        if value in ([], {}) or (
            value is False and key in {"required", "autocomplete"}
        ):
            continue
        normalized[key] = value
    return normalized


def allowed_mentions_for(
    user_ids: Sequence[int],
    role_ids: Sequence[int] = (),
) -> discord.AllowedMentions:
    users: bool | list[discord.Object]
    users = [discord.Object(id=user_id) for user_id in user_ids] if user_ids else False
    roles: bool | list[discord.Object]
    roles = [discord.Object(id=role_id) for role_id in role_ids] if role_ids else False
    return discord.AllowedMentions(
        everyone=False,
        roles=roles,
        users=users,
        replied_user=False,
    )


def build_recruitment_embed(
    config: AppConfig,
    description: str,
) -> discord.Embed:
    embed = discord.Embed(
        title="내전 모집",
        description=description,
        color=discord.Color.from_rgb(67, 181, 129),
    )
    embed.set_footer(text=config.messages.manner_notice)
    return embed


def build_substitute_recruitment_embed(session: MatchSession) -> discord.Embed:
    return discord.Embed(
        title="내전 대타 모집",
        description=(
            f"**{format_discord_timestamp(session.starts_at, 'F')}** · "
            f"{format_discord_timestamp(session.starts_at, 'R')}\n\n"
            "이 메시지에 ✅ 반응하면 해당 내전의 대타 대기열에 등록됩니다.\n"
            "첫 **1명**이 등록되면 티어 작성 안내를 보내드립니다."
        ),
        color=discord.Color.from_rgb(237, 66, 69),
    )
