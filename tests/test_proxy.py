from __future__ import annotations

import socks
import pytest

from ChannelsParser.proxy import ProxyConfigError, aiogram_proxy, parse_proxy_url, telethon_proxy


def test_parse_proxy_url_accepts_http_with_auth() -> None:
    proxy = parse_proxy_url("http://user:pass@127.0.0.1:8080")

    assert proxy.scheme == "http"
    assert proxy.host == "127.0.0.1"
    assert proxy.port == 8080
    assert proxy.username == "user"
    assert proxy.password == "pass"
    assert aiogram_proxy(proxy.url) == "http://user:pass@127.0.0.1:8080"


def test_telethon_proxy_converts_socks5_url() -> None:
    proxy = telethon_proxy("socks5://user:pass@localhost:1080")

    assert proxy == (socks.SOCKS5, "localhost", 1080, True, "user", "pass")


def test_parse_proxy_url_rejects_shadowsocks_url() -> None:
    with pytest.raises(ProxyConfigError, match="Shadowsocks local client"):
        parse_proxy_url("ss://example")


def test_parse_proxy_url_requires_port() -> None:
    with pytest.raises(ProxyConfigError, match="port"):
        parse_proxy_url("socks5://127.0.0.1")
