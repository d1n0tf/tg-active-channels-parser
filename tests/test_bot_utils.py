from __future__ import annotations

import asyncio
from types import SimpleNamespace

from ChannelsParser.bot import (
    AccessControl,
    BotState,
    filter_section_keyboard,
    filters_keyboard,
    format_access_denied,
    format_access_status,
    format_allowlist,
    format_filter_dashboard,
    format_main_menu,
    format_remaining_duration,
    format_renewal_reminder,
    main_keyboard,
    parse_allow_args,
    parse_discover_args,
    run_discovery,
    run_scan,
    scan_cancel_keyboard,
    split_text,
    _parse_user_ids,
)
from ChannelsParser.models import DiscoveryOptions, SearchFilters, SearchRunResult, discovery_filters


def test_split_text_respects_limit_for_long_blocks() -> None:
    chunks = split_text("a" * 25, limit=10)

    assert chunks == ["a" * 10, "a" * 10, "a" * 5]


def test_split_text_prefers_paragraph_boundaries() -> None:
    chunks = split_text("first\n\nsecond\n\nthird", limit=14)

    assert chunks == ["first\n\nsecond", "third"]


def test_filter_dashboard_uses_grouped_controls() -> None:
    filters = SearchFilters()
    markup = filters_keyboard(filters)
    rows = [[button.text for button in row] for row in markup.inline_keyboard]

    assert "Фильтры поиска" in format_filter_dashboard(filters)
    assert rows[0] == ["Подписчики: 1k-50k", "Посты: <= 7 дн."]
    assert ["Тип: тематические", "ЦА: женская"] in rows
    assert ["Возраст: любой", "Сортировка: score"] in rows
    assert len([button for row in rows for button in row]) == 11


def test_filter_section_keyboard_shows_only_one_group() -> None:
    markup = filter_section_keyboard("subs", SearchFilters())
    buttons = [button.text for row in markup.inline_keyboard for button in row]

    assert buttons == ["100-300", "300-1k", "1k-5k", "5k-20k", "20k-50k", "От 50k", "Любые", "К фильтрам"]


def test_parse_discover_args_accepts_optional_post_limit() -> None:
    identifier, post_limit, options = parse_discover_args("@source 200")
    assert (identifier, post_limit) == ("@source", 200)
    assert options == DiscoveryOptions()

    identifier, post_limit, options = parse_discover_args("https://t.me/source")
    assert (identifier, post_limit) == ("https://t.me/source", 100)
    assert options == DiscoveryOptions()


def test_main_keyboard_matches_reference_menu() -> None:
    rows = [[button.text for button in row] for row in main_keyboard().inline_keyboard]

    assert rows == [
        ["🧻 Парсинг"],
        ["💾 База данных", "🧾 Мои аккаунты"],
        ["💎 Подписка", "🎧 Поддержка"],
    ]


def test_parsing_keyboard_has_core_actions() -> None:
    from ChannelsParser.bot import parsing_keyboard

    buttons = [button.text for row in parsing_keyboard().inline_keyboard for button in row]

    assert "🧩 Пресеты запросов" in buttons
    assert "🔎 Свой поиск" in buttons
    assert "🔗 Discovery" in buttons
    assert "🧪 Check канала" in buttons


def test_scan_cancel_keyboard_uses_reference_actions() -> None:
    buttons = [button.text for row in scan_cancel_keyboard().inline_keyboard for button in row]

    assert buttons == ["💾 Завершить досрочно", "🚫 Отмена"]


def test_parse_user_ids_and_access_control() -> None:
    assert _parse_user_ids("111 222,333") == [111, 222, 333]
    assert parse_allow_args("123456789") == ([123456789], None)
    assert parse_allow_args("123456789 30") == ([123456789], 30)
    assert parse_allow_args("111 222") == ([111, 222], None)
    assert parse_allow_args("5000 30d") == ([5000], 30)
    assert parse_allow_args("111 222 7d") == ([111, 222], 7)
    assert parse_allow_args("111 222 7д") == ([111, 222], 7)
    storage = FakeStorage()

    class S:
        admin_user_ids = frozenset({1})

        def is_admin(self, user_id: int) -> bool:
            return user_id == 1

    access = AccessControl(S(), storage)  # type: ignore
    assert access.has_access(1) is True
    assert access.has_access(100) is False
    storage.grant_access(100, granted_by=1, days=7)
    assert access.has_access(100) is True
    assert "закрыт" in format_access_denied(100).lower() or "закрыт" in format_access_denied(100)
    text = format_allowlist(S(), storage)  # type: ignore
    assert "100" in text
    assert "1" in text
    status = format_access_status(S(), storage, 100)  # type: ignore
    assert "Доступ" in status or "доступ" in status.lower()
    assert "maxxkireev" in format_renewal_reminder(
        __import__("datetime").datetime.now(__import__("datetime").timezone.utc)
        + __import__("datetime").timedelta(hours=10)
    )
    assert "ч." in format_remaining_duration(__import__("datetime").timedelta(hours=5, minutes=12))
    menu = format_main_menu(S(), storage, 100)  # type: ignore
    assert "Доступ" in menu or "доступ" in menu.lower()


