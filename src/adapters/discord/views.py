"""관리자 확인·수정 화면. Discord 응답과 요청자 검증을 담당한다."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import discord

from src.application.coordinator import DuplicateSourceRequest, LobbyCapacityExceeded
from src.application.rendering import format_match_time, format_korean_time, format_lobby_target
from src.domain.models import MatchSession
from src.parsing.match_command import (
    CommandParseError,
    ParsedMatchCommand,
    PastTimeError,
)

from .common import allowed_mentions_for, discord_message_url

if TYPE_CHECKING:
    from .commands import GatherCog
logger = logging.getLogger(__name__)


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
        arguments = f"{self.time_input.value} {self.mode_input.value or ''}".strip()
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
        try:
            lobby_id, lobby_name = self._cog._coordinator.lobby_assignment_for(self._parsed.starts_at)
            lobby_name = format_lobby_target(lobby_id, lobby_name)
        except LobbyCapacityExceeded:
            lobby_name = "현재 빈 대기실 없음 · 시간을 변경해 주세요"
        return (
            "**내전 생성 확인**\n\n"
            f"시작 · {format_match_time(self._parsed.starts_at)}\n"
            f"모드 · {mode}\n"
            f"티어 마감 · "
            f"{format_match_time(self._parsed.tier_deadline_at)}\n"
            f"대기실 입장 · {format_match_time(self._parsed.lobby_at)} "
            f"· {lobby_name} (예상)\n\n"
            "대기실은 실제 생성 순서에 따라 확정됩니다.\n"
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
        except LobbyCapacityExceeded:
            await interaction.edit_original_response(
                content=(
                    "해당 시간대의 대기실이 모두 예약되어 있습니다. "
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
                f"✅ {format_match_time(session.starts_at)} 내전을 만들었습니다. "
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
        announcement_updated = await self._cog._mark_announcement_canceled(canceled)
        anchor_deleted = await self._cog._delete_tier_anchor_message(canceled)
        substitutes_deleted = (
            await self._cog._delete_substitute_recruitment_messages(
                canceled,
                substitute_message_ids,
            )
            if substitute_message_ids
            else True
        )
        result = f"{format_match_time(canceled.starts_at)} 내전이 취소되었습니다."
        if not announcement_updated:
            result += "\n모집 공지에 취소 안내를 표시하지 못했습니다. 공지 채널에 직접 안내해 주세요."
        if not anchor_deleted or not substitutes_deleted:
            result += (
                "\n일부 봇 메시지를 지우지 못했습니다. "
                "봇의 채널 보기·메시지 기록 보기 권한을 확인하고 남은 메시지를 직접 정리해 주세요."
            )
        await interaction.edit_original_response(
            content=result,
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
            logger.debug(
                "failed to delete match cancellation confirmation", exc_info=True
            )
