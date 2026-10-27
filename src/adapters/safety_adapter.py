from __future__ import annotations

import asyncio
import logging
from typing import Literal

import discord
from discord import app_commands
from discord.ext import commands

from src.adapters.discord.common import _interaction_role_ids, allowed_mentions_for
from src.application.safety import SafetyService
from src.config import AppConfig
from src.domain.warnings import WarningReason, WarningSummary

logger = logging.getLogger(__name__)


def warning_authorization_error(config: AppConfig, interaction: discord.Interaction) -> str | None:
    if interaction.guild_id != config.guild_id:
        return "이 서버에서는 경고를 관리할 수 없습니다."
    if config.channels.warning is None:
        return "경고 채널이 설정되지 않았습니다. 운영자에게 경고 채널 설정을 요청해 주세요."
    if interaction.channel_id != config.channels.warning:
        return f"경고 관리는 <#{config.channels.warning}> 채널에서만 사용할 수 있습니다."
    if not config.is_manager(interaction.user.id, _interaction_role_ids(interaction)):
        return "내전 관리자 또는 스태프 역할이 필요합니다."
    return None


def summary_text(summary: WarningSummary) -> str:
    return f"노쇼 {summary.no_show}회 · 비매너 {summary.bad_manners}회 · 합계 {summary.total}회"


