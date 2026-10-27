from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import yaml
from dotenv import load_dotenv

from src.domain.settings import (
    AppConfig,
    ChannelConfig,
    DefaultConfig,
    ManagerConfig,
    MessageConfig,
    PolicyConfig,
)

ALLOWED_LOG_LEVELS = frozenset({"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"})


class ConfigurationError(ValueError):
    pass


def _snowflake(value: Any, field_name: str) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be a Discord snowflake") from exc
    if parsed <= 0:
        raise ValueError(f"{field_name} must be positive")
    return parsed


@dataclass(frozen=True, slots=True)
class RuntimeConfig:
    token: str = field(repr=False)
    app: AppConfig
    config_path: Path
    database_path: Path
    template_path: Path
    recruitment_complete_template_path: Path
    log_level: str
    dotenv_path: Path
    dotenv_loaded: bool
    working_directory: Path


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def load_app_config(path: Path) -> AppConfig:
    with path.open("r", encoding="utf-8") as file:
        raw = yaml.safe_load(file) or {}

    channels_raw = raw.get("channels", {})
    defaults_raw = raw.get("defaults", {})
    messages_raw = raw.get("messages", {})
    policy_raw = raw.get("policy", {}) or {}
    policy_urls = {}
    for name in ("privacy_url", "terms_url"):
        value = _optional_text(policy_raw.get(name))
        if value:
            url = urlsplit(value)
            if url.scheme != "https" or not url.hostname or url.username or url.password:
                raise ValueError(f"policy.{name} must be a public HTTPS URL")
        policy_urls[name] = value

    defaults = DefaultConfig(
        participant_limit=int(defaults_raw.get("participant_limit", 10)),
        tier_deadline_offset_minutes=int(defaults_raw.get("tier_deadline_offset_minutes", 30)),
        lobby_offset_minutes=int(defaults_raw.get("lobby_offset_minutes", 10)),
        past_time_policy=str(defaults_raw.get("past_time_policy", "reject")),
        recruitment_role_id=(
            _snowflake(defaults_raw["recruitment_role_id"], "defaults.recruitment_role_id")
            if defaults_raw.get("recruitment_role_id") is not None
            else None
        ),
        lobby_voice_channel_id=(
            _snowflake(
                defaults_raw["lobby_voice_channel_id"],
                "defaults.lobby_voice_channel_id",
            )
            if defaults_raw.get("lobby_voice_channel_id") is not None
            else None
        ),
        lobby_voice_channel_2_id=(
            _snowflake(
                defaults_raw["lobby_voice_channel_2_id"],
                "defaults.lobby_voice_channel_2_id",
            )
            if defaults_raw.get("lobby_voice_channel_2_id") is not None
            else None
        ),
        mode_display_fallback=_optional_text(defaults_raw.get("mode_display_fallback")),
        lobby_name=str(defaults_raw.get("lobby_name", "대기실 1번")).strip(),
        lobby_2_name=str(defaults_raw.get("lobby_2_name", "대기실 2번")).strip(),
    )
    if defaults.participant_limit <= 0:
        raise ValueError("participant_limit must be positive")
    if defaults.past_time_policy != "reject":
        raise ValueError("MVP only supports past_time_policy: reject")
    if defaults.lobby_voice_channel_id is None:
        raise ValueError("defaults.lobby_voice_channel_id is required")
    if not defaults.lobby_name:
        raise ValueError("defaults.lobby_name is required")
    if defaults.lobby_voice_channel_2_id is not None and not defaults.lobby_2_name:
        raise ValueError(
            "defaults.lobby_2_name is required when lobby_voice_channel_2_id is set"
        )

    managers: dict[int, ManagerConfig] = {}
    for user_id, manager_raw in (raw.get("managers", {}) or {}).items():
        manager_raw = manager_raw or {}
        managers[_snowflake(user_id, "managers key")] = ManagerConfig(
            default_mode=_optional_text(manager_raw.get("default_mode")),
            extra_notice=str(manager_raw.get("extra_notice", "")).strip(),
        )

    return AppConfig(
        guild_id=_snowflake(raw.get("guild_id"), "guild_id"),
        channels=ChannelConfig(
            command=_snowflake(channels_raw.get("command"), "channels.command"),
            announcement=_snowflake(channels_raw.get("announcement"), "channels.announcement"),
            tier=_snowflake(channels_raw.get("tier"), "channels.tier"),
            admin=_snowflake(channels_raw.get("admin"), "channels.admin"),
            warning=(
                _snowflake(channels_raw["warning"], "channels.warning")
                if channels_raw.get("warning") is not None else None
            ),
        ),
        admin_user_ids=frozenset(
            _snowflake(item, "admin_user_ids") for item in raw.get("admin_user_ids", [])
        ),
        admin_role_ids=frozenset(
            _snowflake(item, "admin_role_ids") for item in raw.get("admin_role_ids", [])
        ),
        defaults=defaults,
        messages=MessageConfig(
            participation_notice=str(messages_raw.get("participation_notice", "")).strip(),
            tier_notice=str(messages_raw.get("tier_notice", "")).strip(),
            manner_notice=str(messages_raw.get("manner_notice", "")).strip(),
        ),
        managers=managers,
        policy=PolicyConfig(
            **policy_urls,
            support_contact=_optional_text(policy_raw.get("support_contact")),
        ),
    )


