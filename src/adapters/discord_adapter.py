"""기존 import 경로 호환용. 실제 구현은 adapters/discord의 책임별 모듈에 있다."""
from .discord.bot import GatherBot
from .discord.commands import GatherCog
from .discord.common import (
    ManagementCommandCheckFailure,
    _interaction_role_ids,
    _management_authorization_error,
    _normalized_command_payload,
    allowed_mentions_for,
    build_recruitment_embed,
    build_substitute_recruitment_embed,
    discord_message_url,
    management_command_only,
    match_status_label,
)
from .discord.transport import DiscordNotificationTransport
from .discord.views import (
    CancelMatchConfirmationView,
    CreateMatchConfirmationView,
    EditMatchModal,
)

__all__ = [
    "GatherBot", "GatherCog", "DiscordNotificationTransport", "EditMatchModal",
    "CreateMatchConfirmationView", "CancelMatchConfirmationView", "match_status_label",
    "discord_message_url", "ManagementCommandCheckFailure", "_interaction_role_ids",
    "_management_authorization_error", "management_command_only", "_normalized_command_payload",
    "allowed_mentions_for", "build_recruitment_embed", "build_substitute_recruitment_embed",
]
