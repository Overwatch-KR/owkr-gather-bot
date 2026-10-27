from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Sequence, TypeVar

import discord
from discord import app_commands
from discord.ext import commands

from src.application.coordinator import (
    CreateMatchRequest,
    DuplicateStartTime,
    DuplicateSourceRequest,
    SessionCoordinator,
)
from src.application.notification_worker import NotificationTransport, NotificationWorker
from src.application.rendering import (
    KST,
    NotificationRenderer,
    RecruitmentTemplateRenderer,
    RenderedNotification,
    format_discord_timestamp,
    format_korean_time,
)
from src.application.scheduler import MatchScheduler
from src.application.tier_collector import (
    TierCollector,
    TierRouteResult,
    TierRouteStatus,
)
from src.config import AppConfig
from src.domain.clock import Clock
from src.domain.models import (
    MatchSession,
    MatchStatus,
    ReactionAction,
    RosterStatus,
)
from src.infrastructure.persistence_writer import PersistenceWriter
from src.parsing.match_command import (
    CommandParseError,
    MatchCommandParser,
    ParsedMatchCommand,
    PastTimeError,
)
from src.ports.repositories import MatchRepository


logger = logging.getLogger(__name__)
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
        if value in ([], {}) or (value is False and key in {"required", "autocomplete"}):
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
            raise RuntimeError("Guild Voice States intent is required for lobby reminders")
        channel = self._bot.get_channel(channel_id)
        if channel is None:
            channel = await self._bot.fetch_channel(channel_id)
        if not isinstance(channel, (discord.VoiceChannel, discord.StageChannel)):
            raise TypeError(f"channel {channel_id} is not a voice channel")
        return set(channel.voice_states)


class EditMatchModal(discord.ui.Modal, title="내전 시간·모드 수정"):
    def __init__(self, view: CreateMatchConfirmationView) -> None:
        super().__init__(timeout=120)
        self._view = view
        self.time_input = discord.ui.TextInput(
            style=discord.TextStyle.short,
            custom_id="match_time",
            default=format_korean_time(view.parsed.starts_at),
            required=True,
            max_length=30,
        )
        self.mode_input = discord.ui.TextInput(
            style=discord.TextStyle.short,
            custom_id="match_mode",
            default=view.parsed.mode or None,
            required=False,
            max_length=100,
        )
        self.add_item(
            discord.ui.Label(
                text="시작 시간",
                description="예: 오후 8시, 20:00, 23시",
                component=self.time_input,
            )
        )
        self.add_item(
            discord.ui.Label(
                text="모드 (선택)",
                description="비워 두면 일반 내전으로 표시합니다.",
                component=self.mode_input,
            )
        )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self._view.requester_user_id:
            await interaction.response.send_message(
                "이 수정창은 명령을 실행한 관리자만 사용할 수 있습니다.",
                ephemeral=True,
            )
            return
        if self._view.finished:
            await interaction.response.send_message(
                "이미 처리된 내전 생성 요청입니다.",
                ephemeral=True,
            )
            return
        arguments = (
            f"{self.time_input.value} {self.mode_input.value or ''}".strip()
        )
        try:
            parsed = self._view.cog._parser.parse(
                arguments,
                now=self._view.cog._clock.now(),
                default_mode=None,
            )
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

        self._view.update_parsed(parsed)
        await interaction.response.defer()
        await self._view.refresh()


