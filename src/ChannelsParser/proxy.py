from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import unquote, urlparse

import socks


SUPPORTED_PROXY_SCHEMES = {"http", "https", "socks4", "socks4a", "socks5", "socks5h"}


class ProxyConfigError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class ParsedProxy:
    url: str
    scheme: str
    host: str
    port: int
    username: str | None = None
    password: str | None = None


def validate_proxy_url(value: str | None) -> str | None:
    if not value:
        return None
    return parse_proxy_url(value).url


def aiogram_proxy(value: str | None) -> str | None:
    parsed = parse_proxy_url(value) if value else None
    return parsed.url if parsed else None


def telethon_proxy(value: str | None) -> tuple[Any, ...] | None:
    parsed = parse_proxy_url(value) if value else None
    if parsed is None:
        return None

    proxy_type = _telethon_proxy_type(parsed.scheme)
    rdns = parsed.scheme in {"socks4a", "socks5", "socks5h"}
    return (
        proxy_type,
        parsed.host,
        parsed.port,
        rdns,
        parsed.username,
        parsed.password,
    )


def parse_proxy_url(value: str | None) -> ParsedProxy:
    if value is None:
        raise ProxyConfigError("Proxy URL is empty")

    raw = value.strip()
    parsed = urlparse(raw)
    scheme = parsed.scheme.lower()

    if scheme == "ss":
        raise ProxyConfigError(
            "PROXY_URL=ss://... is not supported directly. Start a Shadowsocks local client "
            "and use its local endpoint, for example PROXY_URL=socks5://127.0.0.1:1080"
        )
    if scheme not in SUPPORTED_PROXY_SCHEMES:
        supported = ", ".join(sorted(SUPPORTED_PROXY_SCHEMES))
        raise ProxyConfigError(f"Unsupported proxy scheme '{parsed.scheme}'. Supported: {supported}")
    if not parsed.hostname:
        raise ProxyConfigError("Proxy URL must include host")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ProxyConfigError("Proxy URL port must be an integer") from exc
    if port is None:
        raise ProxyConfigError("Proxy URL must include port")

    return ParsedProxy(
        url=raw,
        scheme=scheme,
        host=parsed.hostname,
        port=port,
        username=unquote(parsed.username) if parsed.username else None,
        password=unquote(parsed.password) if parsed.password else None,
    )


def _telethon_proxy_type(scheme: str) -> int:
    if scheme in {"http", "https"}:
        return socks.HTTP
    if scheme in {"socks4", "socks4a"}:
        return socks.SOCKS4
    if scheme in {"socks5", "socks5h"}:
        return socks.SOCKS5
    raise ProxyConfigError(f"Unsupported proxy scheme '{scheme}'")
