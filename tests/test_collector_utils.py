from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from ChannelsParser.collector import (
    TelegramChannelCollector,
    _comment_count,
    _reaction_count,
    _views,
    normalize_channel_identifier,
)
from ChannelsParser.models import SearchFilters


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("@durov", "durov"),
        ("https://t.me/durov", "durov"),
        ("https://telegram.me/durov", "durov"),
        ("https://t.me/s/durov", "durov"),
        ("t.me/durov/123?single", "durov"),
    ],
)
def test_normalize_channel_identifier(raw: str, expected: str) -> None:
    assert normalize_channel_identifier(raw) == expected


def test_normalize_channel_identifier_rejects_invite_links() -> None:
    with pytest.raises(ValueError):
        normalize_channel_identifier("https://t.me/+abcdef")


def test_normalize_channel_identifier_rejects_private_internal_links() -> None:
    with pytest.raises(ValueError):
        normalize_channel_identifier("https://t.me/c/123456/789")


def test_normalize_channel_identifier_rejects_invalid_username() -> None:
    with pytest.raises(ValueError):
        normalize_channel_identifier("https://t.me/not-a-channel")


def test_message_counters_tolerate_unexpected_values() -> None:
    message = SimpleNamespace(
        views="not-a-number",
        reactions=SimpleNamespace(results=[SimpleNamespace(count="3"), SimpleNamespace(count="bad")]),
        replies=SimpleNamespace(replies=-5),
    )

    assert _views(message) is None  # type: ignore
    assert _reaction_count(message) == 3  # type: ignore
    assert _comment_count(message) == 0  # type: ignore


def test_search_channels_skips_unexpected_channel_errors() -> None:
    async def scenario() -> None:
        collector = BrokenCollector()
        result = await collector.search_channels(["x"], SearchFilters(audience_bias="any", min_activity_score=0))

        assert result.reports == []
        assert result.total_candidates == 1
        assert result.skipped_channels == 1
        assert "RuntimeError: boom" in result.errors[0]

    asyncio.run(scenario())


class BrokenCollector(TelegramChannelCollector):
    def __init__(self) -> None:
        self._settings = SimpleNamespace(flood_sleep_limit_seconds=0)

    async def _search_public_chats(self, query: str):
        return [SimpleNamespace(id=1, username="broken", broadcast=True)]

    async def inspect_channel(self, *args, **kwargs):
        raise RuntimeError("boom")
