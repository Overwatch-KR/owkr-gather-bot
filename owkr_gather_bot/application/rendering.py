from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo

from jinja2 import Environment, StrictUndefined

from owkr_gather_bot.config import AppConfig, ManagerConfig
from owkr_gather_bot.domain.models import MatchSession, NotificationKind, NotificationRecord


KST = ZoneInfo("Asia/Seoul")


def format_korean_datetime(value) -> str:
    local = value.astimezone(KST)
    period = "오전" if local.hour < 12 else "오후"
    hour = local.hour % 12 or 12
    minute = f" {local.minute}분" if local.minute else ""
    return f"{local.month}월 {local.day}일 {period} {hour}시{minute}"


@dataclass(frozen=True, slots=True)
class RenderedNotification:
    content: str
    allowed_user_ids: tuple[int, ...] = ()


class RecruitmentTemplateRenderer:
    def __init__(self, config: AppConfig, template_path: Path) -> None:
        self._config = config
        environment = Environment(
            autoescape=False,
            undefined=StrictUndefined,
            keep_trailing_newline=False,
            trim_blocks=True,
            lstrip_blocks=True,
        )
        self._template = environment.from_string(template_path.read_text(encoding="utf-8"))

    def render(self, session: MatchSession, manager: ManagerConfig) -> str:
        mode_display = session.mode or self._config.defaults.mode_display_fallback
        content = self._template.render(
            starts_at_display=format_korean_datetime(session.starts_at),
            mode_display=mode_display,
            participation_notice=self._config.messages.participation_notice,
            tier_deadline_display=format_korean_datetime(session.tier_deadline_at),
            lobby_display=format_korean_datetime(session.lobby_at),
            manager_rules=manager.render_rules(),
            tier_notice=self._config.messages.tier_notice,
            manner_notice=self._config.messages.manner_notice,
        ).strip()
        if len(content) > 2000:
            raise ValueError("rendered recruitment message exceeds Discord's 2000 character limit")
        return content


class NotificationRenderer:
    def __init__(self, config: AppConfig) -> None:
        self._config = config

    def render(
        self, notification: NotificationRecord, session: MatchSession
    ) -> RenderedNotification:
        if notification.kind is NotificationKind.RECRUITMENT_COMPLETE:
            user_ids = tuple(int(user_id) for user_id in notification.payload.get("user_ids", []))
            mentions = self._mention_lines(user_ids)
            content = (
                f"✅ {format_korean_datetime(session.starts_at)} 내전 모집이 완료되었습니다.\n\n"
                f"{mentions}\n\n"
                f"<#{session.tier_channel_id}> 내전 티어 채널에 가능한 빠르게 아래 형식으로 작성해 주세요.\n\n"
                "배틀태그\n탱커 / 딜러 / 힐러\n\n"
                "예시:\nlemon#32146\n마4 / 마4! / 마4\n\n"
                f"티어 작성 마감: {format_korean_datetime(session.tier_deadline_at)}\n"
                f"대기실 입장: {format_korean_datetime(session.lobby_at)}\n\n"
                f"{self._config.messages.manner_notice}"
            )
            return RenderedNotification(content=content, allowed_user_ids=user_ids)

        if notification.kind is NotificationKind.LOBBY_REMINDER:
            user_ids = tuple(int(user_id) for user_id in notification.payload.get("user_ids", []))
            mentions = self._mention_lines(user_ids)
            content = (
                f"🔊 {format_korean_datetime(session.starts_at)} 내전 시작 10분 전입니다.\n\n"
                f"{mentions}\n\n"
                f"{self._config.defaults.lobby_name}으로 입장해 주세요."
            )
            return RenderedNotification(content=content, allowed_user_ids=user_ids)

        return RenderedNotification(
            content=f"✅ {format_korean_datetime(session.starts_at)} 내전 참가자 전원이 티어 작성을 완료했습니다."
        )

    @staticmethod
    def _mention_lines(user_ids: tuple[int, ...]) -> str:
        mentions = [f"<@{user_id}>" for user_id in user_ids]
        return "\n".join(" ".join(mentions[index : index + 5]) for index in range(0, len(mentions), 5))
