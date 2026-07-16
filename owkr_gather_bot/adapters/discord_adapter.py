from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Sequence, TypeVar

import discord
from discord import app_commands
from discord.ext import commands
from jinja2 import TemplateError
from yaml import YAMLError

from owkr_gather_bot.application.coordinator import CreateMatchRequest, SessionCoordinator
from owkr_gather_bot.application.notification_worker import NotificationTransport, NotificationWorker
from owkr_gather_bot.application.recruitment_complete_editor import (
    RecruitmentCompleteCopy,
    build_recruitment_complete_template,
    dump_recruitment_complete_copy,
    load_recruitment_complete_copy,
    recruitment_complete_copy_path,
)
from owkr_gather_bot.application.rendering import (
    KST,
    NotificationRenderer,
    RecruitmentTemplateRenderer,
    RenderedNotification,
    format_discord_timestamp,
    format_korean_time,
    format_korean_datetime,
)
from owkr_gather_bot.application.scheduler import MatchScheduler
from owkr_gather_bot.application.tier_collector import TierCollector
from owkr_gather_bot.config import AppConfig
from owkr_gather_bot.domain.clock import Clock
from owkr_gather_bot.domain.models import MatchSession, ReactionAction
from owkr_gather_bot.infrastructure.persistence_writer import PersistenceWriter
from owkr_gather_bot.parsing.match_command import (
    CommandParseError,
    MatchCommandParser,
    ParsedMatchCommand,
    PastTimeError,
)
from owkr_gather_bot.ports.repositories import MatchRepository


logger = logging.getLogger(__name__)
CommandType = TypeVar("CommandType")


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


class RecruitmentCompleteCopyModal(discord.ui.Modal):
    def __init__(self, cog: GatherCog, copy: RecruitmentCompleteCopy) -> None:
        super().__init__(title="모집 완료 공지 문구")
        self._cog = cog
        self.start_heading_input = discord.ui.TextInput(
            style=discord.TextStyle.short,
            custom_id="start_heading",
            default=copy.start_heading,
            required=True,
            max_length=50,
        )
        self.tier_heading_input = discord.ui.TextInput(
            style=discord.TextStyle.short,
            custom_id="tier_heading",
            default=copy.tier_heading,
            required=True,
            max_length=50,
        )
        self.tier_instruction_input = discord.ui.TextInput(
            style=discord.TextStyle.paragraph,
            custom_id="tier_instruction",
            default=copy.tier_instruction,
            required=True,
            max_length=300,
        )
        self.tier_format_example_input = discord.ui.TextInput(
            style=discord.TextStyle.paragraph,
            custom_id="tier_format_example",
            default=copy.tier_format_example,
            required=True,
            max_length=800,
        )
        self.extra_notice_input = discord.ui.TextInput(
            style=discord.TextStyle.paragraph,
            custom_id="extra_notice",
            default=copy.extra_notice or None,
            required=False,
            max_length=500,
        )
        self.add_item(
            discord.ui.Label(
                text="시작 제목",
                description="시작 시간은 봇이 자동으로 넣습니다.",
                component=self.start_heading_input,
            )
        )
        self.add_item(
            discord.ui.Label(
                text="티어 작성 제목",
                component=self.tier_heading_input,
            )
        )
        self.add_item(
            discord.ui.Label(
                text="티어 작성 안내",
                description="티어 채널 멘션은 봇이 앞에 자동으로 붙입니다.",
                component=self.tier_instruction_input,
            )
        )
        self.add_item(
            discord.ui.Label(
                text="작성 양식과 예시",
                description="관리자가 참가자에게 보여 줄 예시만 입력하세요.",
                component=self.tier_format_example_input,
            )
        )
        self.add_item(
            discord.ui.Label(
                text="추가 안내 (선택)",
                description="마감·대기실 시각과 매너 안내는 자동으로 표시됩니다.",
                component=self.extra_notice_input,
            )
        )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not await self._cog._require_manager(interaction):
            return
        copy = RecruitmentCompleteCopy(
            start_heading=str(self.start_heading_input.value),
            tier_heading=str(self.tier_heading_input.value),
            tier_instruction=str(self.tier_instruction_input.value),
            tier_format_example=str(self.tier_format_example_input.value),
            extra_notice=str(self.extra_notice_input.value or ""),
        )
        try:
            self._cog.save_recruitment_complete_copy(copy)
        except (TemplateError, ValueError) as exc:
            await interaction.response.send_message(
                f"공지 문구를 저장할 수 없습니다: {exc}",
                ephemeral=True,
                allowed_mentions=allowed_mentions_for([]),
            )
            return
        except OSError:
            logger.exception("failed to save recruitment complete copy")
            await interaction.response.send_message(
                "공지 문구 파일을 저장하지 못했습니다. 관리자 로그를 확인해 주세요.",
                ephemeral=True,
                allowed_mentions=allowed_mentions_for([]),
            )
            return
        await interaction.response.send_message(
            "모집 완료 공지 문구를 저장했습니다. 시간·채널·일정은 봇이 자동으로 "
            "넣습니다. `/모집완료미리보기`에서 결과를 확인해 주세요.",
            ephemeral=True,
            allowed_mentions=allowed_mentions_for([]),
        )


