from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from jinja2 import Environment, StrictUndefined
from jinja2.sandbox import SandboxedEnvironment

from src.config import AppConfig, ManagerConfig
from src.domain.models import MatchSession, NotificationKind, NotificationRecord


KST = ZoneInfo("Asia/Seoul")


def _template_environment() -> Environment:
    return SandboxedEnvironment(
        autoescape=False,
        undefined=StrictUndefined,
        keep_trailing_newline=False,
        trim_blocks=True,
        lstrip_blocks=True,
    )


def format_korean_datetime(value) -> str:
    local = value.astimezone(KST)
    return f"{local.month}월 {local.day}일 {format_korean_time(local)}"


def format_korean_time(value) -> str:
    local = value.astimezone(KST)
    period = "오전" if local.hour < 12 else "오후"
    hour = local.hour % 12 or 12
    minute = f" {local.minute}분" if local.minute else ""
    return f"{period} {hour}시{minute}"


def format_discord_timestamp(value, style: str) -> str:
    if style not in {"t", "T", "d", "D", "f", "F", "R"}:
        raise ValueError(f"unsupported Discord timestamp style: {style}")
    return f"<t:{int(value.timestamp())}:{style}>"


@dataclass(frozen=True, slots=True)
class RenderedNotification:
    content: str
    allowed_user_ids: tuple[int, ...] = ()
    voice_channel_id: int | None = None
    content_before_mentions: str | None = None
    content_after_mentions: str | None = None

    def with_allowed_user_ids(
        self,
        user_ids: tuple[int, ...],
    ) -> RenderedNotification:
        if self.content_before_mentions is None or self.content_after_mentions is None:
            raise ValueError("notification content cannot be rebuilt with filtered mentions")
        return RenderedNotification(
            content=(
                self.content_before_mentions
                + _mention_lines(user_ids)
                + self.content_after_mentions
            ),
            allowed_user_ids=user_ids,
            voice_channel_id=self.voice_channel_id,
            content_before_mentions=self.content_before_mentions,
            content_after_mentions=self.content_after_mentions,
        )


def _mention_lines(user_ids: tuple[int, ...]) -> str:
    mentions = [f"<@{user_id}>" for user_id in user_ids]
    return "\n".join(
        " ".join(mentions[index : index + 5])
        for index in range(0, len(mentions), 5)
    )


class RecruitmentTemplateRenderer:
    def __init__(self, config: AppConfig, template_path: Path) -> None:
        self._config = config
        environment = _template_environment()
        self._template = environment.from_string(template_path.read_text(encoding="utf-8"))

    def render(self, session: MatchSession, manager: ManagerConfig) -> str:
        mode_display = session.mode or self._config.defaults.mode_display_fallback
        content = self._template.render(
            starts_at_display=format_discord_timestamp(session.starts_at, "F"),
            starts_at_relative=format_discord_timestamp(session.starts_at, "R"),
            mode_display=mode_display,
            participant_limit=session.participant_limit,
            participation_notice=self._config.messages.participation_notice,
            tier_deadline_display=format_discord_timestamp(session.tier_deadline_at, "t"),
            lobby_display=format_discord_timestamp(session.lobby_at, "t"),
            tier_channel_mention=f"<#{session.tier_channel_id}>",
            manager_rules=manager.render_rules(),
            tier_notice=self._config.messages.tier_notice,
            manner_notice=self._config.messages.manner_notice,
        ).strip()
        if len(content) > 4096:
            raise ValueError(
                "rendered recruitment description exceeds Discord's 4096 character limit"
            )
        return content


