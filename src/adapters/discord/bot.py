"""Discord 클라이언트. 명령·화면·이벤트·전송 구현은 책임별 모듈에 둔다."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import discord
from discord.ext import commands

from src.application.coordinator import SessionCoordinator
from src.application.notification_worker import NotificationWorker
from src.application.recruitment import RecruitmentService
from src.application.rendering import NotificationRenderer, RecruitmentTemplateRenderer
from src.application.scheduler import MatchScheduler
from src.application.tier_collector import TierCollector
from src.config import AppConfig
from src.domain.clock import Clock
from src.infrastructure.persistence_writer import PersistenceWriter
from src.ports.repositories import MatchRepository

from .commands import GatherCog
from .common import _normalized_command_payload
from .transport import DiscordNotificationTransport, DiscordRecruitmentPublisher

logger = logging.getLogger(__name__)


class GatherBot(commands.Bot):
    def __init__(
        self,
        *,
        config: AppConfig,
        repository: MatchRepository,
        writer: PersistenceWriter,
        coordinator: SessionCoordinator,
        tier_collector: TierCollector,
        scheduler: MatchScheduler,
        clock: Clock,
        template_path: Path,
        recruitment_complete_template_path: Path,
        migration_path: Path,
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
        self.repository = repository
        self.writer = writer
        self.coordinator = coordinator
        self.tier_collector = tier_collector
        self.scheduler = scheduler
        self.clock = clock
        self.template_path = template_path
        self.recruitment_complete_template_path = recruitment_complete_template_path
        self.migration_path = migration_path
        self.notification_worker: NotificationWorker | None = None
        self._caught_up = False
        if config.channels.command == config.channels.announcement:
            logger.warning(
                "command and announcement channel IDs are identical; "
                "use separate channels and Discord channel permissions for "
                "announcement-only operation channel_id=%s",
                config.channels.announcement,
            )

    async def setup_hook(self) -> None:
        from src.adapters.safety_adapter import SafetyCog
        from src.application.safety import SafetyService

        await self.repository.initialize(self.migration_path)
        self.writer.start()
        await self.coordinator.restore()
        safety = SafetyService(self.repository, self.clock, self.app_config.guild_id)
        await safety.purge_closed()
        await self.add_cog(SafetyCog(self, self.app_config, safety))
        await self.add_cog(
            GatherCog(
                self,
                self.app_config,
                self.repository,
                self.writer,
                self.coordinator,
                self.tier_collector,
                self.clock,
                RecruitmentService(
                    self.coordinator,
                    DiscordRecruitmentPublisher(
                        self,
                        self.app_config,
                        RecruitmentTemplateRenderer(
                            self.app_config, self.template_path
                        ),
                    ),
                ),
            )
        )
        guild = discord.Object(id=self.app_config.guild_id)
        self.tree.copy_global_to(guild=guild)
        await self._sync_guild_commands_if_changed(guild)
        self.notification_worker = NotificationWorker(
            self.repository,
            self.coordinator,
            NotificationRenderer(
                self.app_config,
                self.recruitment_complete_template_path,
            ),
            DiscordNotificationTransport(self),
            self.clock,
        )
        await self.notification_worker.start()
        self.scheduler.start()

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
            asyncio.create_task(
                self._catch_up_tier_messages(), name="tier-message-catch-up"
            )

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
        # A message created before recruitment completion can still qualify when it was
        # actually edited inside the collection window. Discord history filters by the
        # message creation snowflake, so startup recovery must scan the channel once and
        # apply the activity timestamp boundary locally.
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
        await self.remove_cog("SafetyCog")
        await self.scheduler.stop()
        if self.notification_worker is not None:
            await self.notification_worker.stop()
        await self.coordinator.stop()
        await self.writer.stop()
        await self.repository.close()
        await super().close()
