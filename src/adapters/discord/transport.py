"""Discord 전송 구현. 모든 전송에서 멘션 대상과 채널 종류를 제한한다."""

from __future__ import annotations

import logging

import discord
from discord.ext import commands

from src.application.rendering import (
    RecruitmentTemplateRenderer,
    format_discord_timestamp,
)
from src.config import AppConfig
from src.domain.models import MatchSession
from src.ports.notifications import NotificationTransport, RenderedNotification

from .common import allowed_mentions_for, build_recruitment_embed

logger = logging.getLogger(__name__)


class DiscordPublishedRecruitment:
    """생성 흐름 동안에만 보관하는 메시지 핸들. application에는 전송 계약만 노출한다."""

    def __init__(self, session: MatchSession, message: discord.Message) -> None:
        self._session = session
        self._message = message

    @property
    def message_id(self) -> int:
        return self._message.id

    async def add_seed_reaction(self) -> None:
        await self._message.add_reaction("✅")

    async def delete_incomplete(self) -> None:
        try:
            await self._message.delete()
        except discord.HTTPException:
            logger.warning(
                "failed to delete incomplete recruitment message match_id=%s message_id=%s",
                self._session.id,
                self.message_id,
                exc_info=True,
            )


class DiscordRecruitmentPublisher:
    """모집 공지·초기 반응·실패 시 삭제를 Discord API로 수행한다."""

    def __init__(
        self,
        bot: commands.Bot,
        config: AppConfig,
        renderer: RecruitmentTemplateRenderer,
    ) -> None:
        self._bot = bot
        self._config = config
        self._renderer = renderer

    async def publish(self, session: MatchSession) -> DiscordPublishedRecruitment:
        channel = self._bot.get_channel(session.announcement_channel_id)
        if channel is None:
            channel = await self._bot.fetch_channel(session.announcement_channel_id)
        if not isinstance(channel, discord.abc.Messageable):
            raise TypeError("announcement channel is not messageable")
        description = self._renderer.render(
            session, self._config.manager(session.manager_user_id)
        )
        role_id = self._config.defaults.recruitment_role_id
        role_mention = f"<@&{role_id}> " if role_id else ""
        lead = (
            f"{role_mention}<@{session.manager_user_id}>님이 여는 "
            f"**{format_discord_timestamp(session.starts_at, 't')} 내전**!\n"
            "참가하려면 아래 ✅을 눌러 주세요."
        )
        message = await channel.send(
            content=lead,
            embed=build_recruitment_embed(self._config, description),
            allowed_mentions=allowed_mentions_for([], [role_id] if role_id else []),
        )
        return DiscordPublishedRecruitment(session, message)


class DiscordNotificationTransport(NotificationTransport):
    def __init__(self, bot: commands.Bot) -> None:
        self._bot = bot

    async def send(
        self,
        channel_id: int,
        rendered: RenderedNotification,
    ) -> int | None:
        if rendered.voice_channel_id is not None:
            voice_member_ids = await self._voice_channel_member_ids(
                rendered.voice_channel_id
            )
            missing_user_ids = tuple(
                user_id
                for user_id in rendered.allowed_user_ids
                if user_id not in voice_member_ids
            )
            if not missing_user_ids:
                logger.info(
                    "lobby reminder skipped because all participants are present "
                    "voice_channel_id=%s",
                    rendered.voice_channel_id,
                )
                return None
            rendered = rendered.with_allowed_user_ids(missing_user_ids)

        channel = self._bot.get_channel(channel_id)
        if channel is None:
            channel = await self._bot.fetch_channel(channel_id)
        if not isinstance(channel, discord.abc.Messageable):
            raise TypeError(f"channel {channel_id} is not messageable")
        message = await channel.send(
            rendered.content,
            allowed_mentions=allowed_mentions_for(rendered.allowed_user_ids),
        )
        return message.id

    async def _voice_channel_member_ids(self, channel_id: int) -> set[int]:
        if not self._bot.intents.voice_states:
            raise RuntimeError(
                "Guild Voice States intent is required for lobby reminders"
            )
        channel = self._bot.get_channel(channel_id)
        if channel is None:
            channel = await self._bot.fetch_channel(channel_id)
        if not isinstance(channel, (discord.VoiceChannel, discord.StageChannel)):
            raise TypeError(f"channel {channel_id} is not a voice channel")
        return set(channel.voice_states)
