"""Discord 이벤트를 세션·티어 입력으로 변환한다. 반응 순서는 Actor에서 결정한다."""

from __future__ import annotations

import asyncio
import logging

import discord
from discord.ext import commands

from src.application.tier_collector import TierRouteResult, TierRouteStatus
from src.domain.models import ReactionAction

from .common import allowed_mentions_for

logger = logging.getLogger(__name__)


class DiscordEventHandlers:
    """GatherCog에 결합되는 이벤트 처리부. Cog가 listener 메타데이터를 등록한다."""

    @commands.Cog.listener()
    async def on_raw_reaction_add(
        self, payload: discord.RawReactionActionEvent
    ) -> None:
        if payload.guild_id != self._config.guild_id or payload.emoji.name != "✅":
            return
        user = payload.member or self.bot.get_user(payload.user_id)
        if user is not None and user.bot:
            return
        display_name = getattr(user, "display_name", None) or str(payload.user_id)
        accepted = self._coordinator.ingest_reaction(
            announcement_message_id=payload.message_id,
            discord_user_id=payload.user_id,
            discord_display_name=display_name,
            action=ReactionAction.ADD,
            received_at=self._clock.now(),
        )
        if accepted:
            self._schedule_bot_seed_reaction_reconcile(
                payload.channel_id, payload.message_id
            )

    def _schedule_bot_seed_reaction_reconcile(
        self,
        channel_id: int,
        message_id: int,
    ) -> None:
        self._seed_reaction_versions[message_id] = (
            self._seed_reaction_versions.get(message_id, 0) + 1
        )
        task = self._seed_reaction_tasks.get(message_id)
        if task is not None and not task.done():
            return
        self._seed_reaction_tasks[message_id] = asyncio.create_task(
            self._reconcile_bot_seed_reaction(channel_id, message_id),
            name=f"reconcile-bot-seed-reaction:{message_id}",
        )

    async def _reconcile_bot_seed_reaction(
        self,
        channel_id: int,
        message_id: int,
    ) -> None:
        try:
            while True:
                version = self._seed_reaction_versions.get(message_id, 0)
                actor = self._coordinator.actor_for_announcement(message_id)
                if actor is None:
                    return
                await actor.drain()
                actor = self._coordinator.actor_for_announcement(message_id)
                if actor is None:
                    return
                should_show_seed = (
                    not actor.is_substitute_recruitment_filled(message_id)
                    if self._coordinator.is_substitute_recruitment_message(message_id)
                    is True
                    else actor.active_user_count() == 0
                )
                channel = self.bot.get_channel(channel_id)
                if channel is None:
                    channel = await self.bot.fetch_channel(channel_id)
                if (
                    not isinstance(channel, discord.abc.Messageable)
                    or self.bot.user is None
                ):
                    raise TypeError(f"channel {channel_id} is not messageable")
                message = await channel.fetch_message(message_id)
                if should_show_seed:
                    await message.add_reaction("✅")
                else:
                    await message.remove_reaction("✅", self.bot.user)
                if version == self._seed_reaction_versions.get(message_id, 0):
                    return
        except (discord.HTTPException, TypeError):
            logger.warning(
                "failed to reconcile bot seed reaction channel_id=%s message_id=%s",
                channel_id,
                message_id,
                exc_info=True,
            )
        finally:
            current_task = asyncio.current_task()
            if self._seed_reaction_tasks.get(message_id) is current_task:
                self._seed_reaction_tasks.pop(message_id, None)

    @commands.Cog.listener()
    async def on_raw_reaction_remove(
        self, payload: discord.RawReactionActionEvent
    ) -> None:
        if payload.guild_id != self._config.guild_id or payload.emoji.name != "✅":
            return
        user = self.bot.get_user(payload.user_id)
        if user is not None and user.bot:
            return
        actor = self._coordinator.actor_for_announcement(payload.message_id)
        display_name = actor.display_name_for(payload.user_id) if actor else None
        accepted = self._coordinator.ingest_reaction(
            announcement_message_id=payload.message_id,
            discord_user_id=payload.user_id,
            discord_display_name=display_name or str(payload.user_id),
            action=ReactionAction.REMOVE,
            received_at=self._clock.now(),
        )
        if accepted:
            self._schedule_bot_seed_reaction_reconcile(
                payload.channel_id,
                payload.message_id,
            )

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if (
            message.guild is None
            or message.author.bot
            or message.guild.id != self._config.guild_id
            or message.channel.id != self._config.channels.tier
        ):
            return
        result = await self._tier_collector.submit_activity(
            guild_id=message.guild.id,
            channel_id=message.channel.id,
            discord_user_id=message.author.id,
            discord_display_name=getattr(
                message.author, "display_name", message.author.name
            ),
            raw_content=message.content,
            discord_message_id=message.id,
            activity_at=message.created_at,
            reply_to_message_id=(
                message.reference.message_id if message.reference is not None else None
            ),
        )
        if result.accepted:
            logger.info(
                "tier message create detected match_id=%s user_id=%s message_id=%s result=queued",
                result.session.id if result.session else None,
                message.author.id,
                message.id,
            )
        await self._send_tier_route_feedback(message, result)

    @commands.Cog.listener()
    async def on_raw_message_edit(self, payload: discord.RawMessageUpdateEvent) -> None:
        if (
            payload.guild_id != self._config.guild_id
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
        result = await self._tier_collector.submit_activity(
            guild_id=payload.guild_id,
            channel_id=payload.channel_id,
            discord_user_id=message.author.id,
            discord_display_name=getattr(
                message.author, "display_name", message.author.name
            ),
            raw_content=message.content,
            discord_message_id=message.id,
            activity_at=message.edited_at or self._clock.now(),
            reply_to_message_id=(
                message.reference.message_id if message.reference is not None else None
            ),
        )
        if result.accepted:
            logger.info(
                "tier message edit detected match_id=%s user_id=%s message_id=%s result=queued",
                result.session.id if result.session else None,
                message.author.id,
                message.id,
            )

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        await self._reconcile_deleted_announcements()

    @commands.Cog.listener()
    async def on_resumed(self) -> None:
        await self._reconcile_deleted_announcements()

    async def _reconcile_deleted_announcements(self) -> None:
        """중단 중 유실된 삭제 이벤트는 확실한 NotFound만 취소로 보완한다."""
        async with self._announcement_reconcile_lock:
            for session in self._coordinator.active_sessions:
                if session.announcement_message_id is None or self._clock.now() >= session.starts_at:
                    continue
                try:
                    channel = self.bot.get_channel(session.announcement_channel_id)
                    if channel is None:
                        channel = await self.bot.fetch_channel(session.announcement_channel_id)
                    await channel.fetch_message(session.announcement_message_id)
                except discord.NotFound:
                    await self._coordinator.cancel_match(session.id, queue_cleanup=True)
                except discord.HTTPException:
                    logger.warning("announcement existence check deferred match_id=%s", session.id, exc_info=True)

    @commands.Cog.listener()
    async def on_raw_message_delete(
        self, payload: discord.RawMessageDeleteEvent
    ) -> None:
        if payload.guild_id is None:
            return
        await self._handle_deleted_message(
            guild_id=payload.guild_id,
            channel_id=payload.channel_id,
            discord_message_id=payload.message_id,
        )

    @commands.Cog.listener()
    async def on_raw_bulk_message_delete(
        self,
        payload: discord.RawBulkMessageDeleteEvent,
    ) -> None:
        if payload.guild_id is None:
            return
        for message_id in payload.message_ids:
            await self._handle_deleted_message(
                guild_id=payload.guild_id,
                channel_id=payload.channel_id,
                discord_message_id=message_id,
            )

    async def _handle_deleted_message(
        self,
        *,
        guild_id: int,
        channel_id: int,
        discord_message_id: int,
    ) -> None:
        if guild_id != self._config.guild_id:
            return
        if channel_id == self._config.channels.announcement:
            actor = self._coordinator.actor_for_announcement(discord_message_id)
            if (
                actor is not None
                and actor.session.announcement_message_id == discord_message_id
                and self._clock.now() < actor.session.starts_at
            ):
                await self._coordinator.cancel_match(actor.session.id, queue_cleanup=True)
                return
        if channel_id != self._config.channels.tier:
            return
        now = self._clock.now()
        recreated = await self._repository.request_tier_anchor_recreation(
            discord_message_id,
            now,
        )
        if recreated is not None:
            self._coordinator.clear_tier_anchor_route(discord_message_id)
            logger.warning(
                "tier anchor deleted; recreation queued match_id=%s "
                "match_code=%s message_id=%s",
                recreated.id,
                recreated.match_code,
                discord_message_id,
            )
            return
        result = await self._tier_collector.submit_delete(
            guild_id=guild_id,
            channel_id=channel_id,
            discord_message_id=discord_message_id,
            received_at=now,
        )
        if result.accepted:
            logger.info(
                "tier message delete detected match_id=%s user_id=unavailable message_id=%s result=queued",
                result.session.id if result.session else None,
                discord_message_id,
            )

    async def _send_tier_route_feedback(
        self,
        message: discord.Message,
        result: TierRouteResult,
    ) -> None:
        content: str | None = None
        if result.status is TierRouteStatus.NOT_PARTICIPANT:
            content = (
                "해당 내전의 참가자 또는 대기자가 아니어서 티어를 저장하지 않았습니다."
            )
        elif result.status is TierRouteStatus.CLOSED:
            content = "해당 내전은 티어 작성 시간이 마감되어 저장하지 않았습니다."
        elif result.status is TierRouteStatus.COLLECTION_DISABLED:
            content = "티어 삭제 요청으로 이 내전의 수집이 중단되어 있습니다. 진행 방법은 내전 관리자에게 문의해 주세요."
        elif result.status is TierRouteStatus.BOUND_TO_OTHER_USER:
            content = "이 티어 메시지는 다른 작성자에게 귀속되어 수정할 수 없습니다."
        if content is None:
            return
        try:
            await message.reply(
                content,
                mention_author=False,
                allowed_mentions=allowed_mentions_for([]),
            )
        except discord.HTTPException:
            logger.warning(
                "failed to send tier routing guidance message_id=%s status=%s",
                message.id,
                result.status.value,
                exc_info=True,
            )
