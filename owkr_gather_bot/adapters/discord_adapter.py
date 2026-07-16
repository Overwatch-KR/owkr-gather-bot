from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from pathlib import Path
from typing import Sequence

import discord
from discord.ext import commands

from owkr_gather_bot.application.coordinator import CreateMatchRequest, SessionCoordinator
from owkr_gather_bot.application.notification_worker import NotificationTransport, NotificationWorker
from owkr_gather_bot.application.rendering import (
    NotificationRenderer,
    RecruitmentTemplateRenderer,
    RenderedNotification,
    format_korean_datetime,
)
from owkr_gather_bot.application.scheduler import MatchScheduler
from owkr_gather_bot.application.tier_collector import TierCollector
from owkr_gather_bot.config import AppConfig
from owkr_gather_bot.domain.clock import Clock
from owkr_gather_bot.domain.models import ReactionAction
from owkr_gather_bot.infrastructure.persistence_writer import PersistenceWriter
from owkr_gather_bot.parsing.match_command import (
    CommandParseError,
    MatchCommandParser,
    ParsedMatchCommand,
    PastTimeError,
)
from owkr_gather_bot.ports.repositories import MatchRepository


logger = logging.getLogger(__name__)


def allowed_mentions_for(user_ids: Sequence[int]) -> discord.AllowedMentions:
    users: bool | list[discord.Object]
    users = [discord.Object(id=user_id) for user_id in user_ids] if user_ids else False
    return discord.AllowedMentions(
        everyone=False,
        roles=False,
        users=users,
        replied_user=False,
    )


class DiscordNotificationTransport(NotificationTransport):
    def __init__(self, bot: commands.Bot) -> None:
        self._bot = bot

    async def send(self, channel_id: int, rendered: RenderedNotification) -> int:
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