class RecruitmentCompleteAdvancedTemplateModal(discord.ui.Modal):
    def __init__(self, cog: GatherCog, current_template: str) -> None:
        super().__init__(title="모집 완료 고급 편집")
        self._cog = cog
        self.template_input = discord.ui.TextInput(
            style=discord.TextStyle.paragraph,
            custom_id="recruitment_complete_template",
            default=current_template,
            required=True,
            max_length=4000,
        )
        self.add_item(
            discord.ui.Label(
                text="개발자용 템플릿",
                description=(
                    "Jinja 변수를 직접 다룹니다. 일반 관리자는 간단 편집을 사용하세요."
                ),
                component=self.template_input,
            )
        )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not await self._cog._require_manager(interaction):
            return
        source = str(self.template_input.value).strip()
        try:
            NotificationRenderer.validate_recruitment_complete_template(
                source,
                self._cog._config,
            )
            self._cog.save_recruitment_complete_template(source)
        except (TemplateError, ValueError) as exc:
            await interaction.response.send_message(
                f"템플릿을 저장할 수 없습니다: {exc}",
                ephemeral=True,
                allowed_mentions=allowed_mentions_for([]),
            )
            return
        except OSError:
            logger.exception("failed to save recruitment complete template")
            await interaction.response.send_message(
                "템플릿 파일을 저장하지 못했습니다. 관리자 로그를 확인해 주세요.",
                ephemeral=True,
                allowed_mentions=allowed_mentions_for([]),
            )
            return
        await interaction.response.send_message(
            "고급 템플릿을 저장했습니다. 간단 편집을 다시 저장하면 고급 레이아웃은 대체됩니다.",
            ephemeral=True,
            allowed_mentions=allowed_mentions_for([]),
        )


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
        recruitment_complete_template_path: Path,
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
        self._recruitment_complete_template_path = recruitment_complete_template_path
        self._recruitment_complete_copy_path = recruitment_complete_copy_path(
            recruitment_complete_template_path
        )
        self._notification_renderer = NotificationRenderer(
            config,
            recruitment_complete_template_path,
        )
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

        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            await self.create_match(
                manager_user_id=interaction.user.id,
                command_channel_id=interaction.channel_id,
                parsed=parsed,
            )
        except Exception:
            logger.exception("match creation failed")
            await interaction.edit_original_response(
                content="내전 생성에 실패했습니다. 관리자 로그를 확인해 주세요."
            )
            return
        try:
            await interaction.delete_original_response()
        except discord.HTTPException:
            logger.debug(
                "ephemeral creation acknowledgement could not be deleted interaction_id=%s",
                interaction.id,
                exc_info=True,
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

    @app_commands.command(
        name="모집완료문구",
        description="모집 완료 공지의 문구와 작성 예시를 간단하게 편집합니다.",
    )
    @app_commands.guild_only()
    @management_command_only()
    async def edit_recruitment_complete_template(
        self,
        interaction: discord.Interaction,
    ) -> None:
        try:
            copy = load_recruitment_complete_copy(
                self._recruitment_complete_copy_path
            )
        except (OSError, YAMLError, ValueError):
            logger.exception("failed to read simple recruitment complete copy")
            await interaction.response.send_message(
                "현재 간단 편집 설정을 읽지 못했습니다. 관리자 로그를 확인해 주세요.",
                ephemeral=True,
            )
            return
        await interaction.response.send_modal(
            RecruitmentCompleteCopyModal(self, copy)
        )

    @app_commands.command(
        name="모집완료고급편집",
        description="개발자용 Jinja 템플릿을 직접 편집합니다.",
    )
    @app_commands.guild_only()
    @management_command_only()
    async def edit_recruitment_complete_advanced_template(
        self,
        interaction: discord.Interaction,
    ) -> None:
        try:
            current_template = self._recruitment_complete_template_path.read_text(
                encoding="utf-8"
            )
        except OSError:
            logger.exception("failed to read recruitment complete template")
            await interaction.response.send_message(
                "현재 고급 템플릿 파일을 읽지 못했습니다. 관리자 로그를 확인해 주세요.",
                ephemeral=True,
            )
            return
        if len(current_template) > 4000:
            await interaction.response.send_message(
                "현재 고급 템플릿이 4,000자를 초과해 모달에서 편집할 수 없습니다.",
                ephemeral=True,
            )
            return
        await interaction.response.send_modal(
            RecruitmentCompleteAdvancedTemplateModal(self, current_template)
        )

    @app_commands.command(
        name="모집완료미리보기",
        description="현재 모집 완료 일반 텍스트 공지를 본인에게만 미리 보여줍니다.",
    )
    @app_commands.guild_only()
    @management_command_only()
    async def preview_recruitment_complete_template(
        self,
        interaction: discord.Interaction,
    ) -> None:
        session = self._coordinator.active_session
        try:
            if session is None:
                rendered = self._notification_renderer.render_recruitment_complete_preview(
                    user_ids=(interaction.user.id,),
                    now=self._clock.now(),
                )
                preview_notice = (
                    "활성 내전이 없어 실행자를 참가자로 넣은 예시 일정입니다. "
                    "아래 메시지는 본인에게만 보입니다."
                )
            else:
                user_ids = await self._current_confirmed_user_ids(session)
                if not user_ids:
                    user_ids = (interaction.user.id,)
                rendered = self._notification_renderer.render_recruitment_complete(
                    session,
                    user_ids,
                )
                preview_notice = (
                    "현재 활성 내전 기준 일반 텍스트 미리보기입니다. "
                    "아래 메시지는 본인에게만 보입니다."
                )
        except (OSError, TemplateError, ValueError) as exc:
            logger.warning("failed to render recruitment complete preview", exc_info=True)
            await interaction.response.send_message(
                f"모집 완료 공지를 미리 볼 수 없습니다: {exc}",
                ephemeral=True,
                allowed_mentions=allowed_mentions_for([]),
            )
            return
        await interaction.response.send_message(
            preview_notice,
            ephemeral=True,
            allowed_mentions=allowed_mentions_for([]),
        )
        await interaction.followup.send(
            rendered.content,
            ephemeral=True,
            allowed_mentions=allowed_mentions_for([]),
        )

    async def _current_confirmed_user_ids(
        self,
        session: MatchSession,
    ) -> tuple[int, ...]:
        actor = self._coordinator.active_actor
        if actor is not None and actor.session.id == session.id:
            await actor.drain()
            return tuple(entry.discord_user_id for entry in actor.current_confirmed())
        await self._writer.flush()
        roster = await self._repository.load_roster(session.id)
        return tuple(
            entry.discord_user_id
            for entry in sorted(roster, key=lambda item: item.reaction_order)
            if entry.status.value == "CONFIRMED"
        )

    def save_recruitment_complete_template(self, source: str) -> None:
        path = self._recruitment_complete_template_path
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path = path.with_suffix(f"{path.suffix}.tmp")
        temporary_path.write_text(f"{source.rstrip()}\n", encoding="utf-8")
        temporary_path.replace(path)

    def save_recruitment_complete_copy(
        self,
        copy: RecruitmentCompleteCopy,
    ) -> None:
        copy = copy.normalized()
        source = build_recruitment_complete_template(copy)
        NotificationRenderer.validate_recruitment_complete_template(
            source,
            self._config,
        )
        template_path = self._recruitment_complete_template_path
        copy_path = self._recruitment_complete_copy_path
        template_path.parent.mkdir(parents=True, exist_ok=True)
        copy_path.parent.mkdir(parents=True, exist_ok=True)
        template_temporary_path = template_path.with_suffix(
            f"{template_path.suffix}.tmp"
        )
        copy_temporary_path = copy_path.with_suffix(f"{copy_path.suffix}.tmp")
        template_temporary_path.write_text(source, encoding="utf-8")
        copy_temporary_path.write_text(
            dump_recruitment_complete_copy(copy),
            encoding="utf-8",
        )
        template_temporary_path.replace(template_path)
        copy_temporary_path.replace(copy_path)

    async def create_match(
        self,
        *,
        manager_user_id: int,
        command_channel_id: int,
        parsed: ParsedMatchCommand,
    ) -> MatchSession:
        previous_session = self._coordinator.active_session
        session = await self._coordinator.create_replacing_active(
            CreateMatchRequest(
                manager_user_id=manager_user_id,
                command_channel_id=command_channel_id,
                parsed=parsed,
            )
        )
        try:
            if previous_session is not None:
                await self._delete_announcement_message(previous_session)
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
                f"**{format_discord_timestamp(session.starts_at, 't')} 내전**! "
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
            await self._coordinator.cancel_current(expected_match_id=session.id)
            raise

    @app_commands.command(name="티어현황", description="확정 참가자의 티어 작성 현황을 확인합니다.")
    @app_commands.guild_only()
    @management_command_only()
    async def tier_status(self, interaction: discord.Interaction) -> None:
        session = self._coordinator.active_session
        if session is None:
            await interaction.response.send_message("활성 내전이 없습니다.", ephemeral=True)
            return
        if session.recruitment_completed_notified_at is None:
            await interaction.response.send_message(
                "모집 완료 공지가 전송된 뒤 티어 수집이 시작됩니다.", ephemeral=True
            )
            return
        await self._writer.flush()
        statuses = await self._repository.get_tier_status(session.id)
        completed = [item.discord_display_name for item in statuses if item.has_tier]
        missing = [item.discord_display_name for item in statuses if not item.has_tier]
        content = (
            f"티어 작성 완료 {len(completed)}명: {', '.join(completed) or '-'}\n"
            f"티어 미작성 {len(missing)}명: {', '.join(missing) or '-'}"
        )
        await interaction.response.send_message(
            content,
            ephemeral=True,
            allowed_mentions=allowed_mentions_for([]),
        )

    @app_commands.command(name="티어미작성알림", description="티어 미작성 확정 참가자만 멘션합니다.")
    @app_commands.guild_only()
    @management_command_only()
    async def remind_missing_tier(self, interaction: discord.Interaction) -> None:
        session = self._coordinator.active_session
        now = self._clock.now()
        if session is None:
            await interaction.response.send_message("활성 내전이 없습니다.", ephemeral=True)
            return
        if session.recruitment_completed_notified_at is None:
            await interaction.response.send_message(
                "모집 완료 공지가 전송된 뒤 사용할 수 있습니다.", ephemeral=True
            )
            return
        if now >= session.tier_deadline_at:
            await interaction.response.send_message(
                "티어 작성 마감 시각이 지났습니다.", ephemeral=True
            )
            return
        await self._writer.flush()
        claimed, missing = await self._repository.claim_missing_tier_reminder(
            session.id, now, cooldown_seconds=300
        )
        if not claimed:
            await interaction.response.send_message(
                "동일 알림은 5분에 한 번만 보낼 수 있습니다.", ephemeral=True
            )
            return
        if not missing:
            await interaction.response.send_message(
                "현재 확정 참가자 전원이 티어를 작성했습니다.", ephemeral=True
            )
            return
        user_ids = [item.discord_user_id for item in missing]
        mentions = " ".join(f"<@{user_id}>" for user_id in user_ids)
        await interaction.response.send_message(
            f"📝 아직 티어를 작성하지 않은 참가자입니다.\n{mentions}",
            allowed_mentions=allowed_mentions_for(user_ids),
        )
        logger.info(
            "missing tier reminder sent match_id=%s recipient_count=%s",
            session.id,
            len(user_ids),
        )

    @app_commands.command(name="내전상태", description="최근 내전의 상태와 참가 인원을 확인합니다.")
    @app_commands.guild_only()
    @management_command_only()
    async def match_status(self, interaction: discord.Interaction) -> None:
        session = self._coordinator.active_session
        actor = self._coordinator.active_actor
        if session is None:
            session = await self._repository.get_latest_match(self._config.guild_id)
        if session is None:
            await interaction.response.send_message("생성된 내전이 없습니다.", ephemeral=True)
            return
        await self._writer.flush()
        if actor is not None and actor.session.id == session.id:
            confirmed = len(actor.current_confirmed())
            waitlisted = len(actor.current_waitlist())
        else:
            roster = await self._repository.load_roster(session.id)
            confirmed = sum(1 for entry in roster if entry.status.value == "CONFIRMED")
            waitlisted = sum(1 for entry in roster if entry.status.value == "WAITLISTED")
        await interaction.response.send_message(
            f"상태: {session.status.value}\n"
            f"시작: {format_korean_datetime(session.starts_at)}\n"
            f"모드: {session.mode or '일반 내전'}\n"
            f"확정 참가자: {confirmed}명\n대기자: {waitlisted}명",
            ephemeral=True,
            allowed_mentions=allowed_mentions_for([]),
        )

    @app_commands.command(name="내전취소", description="활성 내전을 취소하고 모집 공지를 삭제합니다.")
    @app_commands.guild_only()
    @management_command_only()
    async def cancel_match(self, interaction: discord.Interaction) -> None:
        canceled = await self._coordinator.cancel_current()
        if canceled is None:
            await interaction.response.send_message("활성 내전이 없습니다.", ephemeral=True)
            return
        announcement_deleted = await self._delete_announcement_message(canceled)
        content = "활성 내전을 취소하고 모집 공지와 예약 알림을 정리했습니다."
        if not announcement_deleted:
            content = "내전은 취소했지만 모집 공지를 삭제하지 못했습니다. 봇 권한을 확인해 주세요."
        await interaction.response.send_message(content, ephemeral=True)

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
                actor = self._coordinator.active_actor
                if (
                    actor is None
                    or actor.session.announcement_message_id != message_id
                ):
                    return
                await actor.drain()
                actor = self._coordinator.active_actor
                if (
                    actor is None
                    or actor.session.announcement_message_id != message_id
                ):
                    return
                should_show_seed = actor.active_user_count() == 0
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
        actor = self._coordinator.active_actor
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
        recruitment_complete_template_path: Path,
        migration_path: Path,
    ) -> None:
        intents = discord.Intents.none()
        intents.guilds = True
        intents.guild_messages = True
        intents.guild_reactions = True
        intents.message_content = True
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
                self.recruitment_complete_template_path,
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