class NotificationRenderer:
    def __init__(
        self,
        config: AppConfig,
        recruitment_complete_template_path: Path,
    ) -> None:
        self._config = config
        self._recruitment_complete_template_path = recruitment_complete_template_path

    @staticmethod
    def validate_recruitment_complete_template(
        source: str,
        config: AppConfig,
    ) -> None:
        template = _template_environment().from_string(source)
        content = template.render(
            user="",
            users="",
            starts_at="<t:1784199600:F>",
            starts_at_display="<t:1784199600:F>",
            start_time="<t:1784199600:t>",
            starts_in="<t:1784199600:R>",
            mode="6ㄷ6클래식",
            tier_channel="<#123456789012345678>",
            tier_channel_mention="<#123456789012345678>",
            tier_deadline="<t:1784197800:t>",
            tier_deadline_display="<t:1784197800:t>",
            lobby_time="<t:1784199000:t>",
            lobby_display="<t:1784199000:t>",
            lobby_name=config.defaults.lobby_name,
            participant_count=max(10, config.defaults.participant_limit),
            manner_notice=config.messages.manner_notice,
        ).strip()
        preview_prefix = "**내전 코드** · `TEST`\n\n"
        if len(preview_prefix) + len(content) > 2000:
            raise ValueError(
                "rendered recruitment complete message exceeds Discord's "
                "2000 character limit"
            )

    def _load_recruitment_complete_template(self):
        source = self._recruitment_complete_template_path.read_text(encoding="utf-8")
        return _template_environment().from_string(source)

    def render_recruitment_complete(
        self,
        session: MatchSession,
        user_ids: tuple[int, ...],
    ) -> RenderedNotification:
        return self._render_recruitment_complete(
            user_ids=user_ids,
            match_code=session.match_code,
            starts_at=session.starts_at,
            mode=session.mode or self._config.defaults.mode_display_fallback or "",
            tier_channel_id=session.tier_channel_id,
            tier_deadline_at=session.tier_deadline_at,
            lobby_at=session.lobby_at,
        )

    def render_recruitment_complete_preview(
        self,
        *,
        user_ids: tuple[int, ...],
        now: datetime,
    ) -> RenderedNotification:
        starts_at = now + timedelta(hours=1)
        return self._render_recruitment_complete(
            user_ids=user_ids,
            match_code="TEST",
            starts_at=starts_at,
            mode=self._config.defaults.mode_display_fallback or "",
            tier_channel_id=self._config.channels.tier,
            tier_deadline_at=starts_at
            - timedelta(minutes=self._config.defaults.tier_deadline_offset_minutes),
            lobby_at=starts_at
            - timedelta(minutes=self._config.defaults.lobby_offset_minutes),
        )

    def _render_recruitment_complete(
        self,
        *,
        user_ids: tuple[int, ...],
        match_code: str,
        starts_at: datetime,
        mode: str,
        tier_channel_id: int,
        tier_deadline_at: datetime,
        lobby_at: datetime,
    ) -> RenderedNotification:
        starts_at_display = format_discord_timestamp(starts_at, "F")
        start_time = format_discord_timestamp(starts_at, "t")
        starts_in = format_discord_timestamp(starts_at, "R")
        tier_channel = f"<#{tier_channel_id}>"
        tier_deadline = format_discord_timestamp(tier_deadline_at, "t")
        lobby_time = format_discord_timestamp(lobby_at, "t")
        content = self._load_recruitment_complete_template().render(
            user="",
            users="",
            starts_at=starts_at_display,
            starts_at_display=starts_at_display,
            start_time=start_time,
            starts_in=starts_in,
            mode=mode,
            tier_channel=tier_channel,
            tier_channel_mention=tier_channel,
            tier_deadline=tier_deadline,
            tier_deadline_display=tier_deadline,
            lobby_time=lobby_time,
            lobby_display=lobby_time,
            lobby_name=self._config.defaults.lobby_name,
            participant_count=len(user_ids),
            manner_notice=self._config.messages.manner_notice,
        ).strip()
        content = f"**내전 코드** · `{match_code}`\n\n{content}"
        if len(content) > 2000:
            raise ValueError(
                "rendered recruitment complete message exceeds Discord's "
                "2000 character limit"
            )
        return RenderedNotification(content=content)

    def render(
        self, notification: NotificationRecord, session: MatchSession
    ) -> RenderedNotification:
        if notification.kind is NotificationKind.RECRUITMENT_COMPLETE:
            user_ids = tuple(int(user_id) for user_id in notification.payload.get("user_ids", []))
            return self.render_recruitment_complete(session, user_ids)

        if notification.kind is NotificationKind.TIER_ANCHOR:
            mode = session.mode or self._config.defaults.mode_display_fallback or "일반 내전"
            content = (
                f"## 📋 {format_discord_timestamp(session.starts_at, 't')} "
                f"내전 티어 작성 · `{session.match_code}`\n\n"
                f"**관리자** · <@{session.manager_user_id}>\n"
                f"**모드** · {mode}\n\n"
                "이 내전에 참가한 분은 **이 메시지에 답장**으로 티어를 작성해 주세요.\n"
                f"**티어 작성 마감** · "
                f"{format_discord_timestamp(session.tier_deadline_at, 't')}"
            )
            return RenderedNotification(content=content)

        if notification.kind is NotificationKind.TIER_ANCHOR_RECREATED:
            anchor_id = notification.payload.get("tier_anchor_message_id")
            anchor = f"https://discord.com/channels/{session.guild_id}/{session.tier_channel_id}/{anchor_id}"
            content = (
                f"📋 `{session.match_code}` 내전의 티어 작성 안내를 "
                f"[다시 등록했습니다.]({anchor})"
            )
            return RenderedNotification(content=content)

        if notification.kind is NotificationKind.LOBBY_REMINDER:
            user_ids = tuple(int(user_id) for user_id in notification.payload.get("user_ids", []))
            content_before_mentions = (
                f"## 🔊 대기실 입장 안내 · `{session.match_code}`\n\n"
                f"**내전 시작** · {format_discord_timestamp(session.starts_at, 'F')} "
                f"({format_discord_timestamp(session.starts_at, 'R')})\n\n"
            )
            content_after_mentions = (
                f"\n\n📢 **{self._config.defaults.lobby_name}**으로 입장해 주세요."
            )
            content = (
                content_before_mentions
                + _mention_lines(user_ids)
                + content_after_mentions
            )
            return RenderedNotification(
                content=content,
                allowed_user_ids=user_ids,
                voice_channel_id=self._config.defaults.lobby_voice_channel_id,
                content_before_mentions=content_before_mentions,
                content_after_mentions=content_after_mentions,
            )

        if notification.kind is NotificationKind.TIER_MISSING_REMINDER:
            user_ids = tuple(int(user_id) for user_id in notification.payload.get("user_ids", []))
            mentions = _mention_lines(user_ids)
            content = (
                f"**티어 작성 마감 확인 · `{session.match_code}`**\n"
                f"{mentions}\n\n"
                f"<#{session.tier_channel_id}>에서 티어 작성이 확인되지 않았습니다. "
                "관리자 안내에 따라 작성 상태를 확인해 주세요."
            )
            return RenderedNotification(content=content, allowed_user_ids=user_ids)

        return RenderedNotification(
            content=(
                f"✅ {format_discord_timestamp(session.starts_at, 'F')} 내전 참가자 "
                f"전원이 티어 작성을 완료했습니다. · `{session.match_code}`"
            )
        )