class NextDayConfirmationView(discord.ui.View):
    def __init__(
        self,
        cog: GatherCog,
        *,
        manager_user_id: int,
        command_channel_id: int,
        parsed: ParsedMatchCommand,
        timeout: float,
    ) -> None:
        super().__init__(timeout=timeout)
        self._cog = cog
        self._manager_user_id = manager_user_id
        self._command_channel_id = command_channel_id
        self._parsed = parsed
        self._used = False

    @discord.ui.button(label="내일 같은 시각으로 생성", style=discord.ButtonStyle.primary)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if interaction.user.id != self._manager_user_id:
            await interaction.response.send_message(
                "이 확인은 명령을 실행한 관리자만 사용할 수 있습니다.", ephemeral=True
            )
            return
        if self._used:
            await interaction.response.send_message("이미 처리된 요청입니다.", ephemeral=True)
            return
        self._used = True
        button.disabled = True
        await interaction.response.edit_message(view=self)
        try:
            session = await self._cog.create_match(
                manager_user_id=self._manager_user_id,
                command_channel_id=self._command_channel_id,
                parsed=self._parsed,
            )
        except Exception:
            logger.exception("next-day match creation failed")
            await interaction.followup.send("내전 생성에 실패했습니다. 로그를 확인해 주세요.", ephemeral=True)
            return
        await interaction.followup.send(
            f"내일 {format_korean_datetime(session.starts_at)} 내전을 생성했습니다.", ephemeral=True
        )

    @discord.ui.button(label="취소", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if interaction.user.id != self._manager_user_id:
            await interaction.response.send_message(
                "이 확인은 명령을 실행한 관리자만 사용할 수 있습니다.", ephemeral=True
            )
            return
        self._used = True
        for child in self.children:
            child.disabled = True  # type: ignore[attr-defined]
        await interaction.response.edit_message(content="내전 생성을 취소했습니다.", view=self)


class GatherCog(commands.Cog):
    def __init__(
        self,
        bot: GatherBot,
        config: AppConfig,
        repository: MatchRepository,
        writer: PersistenceWriter,
        coordinator: SessionCoordinator,
        tier_collector: TierCollector,
        clock: Clock,
        template_path: Path,
    ) -> None:
        self.bot = bot
        self._config = config
        self._repository = repository
        self._writer = writer
        self._coordinator = coordinator
        self._tier_collector = tier_collector
        self._clock = clock
        self._parser = MatchCommandParser(
            tier_deadline_offset_minutes=config.defaults.tier_deadline_offset_minutes,
            lobby_offset_minutes=config.defaults.lobby_offset_minutes,
        )
        self._template = RecruitmentTemplateRenderer(config, template_path)

    def _authorized(self, ctx: commands.Context) -> bool:
        if ctx.guild is None or ctx.guild.id != self._config.guild_id:
            return False
        if ctx.channel.id != self._config.channels.command:
            return False
        role_ids = {
            role.id for role in getattr(ctx.author, "roles", []) if isinstance(role, discord.Role)
        }
        return self._config.is_manager(ctx.author.id, role_ids)

    async def _require_manager(self, ctx: commands.Context) -> bool:
        if self._authorized(ctx):
            return True
        logger.warning(
            "unauthorized management command user_id=%s guild_id=%s channel_id=%s command=%s",
            ctx.author.id,
            ctx.guild.id if ctx.guild else None,
            ctx.channel.id,
            ctx.command.qualified_name if ctx.command else None,
        )
        await ctx.send("이 채널에서 내전 관리 명령을 사용할 권한이 없습니다.")
        return False

    @commands.command(name="내전")
    async def create_match_command(self, ctx: commands.Context, *, arguments: str = "") -> None:
        if not await self._require_manager(ctx):
            return
        manager = self._config.manager(ctx.author.id)
        try:
            parsed = self._parser.parse(
                arguments,
                now=self._clock.now(),
                default_mode=manager.default_mode,
            )
        except PastTimeError as exc:
            if not self._config.defaults.offer_next_day_confirmation:
                await ctx.send("입력한 시각이 이미 지났습니다. 미래 시각으로 다시 입력해 주세요.")
                return
            view = NextDayConfirmationView(
                self,
                manager_user_id=ctx.author.id,
                command_channel_id=ctx.channel.id,
                parsed=exc.proposal,
                timeout=self._config.defaults.next_day_confirmation_timeout_seconds,
            )
            await ctx.send(
                f"입력한 시각이 이미 지났습니다. {format_korean_datetime(exc.proposal.starts_at)}로 생성할까요?",
                view=view,
                allowed_mentions=allowed_mentions_for([]),
            )
            return
        except CommandParseError as exc:
            await ctx.send(str(exc), allowed_mentions=allowed_mentions_for([]))
            return

        try:
            session = await self.create_match(
                manager_user_id=ctx.author.id,
                command_channel_id=ctx.channel.id,
                parsed=parsed,
            )
        except Exception:
            logger.exception("match creation failed")
            await ctx.send("내전 생성에 실패했습니다. 관리자 로그를 확인해 주세요.")
            return
        await ctx.send(
            f"{format_korean_datetime(session.starts_at)} 내전 모집 공지를 생성했습니다.",
            allowed_mentions=allowed_mentions_for([]),
        )

    async def create_match(
        self,
        *,
        manager_user_id: int,
        command_channel_id: int,
        parsed: ParsedMatchCommand,
    ):
        session = await self._coordinator.create_replacing_active(
            CreateMatchRequest(
                manager_user_id=manager_user_id,
                command_channel_id=command_channel_id,
                parsed=parsed,
            )
        )
        try:
            channel = self.bot.get_channel(session.announcement_channel_id)
            if channel is None:
                channel = await self.bot.fetch_channel(session.announcement_channel_id)
            if not isinstance(channel, discord.abc.Messageable):
                raise TypeError("announcement channel is not messageable")
            content = self._template.render(session, self._config.manager(manager_user_id))
            message = await channel.send(content, allowed_mentions=allowed_mentions_for([]))
            await self._coordinator.activate(session, message.id)
            await message.add_reaction("✅")
            return session
        except Exception:
            await self._coordinator.cancel_current(expected_match_id=session.id)
            raise

    @commands.command(name="티어현황")
    async def tier_status(self, ctx: commands.Context) -> None:
        if not await self._require_manager(ctx):
            return
        session = self._coordinator.active_session
        if session is None:
            await ctx.send("활성 내전이 없습니다.")
            return
        if session.recruitment_completed_notified_at is None:
            await ctx.send("모집 완료 공지가 전송된 뒤 티어 수집이 시작됩니다.")
            return
        await self._writer.flush()
        statuses = await self._repository.get_tier_status(session.id)
        completed = [item.discord_display_name for item in statuses if item.has_tier]
        missing = [item.discord_display_name for item in statuses if not item.has_tier]
        content = (
            f"티어 작성 완료 {len(completed)}명: {', '.join(completed) or '-'}\n"
            f"티어 미작성 {len(missing)}명: {', '.join(missing) or '-'}"
        )
        await ctx.send(content, allowed_mentions=allowed_mentions_for([]))

    @commands.command(name="티어미작성알림")
    async def remind_missing_tier(self, ctx: commands.Context) -> None:
        if not await self._require_manager(ctx):
            return
        session = self._coordinator.active_session
        now = self._clock.now()
        if session is None:
            await ctx.send("활성 내전이 없습니다.")
            return
        if session.recruitment_completed_notified_at is None:
            await ctx.send("모집 완료 공지가 전송된 뒤 사용할 수 있습니다.")
            return
        if now >= session.tier_deadline_at:
            await ctx.send("티어 작성 마감 시각이 지났습니다.")
            return
        await self._writer.flush()
        claimed, missing = await self._repository.claim_missing_tier_reminder(
            session.id, now, cooldown_seconds=300
        )
        if not claimed:
            await ctx.send("동일 알림은 5분에 한 번만 보낼 수 있습니다.")
            return
        if not missing:
            await ctx.send("현재 확정 참가자 전원이 티어를 작성했습니다.")
            return
        user_ids = [item.discord_user_id for item in missing]
        mentions = " ".join(f"<@{user_id}>" for user_id in user_ids)
        await ctx.send(
            f"📝 아직 티어를 작성하지 않은 참가자입니다.\n{mentions}",
            allowed_mentions=allowed_mentions_for(user_ids),
        )
        logger.info(
            "missing tier reminder sent match_id=%s recipient_count=%s",
            session.id,
            len(user_ids),
        )

    @commands.command(name="내전상태")
    async def match_status(self, ctx: commands.Context) -> None:
        if not await self._require_manager(ctx):
            return
        session = self._coordinator.active_session
        actor = self._coordinator.active_actor
        if session is None:
            session = await self._repository.get_latest_match(self._config.guild_id)
        if session is None:
            await ctx.send("생성된 내전이 없습니다.")
            return
        await self._writer.flush()
        if actor is not None and actor.session.id == session.id:
            confirmed = len(actor.current_confirmed())
            waitlisted = len(actor.current_waitlist())
        else:
            roster = await self._repository.load_roster(session.id)
            confirmed = sum(1 for entry in roster if entry.status.value == "CONFIRMED")
            waitlisted = sum(1 for entry in roster if entry.status.value == "WAITLISTED")
        await ctx.send(
            f"상태: {session.status.value}\n"
            f"시작: {format_korean_datetime(session.starts_at)}\n"
            f"모드: {session.mode or '일반 내전'}\n"
            f"확정 참가자: {confirmed}명\n대기자: {waitlisted}명",
            allowed_mentions=allowed_mentions_for([]),
        )

    @commands.command(name="내전취소")
    async def cancel_match(self, ctx: commands.Context) -> None:
        if not await self._require_manager(ctx):
            return
        canceled = await self._coordinator.cancel_current()
        if canceled is None:
            await ctx.send("활성 내전이 없습니다.")
            return
        await ctx.send("활성 내전을 취소하고 예약 알림을 중단했습니다.")

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent) -> None:
        if payload.guild_id != self._config.guild_id or payload.emoji.name != "✅":
            return
        user = payload.member or self.bot.get_user(payload.user_id)
        if user is not None and user.bot:
            return
        display_name = getattr(user, "display_name", None) or str(payload.user_id)
        self._coordinator.ingest_reaction(
            announcement_message_id=payload.message_id,
            discord_user_id=payload.user_id,
            discord_display_name=display_name,
            action=ReactionAction.ADD,
            received_at=self._clock.now(),
        )

    @commands.Cog.listener()
    async def on_raw_reaction_remove(self, payload: discord.RawReactionActionEvent) -> None:
        if payload.guild_id != self._config.guild_id or payload.emoji.name != "✅":
            return
        user = self.bot.get_user(payload.user_id)
        if user is not None and user.bot:
            return
        actor = self._coordinator.active_actor
        display_name = actor.display_name_for(payload.user_id) if actor else None
        self._coordinator.ingest_reaction(
            announcement_message_id=payload.message_id,
            discord_user_id=payload.user_id,
            discord_display_name=display_name or str(payload.user_id),
            action=ReactionAction.REMOVE,
            received_at=self._clock.now(),
        )

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.guild is None or message.author.bot:
            return
        accepted = self._tier_collector.submit_activity(
            guild_id=message.guild.id,
            channel_id=message.channel.id,
            discord_user_id=message.author.id,
            discord_display_name=getattr(message.author, "display_name", message.author.name),
            raw_content=message.content,
            discord_message_id=message.id,
            activity_at=message.created_at,
        )
        if accepted:
            session = self._coordinator.active_session
            logger.info(
                "tier message create detected match_id=%s user_id=%s message_id=%s result=queued",
                session.id if session else None,
                message.author.id,
                message.id,
            )

    @commands.Cog.listener()
    async def on_raw_message_edit(self, payload: discord.RawMessageUpdateEvent) -> None:
        actor = self._coordinator.active_actor
        if (
            actor is None
            or payload.guild_id != self._config.guild_id
            or payload.channel_id != self._config.channels.tier
        ):
            return
        try:
            channel = self.bot.get_channel(payload.channel_id)
            if channel is None:
                channel = await self.bot.fetch_channel(payload.channel_id)
            if not isinstance(channel, discord.abc.Messageable):
                return
            message = await channel.fetch_message(payload.message_id)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            return
        if message.author.bot:
            return
        accepted = self._tier_collector.submit_activity(
            guild_id=payload.guild_id,
            channel_id=payload.channel_id,
            discord_user_id=message.author.id,
            discord_display_name=getattr(message.author, "display_name", message.author.name),
            raw_content=message.content,
            discord_message_id=message.id,
            activity_at=message.edited_at or self._clock.now(),
        )
        if accepted:
            session = self._coordinator.active_session
            logger.info(
                "tier message edit detected match_id=%s user_id=%s message_id=%s result=queued",
                session.id if session else None,
                message.author.id,
                message.id,
            )

    @commands.Cog.listener()
    async def on_raw_message_delete(self, payload: discord.RawMessageDeleteEvent) -> None:
        if payload.guild_id is None:
            return
        accepted = self._tier_collector.submit_delete(
            guild_id=payload.guild_id,
            channel_id=payload.channel_id,
            discord_message_id=payload.message_id,
            received_at=self._clock.now(),
        )
        if accepted:
            session = self._coordinator.active_session
            logger.info(
                "tier message delete detected match_id=%s user_id=unavailable message_id=%s result=queued",
                session.id if session else None,
                payload.message_id,
            )


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
        migration_path: Path,
    ) -> None:
        intents = discord.Intents.none()
        intents.guilds = True
        intents.guild_messages = True
        intents.guild_reactions = True
        intents.message_content = True
        super().__init__(command_prefix=".", intents=intents, help_command=None)
        self.app_config = config
        self.repository = repository
        self.writer = writer
        self.coordinator = coordinator
        self.tier_collector = tier_collector
        self.scheduler = scheduler
        self.clock = clock
        self.template_path = template_path
        self.migration_path = migration_path
        self.notification_worker: NotificationWorker | None = None
        self._caught_up = False

    async def setup_hook(self) -> None:
        await self.repository.initialize(self.migration_path)
        self.writer.start()
        await self.coordinator.restore()
        await self.add_cog(
            GatherCog(
                self,
                self.app_config,
                self.repository,
                self.writer,
                self.coordinator,
                self.tier_collector,
                self.clock,
                self.template_path,
            )
        )
        self.notification_worker = NotificationWorker(
            self.repository,
            self.coordinator,
            NotificationRenderer(self.app_config),
            DiscordNotificationTransport(self),
            self.clock,
        )
        await self.notification_worker.start()
        self.scheduler.start()

    async def on_ready(self) -> None:
        logger.info("bot ready user_id=%s", self.user.id if self.user else None)
        if not self._caught_up:
            self._caught_up = True
            asyncio.create_task(self._catch_up_tier_messages(), name="tier-message-catch-up")

    async def _catch_up_tier_messages(self) -> None:
        actor = self.coordinator.active_actor
        if actor is None:
            return
        session = actor.session
        opened_at = session.recruitment_completed_notified_at
        if opened_at is None:
            return
        before = min(self.clock.now(), session.tier_deadline_at, session.starts_at)
        if before <= opened_at:
            return
        channel = self.get_channel(session.tier_channel_id)
        if channel is None:
            try:
                channel = await self.fetch_channel(session.tier_channel_id)
            except discord.HTTPException:
                return
        if not isinstance(channel, discord.TextChannel):
            return
        # A message created before recruitment completion can still qualify when it was
        # actually edited inside the collection window. Discord history filters by the
        # message creation snowflake, so startup recovery must scan the channel once and
        # apply the activity timestamp boundary locally.
        async for message in channel.history(limit=None, before=before, oldest_first=False):
            if message.author.bot:
                continue
            activity_at = message.edited_at or message.created_at
            if not opened_at <= activity_at < before:
                continue
            self.tier_collector.submit_activity(
                guild_id=session.guild_id,
                channel_id=session.tier_channel_id,
                discord_user_id=message.author.id,
                discord_display_name=getattr(message.author, "display_name", message.author.name),
                raw_content=message.content,
                discord_message_id=message.id,
                activity_at=activity_at,
            )

    async def close(self) -> None:
        await self.scheduler.stop()
        if self.notification_worker is not None:
            await self.notification_worker.stop()
        await self.coordinator.stop()
        await self.writer.stop()
        await self.repository.close()
        await super().close()