class WarningConfirmation(discord.ui.View):
    def __init__(self, cog: SafetyCog, interaction: discord.Interaction,
                 target: discord.Member, action: str, reason: WarningReason | None,
                 warning_id: int | None) -> None:
        super().__init__(timeout=120)
        self.cog = cog
        self.manager_id = interaction.user.id
        self.request_id = interaction.id
        self.target_id = target.id
        self.joined_at = target.joined_at
        self.action = action
        self.reason = reason
        self.warning_id = warning_id
        self.lock = asyncio.Lock()
        self.finished = False

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        error = warning_authorization_error(self.cog.config, interaction)
        if interaction.user.id != self.manager_id:
            error = "이 확인은 명령을 실행한 관리자만 처리할 수 있습니다."
        if error:
            await interaction.response.send_message(error, ephemeral=True, allowed_mentions=allowed_mentions_for([]))
            return False
        return True

    @discord.ui.button(label="상의 완료 · 실행", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.defer(ephemeral=True)
        async with self.lock:
            if self.finished:
                await interaction.followup.send("이미 처리한 요청입니다.", ephemeral=True, allowed_mentions=allowed_mentions_for([]))
                return
            try:
                member = await self.cog.current_member(self.target_id)
                if member is None or member.joined_at != self.joined_at:
                    self.finished = True
                    self.stop()
                    await interaction.edit_original_response(
                        content="대상이 탈퇴했거나 재가입했습니다. 대상을 다시 확인해 주세요.", view=None,
                        allowed_mentions=allowed_mentions_for([]),
                    )
                    return
                if self.action == "기록":
                    assert self.reason is not None
                    summary = await self.cog.service.record(
                        self.target_id, self.reason, self.manager_id, self.request_id
                    )
                    result = f"경고를 기록했습니다. {summary_text(summary)}"
                elif self.action == "취소":
                    assert self.warning_id is not None
                    changed = await self.cog.service.revoke(self.target_id, self.warning_id)
                    result = "선택한 최근 경고를 취소했습니다." if changed else "이미 취소되거나 삭제된 경고입니다."
                    result += f" {summary_text(await self.cog.service.summary(self.target_id))}"
                else:
                    await self.cog.service.forget_departed(self.target_id)
                    result = "대상의 경고 기록 전체를 삭제했습니다."
            except discord.HTTPException:
                await interaction.followup.send(
                    "서버 재적 여부를 확인하지 못했습니다. 잠시 후 다시 시도해 주세요.", ephemeral=True,
                    allowed_mentions=allowed_mentions_for([]),
                )
                return
            except Exception:
                logger.exception("warning operation failed")
                await interaction.followup.send(
                    "경고 처리를 완료하지 못했습니다. 잠시 후 다시 시도해 주세요.", ephemeral=True,
                    allowed_mentions=allowed_mentions_for([]),
                )
                return
            self.finished = True
            self.stop()
            await interaction.edit_original_response(content=result, view=None, allowed_mentions=allowed_mentions_for([]))

    @discord.ui.button(label="닫기", style=discord.ButtonStyle.secondary)
    async def dismiss(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        async with self.lock:
            self.finished = True
            self.stop()
            await interaction.response.edit_message(content="요청을 닫았습니다.", view=None)


class TierErasureConfirmation(discord.ui.View):
    def __init__(self, cog: SafetyCog, user_id: int) -> None:
        super().__init__(timeout=120)
        self.cog = cog
        self.user_id = user_id
        self.lock = asyncio.Lock()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.guild_id == self.cog.config.guild_id and interaction.user.id == self.user_id:
            return True
        await interaction.response.send_message("본인의 요청만 처리할 수 있습니다.", ephemeral=True)
        return False

    @discord.ui.button(label="내 티어 원문 삭제 확인", style=discord.ButtonStyle.danger)
    async def erase(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.defer(ephemeral=True)
        async with self.lock:
            try:
                await self.cog.service.erase_tiers(self.user_id)
            except Exception:
                logger.exception("tier erasure failed")
                await interaction.followup.send("티어를 삭제하지 못했습니다. 잠시 후 다시 시도해 주세요.", ephemeral=True)
                return
            self.stop()
            await interaction.edit_original_response(
                content=("봇 DB의 내 티어 원문과 연결 기록을 삭제했습니다. 현재 참가 중인 내전에서는 다시 수집하지 않습니다.\n"
                         "참가·대기 상태는 유지되며 시작·취소 후 정리됩니다. 경고·Discord 원문·기존 백업과 로그는 이 버튼으로 삭제되지 않습니다.\n"
                         "추가 삭제·수정 요청은 안내된 문의 경로로 접수해 주세요."),
                view=None, allowed_mentions=allowed_mentions_for([]),
            )


class SafetyCog(commands.Cog):
    def __init__(self, bot, config: AppConfig, service: SafetyService) -> None:
        self.bot = bot
        self.config = config
        self.service = service
        self._maintenance_task: asyncio.Task | None = None
        self._membership_task: asyncio.Task | None = None

    async def cog_load(self) -> None:
        self._maintenance_task = asyncio.create_task(self._maintenance(), name="privacy-maintenance")
        self._membership_task = asyncio.create_task(self._membership_maintenance(), name="warning-membership")

    async def cog_unload(self) -> None:
        tasks = [task for task in (self._maintenance_task, self._membership_task) if task is not None]
        for task in tasks:
            task.cancel()
        for task in tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass

    async def current_member(self, user_id: int) -> discord.Member | None:
        guild = self.bot.get_guild(self.config.guild_id)
        if guild is None:
            raise RuntimeError("configured guild is unavailable")
        try:
            member = await guild.fetch_member(user_id)
        except discord.NotFound as exc:
            # Only Unknown Member establishes departure. Unknown Guild and
            # permission/network failures must never erase warning history.
            if exc.code != 10007:
                raise
            await self.service.forget_departed(user_id)
            return None
        if member.joined_at is not None:
            await self.service.reconcile_membership(user_id, member.joined_at)
        return member

    async def maintenance_once(self, *, check_members: bool) -> None:
        await self.service.purge_closed()
        if check_members:
            for user_id in await self.service.warning_users():
                try:
                    await self.current_member(user_id)
                except Exception:
                    logger.warning("warning membership check unavailable; records retained")

    async def _maintenance(self) -> None:
        await self.bot.wait_until_ready()
        while True:
            try:
                await self.maintenance_once(check_members=False)
            except Exception:
                logger.exception("privacy maintenance failed; will retry")
            await asyncio.sleep(10)

    async def _membership_maintenance(self) -> None:
        await self.bot.wait_until_ready()
        while True:
            try:
                await self.maintenance_once(check_members=True)
            except Exception:
                logger.exception("warning membership maintenance failed; will retry")
            await asyncio.sleep(3600)

    @app_commands.command(name="경고", description="상의 후 경고를 기록하거나 조회·취소합니다.")
    @app_commands.guild_only()
    @app_commands.describe(대상="경고를 관리할 서버 이용자", 작업="조회, 기록, 최근 경고 취소 또는 전체 기록 삭제",
                           사유="기록·취소할 경고의 종류")
    async def warning_command(self, interaction: discord.Interaction, 대상: discord.Member,
                              작업: Literal["조회", "기록", "취소", "전체삭제"] = "조회",
                              사유: Literal["노쇼", "비매너"] | None = None) -> None:
        error = warning_authorization_error(self.config, interaction)
        if error:
            await interaction.response.send_message(error, ephemeral=True, allowed_mentions=allowed_mentions_for([]))
            return
        if 대상.bot:
            await interaction.response.send_message("봇 계정에는 경고를 기록하지 않습니다.", ephemeral=True)
            return
        if 작업 in {"기록", "취소"} and 사유 is None:
            await interaction.response.send_message("노쇼 또는 비매너 사유를 선택해 다시 실행해 주세요.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        try:
            member = await self.current_member(대상.id)
        except Exception:
            await interaction.followup.send("서버 재적 여부를 확인하지 못했습니다. 잠시 후 다시 시도해 주세요.", ephemeral=True)
            return
        if member is None:
            await interaction.followup.send("서버를 떠난 이용자의 경고 기록을 정리했습니다.", ephemeral=True)
            return
        if 작업 == "조회":
            await interaction.followup.send(
                f"<@{대상.id}> · {summary_text(await self.service.summary(대상.id))}",
                ephemeral=True, allowed_mentions=allowed_mentions_for([]),
            )
            return
        reason = WarningReason(사유) if 사유 else None
        warning_id = None
        if 작업 == "취소":
            warning_id = await self.service.latest_warning(대상.id, reason)
            if warning_id is None:
                await interaction.followup.send("해당 사유의 취소할 경고가 없습니다.", ephemeral=True)
                return
        action = f"{사유} 경고 1회 {작업}" if 작업 != "전체삭제" else "경고 기록 전체 삭제"
        await interaction.followup.send(
            f"<@{대상.id}> · {action}\n다른 내전 관리자와 상의한 뒤 실행해 주세요. 자동 제재나 참가 제한은 발생하지 않습니다.",
            ephemeral=True, allowed_mentions=allowed_mentions_for([]),
            view=WarningConfirmation(self, interaction, member, 작업, reason, warning_id),
        )

    @app_commands.command(name="개인정보", description="정보 보관·문의 안내를 보고 내 티어 원문을 삭제합니다.")
    @app_commands.guild_only()
    async def privacy_command(self, interaction: discord.Interaction) -> None:
        if interaction.guild_id != self.config.guild_id:
            await interaction.response.send_message("봇이 운영되는 서버에서 실행해 주세요.", ephemeral=True)
            return
        policy = self.config.policy
        lines = [
            "내전 참가·반응·티어 정보는 운영 중에 보관하고 시작·취소 후 자동 정리합니다.",
            "경고는 관리자 판단으로 기록하며 기간 만료 없이 보관합니다. 탈퇴 확인 시 삭제합니다(정기 확인은 약 1시간 간격).",
            "아래 버튼은 내 티어 원문과 연결 기록만 삭제하고, 현재 참가 중인 내전의 티어 재수집을 중단합니다.",
            "참가 상태·경고·Discord 원문·기존 로그와 백업은 이 버튼의 삭제 대상이 아닙니다.",
        ]
        if policy.privacy_url:
            lines.append(f"개인정보처리방침: {policy.privacy_url}")
        if policy.terms_url:
            lines.append(f"이용약관: {policy.terms_url}")
        lines.append(f"추가 수정·삭제 요청 및 문의: {policy.support_contact}" if policy.support_contact
                     else "추가 수정·삭제 요청: 서버 내전 관리자에게 비공개로 문의해 주세요. 전용 연락처는 준비 중입니다.")
        await interaction.response.send_message(
            "\n".join(lines), ephemeral=True, allowed_mentions=allowed_mentions_for([]),
            view=TierErasureConfirmation(self, interaction.user.id),
        )