class CreateMatchConfirmationView(discord.ui.View):
    def __init__(
        self,
        cog: GatherCog,
        original_interaction: discord.Interaction,
        parsed: ParsedMatchCommand,
    ) -> None:
        super().__init__(timeout=60)
        self._cog = cog
        self._original_interaction = original_interaction
        self._requester_user_id = original_interaction.user.id
        self._parsed = parsed
        self._finished = False

    @property
    def cog(self) -> GatherCog:
        return self._cog

    @property
    def requester_user_id(self) -> int:
        return self._requester_user_id

    @property
    def parsed(self) -> ParsedMatchCommand:
        return self._parsed

    @property
    def finished(self) -> bool:
        return self._finished

    def update_parsed(self, parsed: ParsedMatchCommand) -> None:
        self._parsed = parsed

    def content(self) -> str:
        mode = (
            self._parsed.mode
            or self._cog._config.defaults.mode_display_fallback
            or "일반 내전"
        )
        _, lobby_name = self._cog._coordinator.lobby_assignment_for(
            self._parsed.starts_at
        )
        return (
            "**내전 생성 확인**\n\n"
            f"시작 · {format_discord_timestamp(self._parsed.starts_at, 'F')}\n"
            f"모드 · {mode}\n"
            f"티어 마감 · "
            f"{format_discord_timestamp(self._parsed.tier_deadline_at, 't')}\n"
            f"대기실 입장 · {format_discord_timestamp(self._parsed.lobby_at, 't')} "
            f"· {lobby_name}\n\n"
            "이 내용으로 모집 공지를 만들까요?"
        )

    async def refresh(self) -> None:
        await self._original_interaction.edit_original_response(
            content=self.content(),
            view=self,
            allowed_mentions=allowed_mentions_for([]),
        )

    @discord.ui.button(label="생성", style=discord.ButtonStyle.success)
    async def confirm(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        if interaction.user.id != self._requester_user_id:
            await interaction.response.send_message(
                "이 확인창은 명령을 실행한 관리자만 사용할 수 있습니다.",
                ephemeral=True,
            )
            return
        if self._finished:
            await interaction.response.defer()
            return
        self._finished = True
        self.stop()
        await interaction.response.defer()
        try:
            session = await self._cog.create_match(
                manager_user_id=self._requester_user_id,
                command_channel_id=self._original_interaction.channel_id,
                parsed=self._parsed,
                source_request_id=str(self._original_interaction.id),
                source_request_type="INTERACTION",
            )
        except DuplicateSourceRequest as exc:
            session = exc.session
        except DuplicateStartTime:
            await interaction.edit_original_response(
                content=(
                    "같은 시작 시각에 이미 활성 내전이 있습니다. "
                    "시간을 변경하거나 기존 내전을 취소한 뒤 다시 시도해 주세요."
                ),
                view=None,
                allowed_mentions=allowed_mentions_for([]),
            )
            return
        except Exception:
            logger.exception("match creation failed after confirmation")
            await interaction.edit_original_response(
                content="내전을 만들지 못했습니다. 잠시 후 다시 시도해 주세요.",
                view=None,
                allowed_mentions=allowed_mentions_for([]),
            )
            return

        if session.announcement_message_id is None:
            await interaction.edit_original_response(
                content="내전은 생성됐지만 모집 공지를 확인하지 못했습니다.",
                view=None,
                allowed_mentions=allowed_mentions_for([]),
            )
            return
        announcement_url = discord_message_url(
            session.guild_id,
            session.announcement_channel_id,
            session.announcement_message_id,
        )
        await interaction.edit_original_response(
            content=(
                f"✅ {format_discord_timestamp(session.starts_at, 't')} 내전을 만들었습니다. "
                f"[모집 공지 보기]({announcement_url})"
            ),
            view=None,
            allowed_mentions=allowed_mentions_for([]),
        )

    @discord.ui.button(label="시간·모드 수정", style=discord.ButtonStyle.primary)
    async def edit(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        if interaction.user.id != self._requester_user_id:
            await interaction.response.send_message(
                "이 확인창은 명령을 실행한 관리자만 사용할 수 있습니다.",
                ephemeral=True,
            )
            return
        if self._finished:
            await interaction.response.defer()
            return
        await interaction.response.send_modal(EditMatchModal(self))

    @discord.ui.button(label="취소", style=discord.ButtonStyle.secondary)
    async def cancel(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        if interaction.user.id != self._requester_user_id:
            await interaction.response.send_message(
                "이 확인창은 명령을 실행한 관리자만 사용할 수 있습니다.",
                ephemeral=True,
            )
            return
        self._finished = True
        self.stop()
        await interaction.response.defer()
        await self._delete_original_response()

    async def on_timeout(self) -> None:
        if not self._finished:
            await self._delete_original_response()

    async def _delete_original_response(self) -> None:
        try:
            await self._original_interaction.delete_original_response()
        except discord.HTTPException:
            logger.debug("failed to delete expired match confirmation", exc_info=True)


class CancelMatchConfirmationView(discord.ui.View):
    def __init__(
        self,
        cog: GatherCog,
        original_interaction: discord.Interaction,
        session: MatchSession,
    ) -> None:
        super().__init__(timeout=60)
        self._cog = cog
        self._original_interaction = original_interaction
        self._requester_user_id = original_interaction.user.id
        self._session = session
        self._finished = False

    @discord.ui.button(label="내전 취소", style=discord.ButtonStyle.danger)
    async def confirm(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        if interaction.user.id != self._requester_user_id:
            await interaction.response.send_message(
                "이 확인창은 명령을 실행한 관리자만 사용할 수 있습니다.",
                ephemeral=True,
            )
            return
        if self._finished:
            await interaction.response.defer()
            return
        self._finished = True
        self.stop()
        await interaction.response.defer()
        substitute_message_ids = tuple(
            getattr(
                self._cog._coordinator,
                "substitute_message_ids_for_match",
                lambda match_id: (),
            )(self._session.id)
        )
        canceled = await self._cog._coordinator.cancel_match(self._session.id)
        if canceled is None:
            await interaction.edit_original_response(
                content="이미 시작됐거나 취소된 내전입니다.",
                view=None,
                allowed_mentions=allowed_mentions_for([]),
            )
            return
        announcement_deleted = await self._cog._delete_announcement_message(canceled)
        anchor_deleted = await self._cog._delete_tier_anchor_message(canceled)
        substitutes_deleted = (
            await self._cog._delete_substitute_recruitment_messages(
                canceled,
                substitute_message_ids,
            )
            if substitute_message_ids
            else True
        )
        if announcement_deleted and anchor_deleted and substitutes_deleted:
            await self._delete_original_response()
            return
        await interaction.edit_original_response(
            content=(
                "내전은 취소했지만 일부 봇 메시지를 지우지 못했습니다. "
                "봇의 채널 보기·메시지 기록 보기 권한을 확인하고 남은 메시지를 직접 정리해 주세요."
            ),
            view=None,
            allowed_mentions=allowed_mentions_for([]),
        )

    @discord.ui.button(label="돌아가기", style=discord.ButtonStyle.secondary)
    async def cancel(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        if interaction.user.id != self._requester_user_id:
            await interaction.response.send_message(
                "이 확인창은 명령을 실행한 관리자만 사용할 수 있습니다.",
                ephemeral=True,
            )
            return
        self._finished = True
        self.stop()
        await interaction.response.defer()
        await self._delete_original_response()

    async def on_timeout(self) -> None:
        if not self._finished:
            await self._delete_original_response()

    async def _delete_original_response(self) -> None:
        try:
            await self._original_interaction.delete_original_response()
        except discord.HTTPException:
            logger.debug("failed to delete match cancellation confirmation", exc_info=True)


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
        self._seed_reaction_versions: dict[int, int] = {}
        self._seed_reaction_tasks: dict[int, asyncio.Task[None]] = {}

    def _authorized(self, interaction: discord.Interaction) -> bool:
        return _management_authorization_error(self._config, interaction) is None

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
    )
    async def create_match_command(
        self,
        interaction: discord.Interaction,
        시간: str,
        모드: str | None = None,
    ) -> None:
        manager = self._config.manager(interaction.user.id)
        arguments = f"{시간} {모드 or ''}".strip()
        try:
            parsed = self._parser.parse(
                arguments,
                now=self._clock.now(),
                default_mode=manager.default_mode,
            )
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
        candidate = local_now.replace(second=0, microsecond=0) + timedelta(minutes=next_minute)
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
        session = await self._coordinator.create_match(
            CreateMatchRequest(
                manager_user_id=manager_user_id,
                command_channel_id=command_channel_id,
                parsed=parsed,
                source_request_id=source_request_id,
                source_request_type=source_request_type,
            )
        )
        message: discord.Message | None = None
        try:
            channel = self.bot.get_channel(session.announcement_channel_id)
            if channel is None:
                channel = await self.bot.fetch_channel(session.announcement_channel_id)
            if not isinstance(channel, discord.abc.Messageable):
                raise TypeError("announcement channel is not messageable")
            description = self._template.render(
                session, self._config.manager(manager_user_id)
            )
            role_id = self._config.defaults.recruitment_role_id
            role_mention = f"<@&{role_id}> " if role_id else ""
            lead = (
                f"{role_mention}<@{manager_user_id}>님이 여는 "
                f"**{format_discord_timestamp(session.starts_at, 't')} 내전**!\n"
                "참가하려면 아래 ✅을 눌러 주세요."
            )
            message = await channel.send(
                content=lead,
                embed=build_recruitment_embed(self._config, description),
                allowed_mentions=allowed_mentions_for([], [role_id] if role_id else []),
            )
            await self._coordinator.activate(session, message.id)
            await message.add_reaction("✅")
            return session
        except Exception:
            await self._coordinator.cancel_match(session.id)
            if message is not None:
                try:
                    await message.delete()
                except discord.HTTPException:
                    logger.warning(
                        "failed to delete incomplete recruitment message "
                        "match_id=%s message_id=%s",
                        session.id,
                        message.id,
                        exc_info=True,
                    )
            raise

    @app_commands.command(name="티어현황", description="내전 참가·대기와 티어 작성 현황을 함께 확인합니다.")
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
        confirmed = [entry for entry in roster if entry.status is RosterStatus.CONFIRMED]
        waitlisted = [entry for entry in roster if entry.status is RosterStatus.WAITLISTED]
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
            completed = [item.discord_display_name for item in statuses if item.has_tier]
            missing = [item.discord_display_name for item in statuses if not item.has_tier]
            content += (
                f"\n티어 작성 {len(completed)}/{len(statuses)}명\n"
                f"완료 · {', '.join(completed) or '-'}\n"
                f"미작성 · {', '.join(missing) or '-'}"
            )
        conflicts = [entry.discord_display_name for entry in waitlisted
                     if entry.conflict_match_id is not None]
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
                channel = await self.bot.fetch_channel(
                    session.announcement_channel_id
                )
            if not isinstance(channel, discord.abc.Messageable):
                raise TypeError("announcement channel is not messageable")
            role_id = self._config.defaults.recruitment_role_id
            role_mention = f"<@&{role_id}> " if role_id else ""
            message = await channel.send(
                content=(
                    f"{role_mention}**"
                    f"{format_discord_timestamp(session.starts_at, 't')} "
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
        mode = session.mode or self._config.defaults.mode_display_fallback or "일반 내전"
        return (
            f"{local.month}/{local.day} {format_korean_time(local)} · "
            f"{manager_name} · {mode} · {session.match_code}"
        )

    @staticmethod
    def _match_status_time_label(session: MatchSession) -> str:
        return format_korean_time(session.starts_at.astimezone(KST))

    async def _delete_announcement_message(self, session: MatchSession) -> bool:
        if session.announcement_message_id is None:
            return True
        try:
            channel = self.bot.get_channel(session.announcement_channel_id)
            if channel is None:
                channel = await self.bot.fetch_channel(session.announcement_channel_id)
            if not isinstance(channel, discord.abc.Messageable):
                raise TypeError(f"channel {session.announcement_channel_id} is not messageable")
            message = await channel.fetch_message(session.announcement_message_id)
            await message.delete()
            return True
        except discord.NotFound:
            return True
        except (discord.Forbidden, discord.HTTPException, TypeError):
            logger.warning(
                "failed to delete canceled recruitment message match_id=%s channel_id=%s message_id=%s",
                session.id,
                session.announcement_channel_id,
                session.announcement_message_id,
                exc_info=True,
            )
            return False

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
                channel = await self.bot.fetch_channel(
                    session.announcement_channel_id
                )
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

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent) -> None:
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
            self._schedule_bot_seed_reaction_reconcile(payload.channel_id, payload.message_id)

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
                    if self._coordinator.is_substitute_recruitment_message(
                        message_id
                    ) is True
                    else actor.active_user_count() == 0
                )
                channel = self.bot.get_channel(channel_id)
                if channel is None:
                    channel = await self.bot.fetch_channel(channel_id)
                if not isinstance(channel, discord.abc.Messageable) or self.bot.user is None:
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
    async def on_raw_reaction_remove(self, payload: discord.RawReactionActionEvent) -> None:
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
            discord_display_name=getattr(message.author, "display_name", message.author.name),
            raw_content=message.content,
            discord_message_id=message.id,
            activity_at=message.created_at,
            reply_to_message_id=(
                message.reference.message_id
                if message.reference is not None
                else None
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
            discord_display_name=getattr(message.author, "display_name", message.author.name),
            raw_content=message.content,
            discord_message_id=message.id,
            activity_at=message.edited_at or self._clock.now(),
            reply_to_message_id=(
                message.reference.message_id
                if message.reference is not None
                else None
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
    async def on_raw_message_delete(self, payload: discord.RawMessageDeleteEvent) -> None:
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
        if (
            guild_id != self._config.guild_id
            or channel_id != self._config.channels.tier
        ):
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
            content = "해당 내전의 참가자 또는 대기자가 아니어서 티어를 저장하지 않았습니다."
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
        super().__init__(command_prefix=commands.when_mentioned, intents=intents, help_command=None)
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
                self.template_path,
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
            _normalized_command_payload(command.to_dict()) for command in remote_commands
        ]
        sort_key = lambda item: (item.get("type", 1), item.get("name", ""))
        if sorted(local_payloads, key=sort_key) == sorted(remote_payloads, key=sort_key):
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
            asyncio.create_task(self._catch_up_tier_messages(), name="tier-message-catch-up")

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
        async for message in channel.history(limit=None, before=before, oldest_first=False):
            if message.author.bot:
                continue
            activity_at = message.edited_at or message.created_at
            if not earliest_opened_at <= activity_at < before:
                continue
            await self.tier_collector.submit_activity(
                guild_id=self.app_config.guild_id,
                channel_id=self.app_config.channels.tier,
                discord_user_id=message.author.id,
                discord_display_name=getattr(message.author, "display_name", message.author.name),
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