def _resolve_path(value: str, working_directory: Path) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = working_directory / path
    return path.resolve()


def load_runtime_config(*, base_directory: Path | None = None) -> RuntimeConfig:
    working_directory = (base_directory or Path.cwd()).resolve()
    dotenv_path = working_directory / ".env"
    dotenv_loaded = load_dotenv(dotenv_path=dotenv_path, override=False)

    token = os.environ.get("DISCORD_BOT_TOKEN", "").strip()
    if not token:
        raise ConfigurationError(
            "DISCORD_BOT_TOKEN is required; set it in the environment or the project .env file"
        )

    config_path = _resolve_path(
        os.environ.get("OWKR_CONFIG_PATH", "config/config.yaml"), working_directory
    )
    database_path = _resolve_path(
        os.environ.get("OWKR_DATABASE_PATH", "data/owkr-gather-bot.sqlite3"),
        working_directory,
    )
    template_path = _resolve_path(
        os.environ.get("OWKR_TEMPLATE_PATH", "templates/recruitment.txt"),
        working_directory,
    )
    recruitment_complete_template_path = _resolve_path(
        os.environ.get(
            "OWKR_RECRUITMENT_COMPLETE_TEMPLATE_PATH",
            "templates/recruitment_complete.txt",
        ),
        working_directory,
    )
    log_level = os.environ.get("OWKR_LOG_LEVEL", "INFO").strip().upper()
    if log_level not in ALLOWED_LOG_LEVELS:
        allowed = ", ".join(sorted(ALLOWED_LOG_LEVELS))
        raise ConfigurationError(f"OWKR_LOG_LEVEL must be one of: {allowed}")

    try:
        app = load_app_config(config_path)
    except OSError as exc:
        detail = exc.strerror or exc.__class__.__name__
        raise ConfigurationError(f"failed to read config file {config_path}: {detail}") from exc
    except yaml.YAMLError as exc:
        raise ConfigurationError(f"invalid YAML in config file {config_path}") from exc
    except ValueError as exc:
        raise ConfigurationError(f"invalid config file {config_path}: {exc}") from exc

    return RuntimeConfig(
        token=token,
        app=app,
        config_path=config_path,
        database_path=database_path,
        template_path=template_path,
        recruitment_complete_template_path=recruitment_complete_template_path,
        log_level=log_level,
        dotenv_path=dotenv_path,
        dotenv_loaded=dotenv_loaded,
        working_directory=working_directory,
    )
