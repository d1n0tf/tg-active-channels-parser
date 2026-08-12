from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from ChannelsParser.collector import (
    CandidateSource,
    TelegramChannelCollector,
    _comment_count,
    _collect_gift_channel_refs,
    _collect_personal_channel_ref,
    _peer_id_value,
    _reaction_count,
    _views,
    extract_channel_references,
    normalize_channel_identifier,
)
from ChannelsParser.models import SearchFilters
from telethon.errors import RpcCallFailError
from telethon import types


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


def test_extract_channel_references_reads_links_and_mentions() -> None:
    assert extract_channel_references("каналы: https://t.me/fashion_shop и @brand_agency") == [
        "fashion_shop",
        "brand_agency",
    ]


def test_collect_personal_channel_ref_reads_user_full_chats() -> None:
    candidates: dict[str, str] = {}
    full = SimpleNamespace(
        full_user=SimpleNamespace(personal_channel_id=123),
        chats=[
            types.Channel(
                id=123,
                title="Personal brand",
                photo=types.ChatPhotoEmpty(),
                date=None,
                broadcast=True,
                username="personal_brand",
            )
        ],
    )

    assert _collect_personal_channel_ref(
        candidates,
        full,
        owner_username="owner_user",
        owner_display_name="Owner Name",
    ) == 1
    assert candidates["personal_brand"].owner_username == "owner_user"
    assert candidates["personal_brand"].owner_display_name == "Owner Name"
    assert candidates == {"personal_brand": "личный канал профиля"}


def test_collect_gift_channel_refs_reads_channel_gifters() -> None:
    candidates: dict[str, str] = {}
    saved_gifts = SimpleNamespace(
        gifts=[
            SimpleNamespace(from_id=types.PeerChannel(channel_id=123)),
            SimpleNamespace(from_id=types.PeerUser(user_id=456)),
        ],
        chats=[
            types.Channel(
                id=123,
                title="Gift sender",
                photo=types.ChatPhotoEmpty(),
                date=None,
                broadcast=True,
                username="gift_sender",
            )
        ],
    )

    assert _collect_gift_channel_refs(candidates, saved_gifts) == 1
    assert candidates == {"gift_sender": "подарок от канала"}


def test_collect_sender_refs_checks_gifts_without_profile_refs() -> None:
    async def scenario() -> None:
        collector = GiftOnlyCollector()
        candidates: dict[str, CandidateSource] = {}
        stats = {
            "profiles_seen": 0,
            "profiles_skipped_by_limit": 0,
            "comment_refs": 0,
            "bio_refs": 0,
            "personal_channel_refs": 0,
            "gift_profiles_checked": 0,
            "gift_fetch_errors": 0,
            "gift_refs": 0,
            "channel_commenter_refs": 0,
        }
        message = SimpleNamespace(
            sender=types.User(
                id=42,
                is_self=False,
                access_hash=123,
                first_name="Owner",
                username="owner_user",
            ),
            message="",
        )

        await collector._collect_sender_refs(
            candidates,
            message,  # type: ignore[arg-type]
            seen_sender_ids=set(),
            limit=10,
            profile_limit=0,
            gift_limit=5,
            include_comment_links=False,
            include_profile_refs=False,
            stats=stats,
        )

        assert stats["profiles_seen"] == 0
        assert stats["gift_refs"] == 1
        assert candidates == {"gift_sender": "gift source"}

    asyncio.run(scenario())


def test_collect_sender_refs_uses_resilient_lookup_when_sender_is_missing() -> None:
    class SenderLookupCollector(GiftOnlyCollector):
        def __init__(self) -> None:
            super().__init__()
            self.resilient_calls = 0

        async def _with_resilient_call(self, func, *args, **kwargs):
            self.resilient_calls += 1
            return await func(*args, **kwargs)

    async def scenario() -> None:
        collector = SenderLookupCollector()
        sender = types.User(
            id=42,
            is_self=False,
            access_hash=123,
            first_name="Owner",
            username="owner_user",
        )

        class MessageWithoutSender:
            sender = None
            message = ""

            async def get_sender(self):
                return sender

        stats = {
            "profiles_seen": 0,
            "profiles_skipped_by_limit": 0,
            "comment_refs": 0,
            "bio_refs": 0,
            "personal_channel_refs": 0,
            "gift_profiles_checked": 0,
            "gift_fetch_errors": 0,
            "gift_refs": 0,
            "channel_commenter_refs": 0,
        }
        await collector._collect_sender_refs(
            {},
            MessageWithoutSender(),  # type: ignore[arg-type]
            seen_sender_ids=set(),
            limit=10,
            profile_limit=0,
            gift_limit=0,
            include_comment_links=False,
            include_profile_refs=False,
            stats=stats,
        )

        assert collector.resilient_calls == 1

    asyncio.run(scenario())


