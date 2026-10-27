"""검증된 운영 설정 값. 파일·환경 변수 읽기는 config에서 수행한다."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class ChannelConfig:
    command: int
    announcement: int
    tier: int
    admin: int
    warning: int | None = None


@dataclass(frozen=True, slots=True)
class PolicyConfig:
    privacy_url: str | None = None
    terms_url: str | None = None
    support_contact: str | None = None


@dataclass(frozen=True, slots=True)
class DefaultConfig:
    participant_limit: int = 10
    tier_deadline_offset_minutes: int = 30
    lobby_offset_minutes: int = 10
    past_time_policy: str = "reject"
    recruitment_role_id: int | None = None
    lobby_voice_channel_id: int | None = None
    lobby_voice_channel_2_id: int | None = None
    mode_display_fallback: str | None = None
    lobby_name: str = "대기실 1번"
    lobby_2_name: str = "대기실 2번"


@dataclass(frozen=True, slots=True)
class MessageConfig:
    participation_notice: str
    tier_notice: str
    manner_notice: str


@dataclass(frozen=True, slots=True)
class ManagerConfig:
    default_mode: str | None = None
    extra_notice: str = ""

    def render_rules(self) -> str:
        return self.extra_notice


@dataclass(frozen=True, slots=True)
class AppConfig:
    guild_id: int
    channels: ChannelConfig
    admin_user_ids: frozenset[int]
    admin_role_ids: frozenset[int]
    defaults: DefaultConfig
    messages: MessageConfig
    managers: dict[int, ManagerConfig] = field(default_factory=dict)
    policy: PolicyConfig = field(default_factory=PolicyConfig)

    def manager(self, user_id: int) -> ManagerConfig:
        return self.managers.get(user_id, ManagerConfig())

    def is_manager(self, user_id: int, role_ids: set[int] | frozenset[int]) -> bool:
        return user_id in self.admin_user_ids or bool(
            self.admin_role_ids.intersection(role_ids)
        )