def test_bot_state_soft_finish_does_not_hard_cancel() -> None:
    storage = FakeStorage()
    state = BotState(storage)  # type: ignore

    state.finish_scan_collection()
    assert state.scan_finish_collection_requested() is True
    assert state.scan_cancelled() is False

    state.reset_scan_cancel()
    state.cancel_scan()
    assert state.scan_cancelled() is True
    assert state.scan_finish_collection_requested() is True


def test_parse_discover_args_accepts_source_toggles_and_subscribers() -> None:
    identifier, post_limit, options = parse_discover_args(
        "@source 200 comments off profile:on gifts выкл subs 100 300"
    )

    assert (identifier, post_limit) == ("@source", 200)
    assert options.include_comment_links is False
    assert options.include_profile_refs is True
    assert options.include_gifts is False
    assert options.min_subscribers == 100
    assert options.max_subscribers == 300


def test_discovery_filters_keep_activity_and_relax_profile_filters() -> None:
    filters = discovery_filters(
        SearchFilters(
            min_subscribers=1000,
            max_subscribers=50000,
            max_last_post_days=3,
            min_activity_score=55,
            min_avg_views=200,
            channel_kind="commercial",
            audience_bias="female",
            age_group="25-34",
        )
    )

    assert filters.min_subscribers is None
    assert filters.max_subscribers is None
    assert filters.min_avg_views is None
    assert filters.channel_kind == "any"
    assert filters.audience_bias == "any"
    assert filters.age_group == "any"
    assert filters.max_last_post_days == 3
    assert filters.min_activity_score == 55


def test_discovery_filters_apply_explicit_subscriber_range() -> None:
    filters = discovery_filters(SearchFilters(), DiscoveryOptions(min_subscribers=100, max_subscribers=300))

    assert filters.min_subscribers == 100
    assert filters.max_subscribers == 300


def test_run_scan_rejects_concurrent_scans() -> None:
    async def scenario() -> None:
        storage = FakeStorage()
        state = BotState(storage)  # type: ignore
        collector = FakeCollector()
        settings = SimpleNamespace(top_results=10)
        first_message = FakeMessage()
        second_message = FakeMessage()

        await asyncio.gather(
            run_scan(first_message, ["семейный бюджет"], state, collector, storage, settings, user_id=42),  # type: ignore
            run_scan(second_message, ["финансы"], state, collector, storage, settings, user_id=42),  # type: ignore
        )

        assert len(storage.created_scans) == 1
        answers = first_message.answers + second_message.answers
        assert any("Сейчас уже идет поиск" in answer for answer in answers)

    asyncio.run(scenario())


def test_run_discovery_uses_broad_active_filters() -> None:
    async def scenario() -> None:
        base_filters = SearchFilters(
            min_subscribers=1000,
            max_subscribers=50000,
            min_avg_views=100,
            channel_kind="thematic",
            audience_bias="female",
            age_group="18-24",
            min_activity_score=50,
        )
        storage = FakeStorage(base_filters)
        state = BotState(storage)  # type: ignore
        collector = FakeDiscoveryCollector()
        settings = SimpleNamespace(
            top_results=10,
            discovery_comments_per_post=100,
            discovery_profile_limit=500,
            discovery_candidate_limit=300,
            discovery_gift_limit=10,
        )
        message = FakeMessage()

        await run_discovery(message, "@source", 200, state, collector, storage, settings, user_id=42)  # type: ignore

        assert collector.filters is not None
        assert collector.filters.min_subscribers is None
        assert collector.filters.max_subscribers is None
        assert collector.filters.min_avg_views is None
        assert collector.filters.channel_kind == "any"
        assert collector.filters.audience_bias == "any"
        assert collector.filters.age_group == "any"
        assert collector.filters.min_activity_score == 50
        assert collector.kwargs["gift_limit"] == 10
        assert collector.kwargs["include_comment_links"] is True
        assert collector.kwargs["include_profile_refs"] is True
        assert callable(collector.kwargs.get("should_finish_collection"))
        assert storage.created_scan_filters == [collector.filters]
        assert any("@source" in answer and "0/200" in answer for answer in message.answers)

    asyncio.run(scenario())


class FakeMessage:
    def __init__(self) -> None:
        self.answers: list[str] = []

    async def answer(self, text: str, **kwargs) -> None:
        self.answers.append(text)


class FakeCollector:
    async def search_channels(self, queries, filters, **kwargs) -> SearchRunResult:
        await asyncio.sleep(0.01)
        return SearchRunResult(reports=[], total_candidates=0)


class FakeDiscoveryCollector:
    def __init__(self) -> None:
        self.filters: SearchFilters | None = None
        self.kwargs = {}

    async def discover_channels_from_comments(self, identifier, filters, **kwargs) -> SearchRunResult:
        self.filters = filters
        self.kwargs = kwargs
        return SearchRunResult(reports=[], total_candidates=0)


