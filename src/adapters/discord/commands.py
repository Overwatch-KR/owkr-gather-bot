"""내전 관리 명령과 조회 응답. 생성의 생명주기 조율은 RecruitmentService에 위임한다."""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from src.application.coordinator import SessionCoordinator
from src.application.recruitment import RecruitmentService
from src.application.rendering import KST, format_match_time, format_korean_time
from src.application.tier_collector import TierCollector
from src.config import AppConfig
from src.domain.clock import Clock
from src.domain.models import MatchSession, RosterStatus
from src.parsing.match_command import (
    CommandParseError,
    MatchCommandParser,
    ParsedMatchCommand,
    PastTimeError,
)
from src.ports.persistence import MutationWriter
from src.ports.repositories import MatchRepository

from .common import (
    ManagementCommandCheckFailure,
    _management_authorization_error,
    allowed_mentions_for,
    build_substitute_recruitment_embed,
    discord_message_url,
    management_command_only,
    match_status_label,
)
from .events import DiscordEventHandlers
from .views import CancelMatchConfirmationView, CreateMatchConfirmationView

if TYPE_CHECKING:
    from .bot import GatherBot
logger = logging.getLogger(__name__)


class GatherCog(DiscordEventHandlers, commands.Cog):
    """관리자 명령과 Discord 이벤트를 한 Cog로 등록한다."""

    def __init__(
        self,
        bot: GatherBot,
        config: AppConfig,
        repository: MatchRepository,
        writer: MutationWriter,
        coordinator: SessionCoordinator,
        tier_collector: TierCollector,
        clock: Clock,
        recruitment: RecruitmentService,
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
        self._recruitment = recruitment
        self._announcement_reconcile_lock = asyncio.Lock()
        self._seed_reaction_versions: dict[int, int] = {}
        self._seed_reaction_tasks: dict[int, asyncio.Task[None]] = {}

    def _authorized(self, interaction: discord.Interaction) -> bool:
        return _management_authorization_error(self._config, interaction) is None

    async def cog_unload(self) -> None:
        """DB와 Actor 종료 전에 반응 표시 보정 작업을 멈춘다."""
        tasks = tuple(self._seed_reaction_tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._seed_reaction_tasks.clear()
        self._seed_reaction_versions.clear()

    async def _require_manager(self, interaction: discord.Interaction) -> bool:
        error = _management_authorization_error(self._config, interaction)
        if error is None:
            return True
        logger.warning(
            "unauthorized management command user_id=%s guild_id=%s channel_id=%s command=%s",
            interaction.user.id,
            interaction.guild_id,
            interaction.channel_id,
            interaction.command.name if interaction.command else None,
        )
        await interaction.response.send_message(
            error,
            ephemeral=True,
            allowed_mentions=allowed_mentions_for([]),
        )
        return False

    async def cog_app_command_error(
        self,
        interaction: discord.Interaction,
        error: app_commands.AppCommandError,
    ) -> None:
        if not isinstance(error, ManagementCommandCheckFailure):
            raise error
        logger.warning(
            "management command check failed user_id=%s guild_id=%s channel_id=%s command=%s",
            interaction.user.id,
            interaction.guild_id,
            interaction.channel_id,
            interaction.command.name if interaction.command else None,
        )
        kwargs = {
            "content": error.user_message,
            "ephemeral": True,
            "allowed_mentions": allowed_mentions_for([]),
        }
        if interaction.response.is_done():
            await interaction.followup.send(**kwargs)
        else:
            await interaction.response.send_message(**kwargs)

    @app_commands.command(name="내전", description="새 내전 모집을 생성합니다.")
    @app_commands.guild_only()
    @management_command_only()
    @app_commands.describe(
        시간="시작 시각입니다. 예: 오후 6시 20분, 18:20, 23시",
        모드="선택 사항입니다. 예: 6ㄷ6클래식",
        취소시각="선택 사항입니다. 이 시각까지 10명이 모이지 않으면 취소합니다. 예: 오후 7시 30분",
    )
    async def create_match_command(
        self,
        interaction: discord.Interaction,
        시간: str,
        모드: str | None = None,
        취소시각: str | None = None,
    ) -> None:
        manager = self._config.manager(interaction.user.id)
        arguments = f"{시간} {모드 or ''}".strip()
        try:
            parsed = self._parser.parse(
                arguments,
                now=self._clock.now(),
                default_mode=manager.default_mode,
            )
            parsed = self._parser.with_cancellation_time(parsed, 취소시각, now=self._clock.now())
        except PastTimeError:
            await interaction.response.send_message(
                "입력한 시각이 이미 지났습니다. 미래 시각으로 다시 입력해 주세요.",
                ephemeral=True,
                allowed_mentions=allowed_mentions_for([]),
            )
            return
        except CommandParseError as exc:
            await interaction.response.send_message(
                str(exc),
                ephemeral=True,
                allowed_mentions=allowed_mentions_for([]),
            )
            return

        view = CreateMatchConfirmationView(self, interaction, parsed)
        await interaction.response.send_message(
            view.content(),
            view=view,
            ephemeral=True,
            allowed_mentions=allowed_mentions_for([]),
        )

    @create_match_command.autocomplete("시간")
    async def match_time_autocomplete(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[str]]:
        if not self._authorized(interaction):
            return []
        local_now = self._clock.now().astimezone(KST)
        next_minute = 10 - (local_now.minute % 10)
        candidate = local_now.replace(second=0, microsecond=0) + timedelta(
            minutes=next_minute
        )
        needle = current.replace(" ", "").casefold()
        choices: list[app_commands.Choice[str]] = []
        while candidate.date() == local_now.date() and len(choices) < 25:
            display = format_korean_time(candidate)
            if not needle or needle in display.replace(" ", "").casefold():
                choices.append(app_commands.Choice(name=display, value=display))
            candidate += timedelta(minutes=10)
        return choices

    @create_match_command.autocomplete("모드")
    async def match_mode_autocomplete(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[str]]:
        if not self._authorized(interaction):
            return []
        manager_default = self._config.manager(interaction.user.id).default_mode
        candidates = [manager_default, "6ㄷ6클래식", "일반 내전"]
        needle = current.replace(" ", "").casefold()
        values: list[str] = []
        for candidate in candidates:
            if not candidate or candidate in values:
                continue
            if needle and needle not in candidate.replace(" ", "").casefold():
                continue
            values.append(candidate)
        return [app_commands.Choice(name=value, value=value) for value in values]

    async def create_match(
        self,
        *,
        manager_user_id: int,
        command_channel_id: int,
        parsed: ParsedMatchCommand,
        source_request_id: str | None = None,
        source_request_type: str | None = None,
    ) -> MatchSession:
        return await self._recruitment.create(
            manager_user_id=manager_user_id,
            command_channel_id=command_channel_id,
            parsed=parsed,
            source_request_id=source_request_id,
            source_request_type=source_request_type,
        )

    @app_commands.command(
        name="티어현황",
        description="내전 참가·대기와 티어 작성 현황을 함께 확인합니다.",
    )
    @app_commands.guild_only()
    @management_command_only()
    @app_commands.describe(내전="내전 코드 또는 목록에서 선택합니다.")
    async def tier_status(
        self,
        interaction: discord.Interaction,
        내전: str | None = None,
    ) -> None:
        session = await self._select_active_match(내전, allow_single_default=True)
        if session is None:
            sessions = self._coordinator.active_sessions
            content = (
                "활성 내전이 없습니다."
                if not sessions
                else "활성 내전이 여러 개입니다. `내전` 옵션에서 확인할 내전을 선택해 주세요."
            )
            await interaction.response.send_message(content, ephemeral=True)
            return
        actor = self._coordinator.actor_for_match(session.id)
        if actor is not None:
            await actor.drain()
        await self._writer.flush()
        roster = await self._repository.load_roster(session.id)
        confirmed = [
            entry for entry in roster if entry.status is RosterStatus.CONFIRMED
        ]
        waitlisted = [
            entry for entry in roster if entry.status is RosterStatus.WAITLISTED
        ]
        content = (
            f"**{self._match_label(session)}**\n"
            f"상태 · {match_status_label(session.status)}\n"
            f"참가 · {len(confirmed)}/{session.participant_limit}명\n"
            f"대기 · {len(waitlisted)}명"
        )
        if session.recruitment_completed_notified_at is None:
            content += "\n티어 작성은 모집 완료 공지가 전송된 뒤 시작됩니다."
        else:
            statuses = await self._repository.get_tier_status(session.id)
            completed = [
                item.discord_display_name for item in statuses if item.has_tier
            ]
            missing = [
                item.discord_display_name for item in statuses if not item.has_tier
            ]
            content += (
                f"\n티어 작성 {len(completed)}/{len(statuses)}명\n"
                f"완료 · {', '.join(completed) or '-'}\n"
                f"미작성 · {', '.join(missing) or '-'}"
            )
        conflicts = [
            entry.discord_display_name
            for entry in waitlisted
            if entry.conflict_match_id is not None
        ]
        if conflicts:
            content += f"\n같은 시각의 다른 내전과 충돌 · {', '.join(conflicts)}"
        await interaction.response.send_message(
            content,
            ephemeral=True,
            allowed_mentions=allowed_mentions_for([]),
        )

    @tier_status.autocomplete("내전")
    async def tier_status_match_autocomplete(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[str]]:
        return await self._match_autocomplete(interaction, current)

    @app_commands.command(
        name="내전대타",
        description="선택한 내전에 연결된 1명짜리 대타 모집을 엽니다.",
    )
    @app_commands.guild_only()
    @management_command_only()
    @app_commands.describe(
        내전="대타를 모집할 내전을 선택합니다.",
    )
    async def open_substitute_recruitment(
        self,
        interaction: discord.Interaction,
        내전: str,
    ) -> None:
        session = await self._select_active_match(내전, allow_single_default=False)
        if session is None:
            await interaction.response.send_message(
                "선택한 내전을 찾을 수 없습니다.",
                ephemeral=True,
            )
            return
        actor = self._coordinator.actor_for_match(session.id)
        if actor is None or session.full_reached_at is None:
            await interaction.response.send_message(
                "모집이 완료된 활성 내전에서만 대타 모집을 열 수 있습니다.",
                ephemeral=True,
            )
            return
        if session.recruitment_completed_notified_at is None:
            await interaction.response.send_message(
                "모집 완료 공지가 전송된 뒤 대타 모집을 열 수 있습니다.",
                ephemeral=True,
            )
            return
        if actor.current_waitlist():
            await interaction.response.send_message(
                "이미 대기자가 있습니다. 기존 대기열을 먼저 확인해 주세요.",
                ephemeral=True,
            )
            return
        if await self._repository.get_open_substitute_recruitment(session.id):
            await interaction.response.send_message(
                "이미 진행 중인 대타 모집이 있습니다.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True)
        message: discord.Message | None = None
        try:
            channel = self.bot.get_channel(session.announcement_channel_id)
            if channel is None:
                channel = await self.bot.fetch_channel(session.announcement_channel_id)
            if not isinstance(channel, discord.abc.Messageable):
                raise TypeError("announcement channel is not messageable")
            role_id = self._config.defaults.recruitment_role_id
            role_mention = f"<@&{role_id}> " if role_id else ""
            message = await channel.send(
                content=(
                    f"{role_mention}**"
                    f"{format_match_time(session.starts_at)} "
                    "내전 대타 1명 모집!**\n"
                    "참가하려면 아래 ✅을 눌러 주세요."
                ),
                embed=build_substitute_recruitment_embed(session),
                allowed_mentions=allowed_mentions_for(
                    [],
                    [role_id] if role_id else [],
                ),
            )
            await self._coordinator.register_substitute_recruitment(
                match_id=session.id,
                discord_message_id=message.id,
            )
            await message.add_reaction("✅")
        except ValueError as exc:
            if message is not None:
                await self._coordinator.cancel_substitute_recruitment(message.id)
                try:
                    await message.delete()
                except discord.HTTPException:
                    logger.warning(
                        "failed to delete rejected substitute recruitment "
                        "message_id=%s",
                        message.id,
                        exc_info=True,
                    )
            await interaction.followup.send(
                str(exc),
                ephemeral=True,
                allowed_mentions=allowed_mentions_for([]),
            )
            return
        except Exception:
            logger.exception(
                "failed to create substitute recruitment match_id=%s",
                session.id,
            )
            if message is not None:
                await self._coordinator.cancel_substitute_recruitment(message.id)
                try:
                    await message.delete()
                except discord.HTTPException:
                    logger.warning(
                        "failed to delete incomplete substitute recruitment "
                        "message_id=%s",
                        message.id,
                        exc_info=True,
                    )
            await interaction.followup.send(
                "대타 모집을 만들지 못했습니다. 잠시 후 다시 시도해 주세요.",
                ephemeral=True,
                allowed_mentions=allowed_mentions_for([]),
            )
            return

        recruitment_url = discord_message_url(
            session.guild_id,
            session.announcement_channel_id,
            message.id,
        )
        await interaction.followup.send(
            (
                f"✅ {self._match_status_time_label(session)} 대타 모집을 열었습니다. "
                f"[모집 공지 보기]({recruitment_url})"
            ),
            ephemeral=True,
            allowed_mentions=allowed_mentions_for([]),
        )

    @open_substitute_recruitment.autocomplete("내전")
    async def open_substitute_recruitment_match_autocomplete(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[str]]:
        return await self._match_autocomplete(interaction, current)

    @app_commands.command(name="내전취소", description="선택한 내전을 취소합니다.")
    @app_commands.guild_only()
    @management_command_only()
    @app_commands.describe(내전="취소할 내전을 선택합니다.")
    async def cancel_match(
        self,
        interaction: discord.Interaction,
        내전: str,
    ) -> None:
        session = await self._select_active_match(내전, allow_single_default=False)
        if session is None:
            await interaction.response.send_message(
                "선택한 내전을 찾을 수 없습니다.",
                ephemeral=True,
            )
            return
        await interaction.response.send_message(
            (
                "**내전 취소 확인**\n\n"
                f"{self._match_label(session)}\n\n"
                "모집 공지와 예약된 안내도 함께 정리됩니다."
            ),
            view=CancelMatchConfirmationView(self, interaction, session),
            ephemeral=True,
            allowed_mentions=allowed_mentions_for([]),
        )

    @cancel_match.autocomplete("내전")
    async def cancel_match_match_autocomplete(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[str]]:
        return await self._match_autocomplete(interaction, current)

    async def _select_active_match(
        self,
        value: str | None,
        *,
        allow_single_default: bool,
    ) -> MatchSession | None:
        sessions = self._coordinator.active_sessions
        if value:
            normalized = value.strip()
            for session in sessions:
                if session.id == normalized or session.match_code == normalized.upper():
                    return session
            return None
        if allow_single_default and len(sessions) == 1:
            return sessions[0]
        return None

    async def _match_autocomplete(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[str]]:
        if not self._authorized(interaction):
            return []
        needle = current.strip().casefold()
        choices: list[app_commands.Choice[str]] = []
        for session in self._coordinator.active_sessions:
            label = self._match_label(session)
            searchable = label.casefold()
            if needle and needle not in searchable:
                continue
            choices.append(
                app_commands.Choice(name=label[:100], value=session.match_code)
            )
            if len(choices) == 25:
                break
        return choices

    def _match_label(self, session: MatchSession) -> str:
        local = session.starts_at.astimezone(KST)
        member = None
        guild = self.bot.get_guild(session.guild_id)
        if guild is not None:
            member = guild.get_member(session.manager_user_id)
        user = self.bot.get_user(session.manager_user_id)
        manager_name = (
            getattr(member, "display_name", None)
            or getattr(user, "display_name", None)
            or getattr(user, "name", None)
            or str(session.manager_user_id)
        )
        mode = (
            session.mode or self._config.defaults.mode_display_fallback or "일반 내전"
        )
        return (
            f"{format_korean_time(local)} · "
            f"{manager_name} · {mode} · {session.lobby_name or self._config.defaults.lobby_name}"
        )

    @staticmethod
    def _match_status_time_label(session: MatchSession) -> str:
        return format_korean_time(session.starts_at.astimezone(KST))

    async def _replace_announcement_with_cancellation(self, session: MatchSession) -> tuple[bool, bool]:
        """봇 자신의 모집 공지를 지워 반응을 제거하고, 취소 안내를 별도로 남긴다."""
        deleted = session.announcement_message_id is None
        try:
            channel = self.bot.get_channel(session.announcement_channel_id)
            if channel is None:
                channel = await self.bot.fetch_channel(session.announcement_channel_id)
            if not isinstance(channel, discord.abc.Messageable):
                raise TypeError("announcement channel is not messageable")
        except (discord.HTTPException, TypeError):
            logger.warning("failed to access cancellation channel match_id=%s", session.id, exc_info=True)
            return False, deleted
        if not deleted:
            try:
                message = await channel.fetch_message(session.announcement_message_id)
                await message.delete()
                deleted = True
            except discord.NotFound:
                deleted = True
            except discord.HTTPException:
                logger.warning("failed to delete canceled announcement match_id=%s", session.id, exc_info=True)
        try:
            await channel.send(
                content=(
                    "**내전 취소**\n"
                    f"{format_match_time(session.starts_at)} 내전이 취소되었습니다."
                    + (f"\n대기실 · {session.lobby_name}" if session.lobby_name else "")
                ),
                allowed_mentions=allowed_mentions_for([]),
            )
            return True, deleted
        except discord.HTTPException:
            logger.warning("failed to send cancellation notice match_id=%s", session.id, exc_info=True)
            return False, deleted

    async def _delete_tier_anchor_message(self, session: MatchSession) -> bool:
        if session.tier_anchor_message_id is None:
            return True
        try:
            channel = self.bot.get_channel(session.tier_channel_id)
            if channel is None:
                channel = await self.bot.fetch_channel(session.tier_channel_id)
            if not isinstance(channel, discord.abc.Messageable):
                raise TypeError(f"channel {session.tier_channel_id} is not messageable")
            message = await channel.fetch_message(session.tier_anchor_message_id)
            await message.delete()
            return True
        except discord.NotFound:
            return True
        except (discord.Forbidden, discord.HTTPException, TypeError):
            logger.warning(
                "failed to delete tier anchor match_id=%s channel_id=%s message_id=%s",
                session.id,
                session.tier_channel_id,
                session.tier_anchor_message_id,
                exc_info=True,
            )
            return False

    async def _delete_substitute_recruitment_messages(
        self,
        session: MatchSession,
        message_ids: tuple[int, ...],
    ) -> bool:
        if not message_ids:
            return True
        try:
            channel = self.bot.get_channel(session.announcement_channel_id)
            if channel is None:
                channel = await self.bot.fetch_channel(session.announcement_channel_id)
            if not isinstance(channel, discord.abc.Messageable):
                raise TypeError(
                    f"channel {session.announcement_channel_id} is not messageable"
                )
            for message_id in message_ids:
                try:
                    message = await channel.fetch_message(message_id)
                    await message.delete()
                except discord.NotFound:
                    continue
            return True
        except (discord.Forbidden, discord.HTTPException, TypeError):
            logger.warning(
                "failed to delete substitute recruitment messages "
                "match_id=%s message_ids=%s",
                session.id,
                message_ids,
                exc_info=True,
            )
            return False
