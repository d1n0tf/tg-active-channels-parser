from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

from ChannelsParser.proxy import ProxyConfigError, validate_proxy_url


class ConfigError(RuntimeError):
    pass


@dataclass(frozen=True)
class AppSettings:
    bot_token: str | None
    telegram_api_id: int
    telegram_api_hash: str
    telegram_session: str
    telegram_phone: str | None
    database_path: Path
    search_limit_per_query: int
    history_limit: int
    top_results: int
    flood_sleep_limit_seconds: int
    discovery_comments_per_post: int
    discovery_profile_limit: int
    discovery_candidate_limit: int
    discovery_gift_limit: int
    bot_proxy_url: str | None
    telegram_proxy_url: str | None

    @classmethod
    def from_env(cls, *, require_bot_token: bool = True) -> "AppSettings":
        load_dotenv()

        bot_token = _optional("BOT_TOKEN")
        if require_bot_token and not bot_token:
            raise ConfigError("BOT_TOKEN is required in .env")

        api_id_raw = _required("TELEGRAM_API_ID")
        try:
            api_id = int(api_id_raw)
        except ValueError as exc:
            raise ConfigError("TELEGRAM_API_ID must be an integer") from exc

        proxy_url = _optional("PROXY_URL")
        try:
            bot_proxy_url = validate_proxy_url(_optional("BOT_PROXY_URL", proxy_url))
            telegram_proxy_url = validate_proxy_url(_optional("TELEGRAM_PROXY_URL", proxy_url))
        except ProxyConfigError as exc:
            raise ConfigError(str(exc)) from exc

        return cls(
            bot_token=bot_token,
            telegram_api_id=api_id,
            telegram_api_hash=_required("TELEGRAM_API_HASH"),
            telegram_session=_optional("TELEGRAM_SESSION", "data/telegram.session") or "data/telegram.session",
            telegram_phone=_optional("TELEGRAM_PHONE"),
            database_path=Path(_optional("DATABASE_PATH", "data/channels.sqlite3") or "data/channels.sqlite3"),
            search_limit_per_query=_positive_int("SEARCH_LIMIT_PER_QUERY", 40),
            history_limit=_positive_int("HISTORY_LIMIT", 40),
            top_results=_positive_int("TOP_RESULTS", 10),
            flood_sleep_limit_seconds=_positive_int("FLOOD_SLEEP_LIMIT_SECONDS", 60),
            discovery_comments_per_post=_positive_int("DISCOVERY_COMMENTS_PER_POST", 100),
            discovery_profile_limit=_positive_int("DISCOVERY_PROFILE_LIMIT", 500),
            discovery_candidate_limit=_positive_int("DISCOVERY_CANDIDATE_LIMIT", 300),
            discovery_gift_limit=_non_negative_int("DISCOVERY_GIFT_LIMIT", 10),
            bot_proxy_url=bot_proxy_url,
            telegram_proxy_url=telegram_proxy_url,
        )


def database_path_from_env() -> Path:
    load_dotenv()
    return Path(_optional("DATABASE_PATH", "data/channels.sqlite3") or "data/channels.sqlite3")


def _required(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise ConfigError(f"{name} is required in .env")
    return value.strip()


def _optional(name: str, default: str | None = None) -> str | None:
    value = os.getenv(name)
    if value is None or value == "":
        return default
    return value.strip()


def _positive_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if not value:
        return default
    try:
        parsed = int(value)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer") from exc
    if parsed <= 0:
        raise ConfigError(f"{name} must be positive")
    return parsed


def _non_negative_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if not value:
        return default
    try:
        parsed = int(value)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer") from exc
    if parsed < 0:
        raise ConfigError(f"{name} must be >= 0")
    return parsed