class FakeStorage:
    def __init__(self, filters: SearchFilters | None = None) -> None:
        self.filters = filters or SearchFilters()
        self.created_scans: list[str] = []
        self.created_scan_filters: list[SearchFilters] = []
        self._scan_meta: dict[str, dict] = {}
        self._reports: dict[str, list] = {}

    def get_user_filters(self, user_id: int) -> SearchFilters:
        return self.filters

    def create_scan(self, scan_id: str, **kwargs) -> None:
        self.created_scans.append(scan_id)
        self.created_scan_filters.append(kwargs["filters"])
        self._scan_meta[scan_id] = {
            "user_id": kwargs.get("user_id"),
            "mode": kwargs.get("mode", "search"),
            "queries": kwargs.get("queries", []),
            "filters": kwargs["filters"],
        }
        self._reports[scan_id] = []

    def save_reports(self, scan_id: str, reports) -> None:
        self._reports[scan_id] = list(reports)

    def finish_scan(self, scan_id: str, **kwargs) -> None:
        pass

    def fail_scan(self, scan_id: str, **kwargs) -> None:
        pass

    def get_scan(self, scan_id: str, *, user_id: int | None = None):
        meta = self._scan_meta.get(scan_id)
        if meta is None:
            return None
        if user_id is not None and meta.get("user_id") != user_id:
            return None
        from datetime import datetime, timezone

        from ChannelsParser.models import ScanRecord

        return ScanRecord(
            scan_id=scan_id,
            user_id=meta.get("user_id"),
            mode=meta.get("mode", "search"),
            status="done",
            queries=meta.get("queries", []),
            filters=meta.get("filters", SearchFilters()),
            total_candidates=0,
            total_reports=len(self._reports.get(scan_id, [])),
            error=None,
            started_at=datetime.now(timezone.utc),
        )

    def count_reports(self, scan_id: str) -> int:
        return len(self._reports.get(scan_id, []))

    def reports_page(self, scan_id: str, *, offset: int = 0, limit: int = 8):
        rows = self._reports.get(scan_id, [])
        return rows[offset : offset + limit]

    def scan_ordinal(self, scan_id: str) -> int:
        try:
            return self.created_scans.index(scan_id) + 1
        except ValueError:
            return 1

    def delete_scan(self, scan_id: str, *, user_id: int) -> bool:
        meta = self._scan_meta.get(scan_id)
        if meta is None or meta.get("user_id") != user_id:
            return False
        self._scan_meta.pop(scan_id, None)
        self._reports.pop(scan_id, None)
        return True

    def is_user_allowed(self, user_id: int) -> bool:
        return self.get_access_expiry(user_id) is not False

    def get_access_expiry(self, user_id: int):
        from datetime import datetime, timezone

        if user_id not in getattr(self, "_allowed", set()):
            return False
        grants = getattr(self, "_grants", [])
        for g in grants:
            if g[0] == user_id:
                return g[3]  # expires_at or None
        return None

    def list_users_needing_renewal_reminder(self, *, within_hours: float = 12.0):
        from datetime import datetime, timezone, timedelta

        now = datetime.now(timezone.utc)
        horizon = now + timedelta(hours=within_hours)
        notified = getattr(self, "_renewal_notified", set())
        out = []
        for g in getattr(self, "_grants", []):
            user_id, _by, _created, expires = g
            if expires is None or user_id in notified:
                continue
            if now < expires <= horizon:
                out.append((user_id, expires))
        return out

    def mark_renewal_notified(self, user_id: int) -> None:
        notified = getattr(self, "_renewal_notified", set())
        notified.add(user_id)
        self._renewal_notified = notified

    def grant_access(
        self,
        user_id: int,
        *,
        granted_by: int | None = None,
        note: str | None = None,
        days: int | None = None,
    ) -> str:
        from datetime import datetime, timedelta, timezone

        allowed = getattr(self, "_allowed", set())
        now = datetime.now(timezone.utc)
        expires = None if days is None else now + timedelta(days=days)
        grants = getattr(self, "_grants", [])
        existing = next((i for i, g in enumerate(grants) if g[0] == user_id), None)
        row = (user_id, granted_by, now, expires)
        if existing is not None:
            grants[existing] = row
            self._grants = grants
            notified = getattr(self, "_renewal_notified", set())
            notified.discard(user_id)
            self._renewal_notified = notified
            return "updated"
        allowed.add(user_id)
        self._allowed = allowed
        grants.append(row)
        self._grants = grants
        notified = getattr(self, "_renewal_notified", set())
        notified.discard(user_id)
        self._renewal_notified = notified
        return "created"

    def revoke_access(self, user_id: int) -> bool:
        allowed = getattr(self, "_allowed", set())
        if user_id not in allowed:
            return False
        allowed.discard(user_id)
        self._allowed = allowed
        grants = getattr(self, "_grants", [])
        self._grants = [g for g in grants if g[0] != user_id]
        return True

    def list_allowed_users(self, *, limit: int = 200):
        return list(getattr(self, "_grants", []))[:limit]
