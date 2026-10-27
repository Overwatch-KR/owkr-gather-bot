"""Discord 연결·명령 등록·재연결 후 티어 보완 수집을 담당한다."""

from __future__ import annotations

import asyncio
import logging

import discord
from discord.ext import commands

from src.adapters.safety_adapter import SafetyCog
from src.application.runtime import ApplicationRuntime
from src.config import AppConfig
from src.domain.clock import Clock

from .commands import GatherCog
from .common import _normalized_command_payload

logger = logging.getLogger(__name__)


class GatherBot(commands.Bot):
    """Discord 연결을 소유하고 서비스 생명주기는 ApplicationRuntime에 위임한다."""

    def __init__(
        self,
        *,
        config: AppConfig,
        runtime: ApplicationRuntime,
        clock: Clock,
    ) -> None:
        intents = discord.Intents.none()
        intents.guilds = True
        intents.guild_messages = True
        intents.guild_reactions = True
        intents.message_content = True
        intents.voice_states = True
        super().__init__(
            command_prefix=commands.when_mentioned, intents=intents, help_command=None
        )
        self.app_config = config
        self.runtime = runtime
        self.coordinator = runtime.coordinator
        self.tier_collector = runtime.tier_collector
        self.clock = clock
        self._caught_up = False
        self._catch_up_task: asyncio.Task[None] | None = None
        if config.channels.command == config.channels.announcement:
            logger.warning(
                "command and announcement channel IDs are identical; "
                "use separate channels and Discord channel permissions for "
                "announcement-only operation channel_id=%s",
                config.channels.announcement,
            )

    async def setup_hook(self) -> None:
        # routing이 복구되기 전에 이벤트·알림 처리를 시작하지 않는다.
        if self.runtime.recruitment is None:
            raise RuntimeError("recruitment service is not configured")
        await self.runtime.start_storage()
        await self.add_cog(SafetyCog(self, self.app_config, self.runtime.safety))
        await self.add_cog(
            GatherCog(
                self,
                self.app_config,
                self.runtime.repository,
                self.runtime.writer,
                self.coordinator,
                self.tier_collector,
                self.clock,
                self.runtime.recruitment,
            )
        )
        guild = discord.Object(id=self.app_config.guild_id)
        self.tree.copy_global_to(guild=guild)
        await self._sync_guild_commands_if_changed(guild)
        await self.runtime.start_workers()

    async def _sync_guild_commands_if_changed(self, guild: discord.Object) -> None:
        local_payloads = [
            _normalized_command_payload(command.to_dict(self.tree))
            for command in self.tree.get_commands(guild=guild)
        ]
        remote_commands = await self.tree.fetch_commands(guild=guild)
        remote_payloads = [
            _normalized_command_payload(command.to_dict())
            for command in remote_commands
        ]
        sort_key = lambda item: (item.get("type", 1), item.get("name", ""))
        if sorted(local_payloads, key=sort_key) == sorted(
            remote_payloads, key=sort_key
        ):
            logger.info(
                "guild application commands unchanged; sync skipped guild_id=%s command_count=%s",
                self.app_config.guild_id,
                len(local_payloads),
            )
            return
        synced_commands = await self.tree.sync(guild=guild)
        logger.info(
            "guild application commands synced guild_id=%s command_count=%s",
            self.app_config.guild_id,
            len(synced_commands),
        )

    async def on_ready(self) -> None:
        logger.info("bot ready user_id=%s", self.user.id if self.user else None)
        if not self._caught_up:
            self._caught_up = True
            self._catch_up_task = asyncio.create_task(
                self._run_tier_catch_up(),
                name="tier-message-catch-up",
            )

    async def _run_tier_catch_up(self) -> None:
        try:
            await self._catch_up_tier_messages()
        except Exception:
            logger.exception("tier message catch-up failed")

    async def _catch_up_tier_messages(self) -> None:
        sessions = [
            session
            for session in self.coordinator.active_sessions
            if session.recruitment_completed_notified_at is not None
        ]
        if not sessions:
            return
        earliest_opened_at = min(
            session.recruitment_completed_notified_at
            for session in sessions
            if session.recruitment_completed_notified_at is not None
        )
        before = self.clock.now()
        if before <= earliest_opened_at:
            return
        channel = self.get_channel(self.app_config.channels.tier)
        if channel is None:
            try:
                channel = await self.fetch_channel(self.app_config.channels.tier)
            except discord.HTTPException:
                return
        if not isinstance(channel, discord.TextChannel):
            return
        # Discord 이력은 생성 시각으로 필터링한다. 모집 완료 전 메시지도 수집 기간에
        # 수정됐다면 대상이므로 채널을 한 번 읽고 실제 활동 시각으로 판정한다.
        async for message in channel.history(
            limit=None, before=before, oldest_first=False
        ):
            if message.author.bot:
                continue
            activity_at = message.edited_at or message.created_at
            if not earliest_opened_at <= activity_at < before:
                continue
            await self.tier_collector.submit_activity(
                guild_id=self.app_config.guild_id,
                channel_id=self.app_config.channels.tier,
                discord_user_id=message.author.id,
                discord_display_name=getattr(
                    message.author, "display_name", message.author.name
                ),
                raw_content=message.content,
                discord_message_id=message.id,
                activity_at=activity_at,
                reply_to_message_id=(
                    message.reference.message_id
                    if message.reference is not None
                    else None
                ),
            )

    async def close(self) -> None:
        # Cog의 유지보수 작업부터 멈춰 DB 종료 후 쓰기가 발생하지 않게 한다.
        await self.remove_cog("SafetyCog")
        await self.remove_cog("GatherCog")
        if self._catch_up_task is not None:
            self._catch_up_task.cancel()
            await asyncio.gather(self._catch_up_task, return_exceptions=True)
            self._catch_up_task = None
        await self.runtime.stop()
        await super().close()
