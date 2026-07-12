from __future__ import annotations

import pytest

from ChannelsParser.config import AppSettings, ConfigError


def test_settings_use_common_proxy_for_bot_and_telegram(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_required_env(monkeypatch)
    monkeypatch.setenv("PROXY_URL", "socks5://127.0.0.1:1080")

    settings = AppSettings.from_env(require_bot_token=True)

    assert settings.bot_proxy_url == "socks5://127.0.0.1:1080"
    assert settings.telegram_proxy_url == "socks5://127.0.0.1:1080"


def test_settings_allow_proxy_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_required_env(monkeypatch)
    monkeypatch.setenv("PROXY_URL", "socks5://127.0.0.1:1080")
    monkeypatch.setenv("BOT_PROXY_URL", "http://127.0.0.1:8080")
    monkeypatch.setenv("TELEGRAM_PROXY_URL", "socks5://127.0.0.1:1081")

    settings = AppSettings.from_env(require_bot_token=True)

    assert settings.bot_proxy_url == "http://127.0.0.1:8080"
    assert settings.telegram_proxy_url == "socks5://127.0.0.1:1081"


def test_settings_reject_direct_shadowsocks_proxy(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_required_env(monkeypatch)
    monkeypatch.setenv("PROXY_URL", "ss://example")

    with pytest.raises(ConfigError, match="Shadowsocks local client"):
        AppSettings.from_env(require_bot_token=True)


def test_settings_allow_disabling_gift_discovery(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_required_env(monkeypatch)
    monkeypatch.setenv("DISCOVERY_GIFT_LIMIT", "0")

    settings = AppSettings.from_env(require_bot_token=True)

    assert settings.discovery_gift_limit == 0


def _set_required_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BOT_TOKEN", "123456:test")
    monkeypatch.setenv("TELEGRAM_API_ID", "123456")
    monkeypatch.setenv("TELEGRAM_API_HASH", "hash")
    monkeypatch.delenv("PROXY_URL", raising=False)
    monkeypatch.delenv("BOT_PROXY_URL", raising=False)
    monkeypatch.delenv("TELEGRAM_PROXY_URL", raising=False)
    monkeypatch.delenv("DISCOVERY_GIFT_LIMIT", raising=False)