def test_peer_id_value_reads_channel_peer() -> None:
    assert _peer_id_value(types.PeerChannel(channel_id=123)) == 123


def test_search_channels_skips_unexpected_channel_errors() -> None:
    async def scenario() -> None:
        collector = BrokenCollector()
        result = await collector.search_channels(["x"], SearchFilters(audience_bias="any", min_activity_score=0))

        assert result.reports == []
        assert result.total_candidates == 1
        assert result.skipped_channels == 1
        assert "RuntimeError: boom" in result.errors[0]

    asyncio.run(scenario())


def test_resilient_call_retries_server_rpc_error(monkeypatch: pytest.MonkeyPatch) -> None:
    class RetryCollector(TelegramChannelCollector):
        def __init__(self) -> None:
            self._settings = SimpleNamespace(
                flood_sleep_limit_seconds=0, flood_switch_threshold_seconds=60
            )
            self._pool = _FakePool()  # type: ignore[assignment]
            self.reconnects = 0

        async def _recover_from_transient_error(self, exc, *, failures_on_account: int) -> None:
            self.reconnects += 1

    async def no_sleep(_: float) -> None:
        return None

    monkeypatch.setattr("ChannelsParser.collector.asyncio.sleep", no_sleep)
    collector = RetryCollector()

    async def scenario() -> None:
        calls = 0

        async def operation() -> str:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RpcCallFailError(None)
            return "ok"

        assert await collector._with_resilient_call(operation, attempts=2) == "ok"
        assert calls == 2
        assert collector.reconnects == 1

    asyncio.run(scenario())


def test_resilient_call_times_out_and_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    class RetryCollector(TelegramChannelCollector):
        def __init__(self) -> None:
            self._settings = SimpleNamespace(
                flood_sleep_limit_seconds=0,
                flood_switch_threshold_seconds=60,
                telegram_request_timeout_seconds=1,
            )
            self._pool = _FakePool()  # type: ignore[assignment]
            self.recoveries = 0

        async def _recover_from_transient_error(self, exc, *, failures_on_account: int) -> None:
            self.recoveries += 1

    async def no_sleep(_: float) -> None:
        return None

    monkeypatch.setattr("ChannelsParser.collector.asyncio.sleep", no_sleep)
    collector = RetryCollector()

    async def scenario() -> None:
        calls = 0

        async def operation() -> str:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise asyncio.TimeoutError()
            return "ok"

        assert await collector._with_resilient_call(operation, attempts=2) == "ok"
        assert calls == 2
        assert collector.recoveries == 1

    asyncio.run(scenario())


def test_resilient_call_checks_connection_before_each_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class RetryCollector(TelegramChannelCollector):
        def __init__(self) -> None:
            self._settings = SimpleNamespace(
                flood_sleep_limit_seconds=0,
                flood_switch_threshold_seconds=60,
                telegram_request_timeout_seconds=1,
            )
            self._pool = _FakePool()  # type: ignore[assignment]
            self.connection_checks = 0

        async def _ensure_connected(self) -> None:
            self.connection_checks += 1

        async def _recover_from_transient_error(self, exc, *, failures_on_account: int) -> None:
            return None

    async def no_sleep(_: float) -> None:
        return None

    monkeypatch.setattr("ChannelsParser.collector.asyncio.sleep", no_sleep)
    collector = RetryCollector()

    async def scenario() -> None:
        calls = 0

        async def operation() -> str:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise asyncio.TimeoutError()
            return "ok"

        assert await collector._with_resilient_call(operation, attempts=2) == "ok"
        assert collector.connection_checks == 2

    asyncio.run(scenario())


class _FakePool:
    async def acquire(self):
        return object()

    def bind_lease(self, lease):
        return object()

    def unbind_lease(self, token):
        return None

    async def release(self, lease):
        return None

    def current_lease(self):
        return object()

    def list_info(self):
        return []

    async def ensure_connected(self):
        return None

    async def reconnect_active(self):
        return None

    async def rotate_lease_to_healthy(self, *, reason: str):
        return False


class BrokenCollector(TelegramChannelCollector):
    def __init__(self) -> None:
        self._settings = SimpleNamespace(flood_sleep_limit_seconds=0, flood_switch_threshold_seconds=60)
        self._pool = _FakePool()  # type: ignore[assignment]

    async def _search_public_chats(self, query: str):
        return [SimpleNamespace(id=1, username="broken", broadcast=True)]

    async def inspect_channel(self, *args, **kwargs):
        raise RuntimeError("boom")


class GiftOnlyCollector(TelegramChannelCollector):
    def __init__(self) -> None:
        self._settings = SimpleNamespace(flood_sleep_limit_seconds=0, flood_switch_threshold_seconds=60)
        self._pool = _FakePool()  # type: ignore[assignment]

    async def _collect_gift_refs(self, candidates, sender, *, gift_limit, stats):
        candidates["gift_sender"] = CandidateSource("gift source")
        return 1
