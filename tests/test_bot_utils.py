from __future__ import annotations

import asyncio
from types import SimpleNamespace

from ChannelsParser.bot import (
    BotState,
    filter_section_keyboard,
    filters_keyboard,
    format_filter_dashboard,
    main_keyboard,
    parse_discover_args,
    run_discovery,
    run_scan,
    scan_cancel_keyboard,
    split_text,
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

    def get_user_filters(self, user_id: int) -> SearchFilters:
        return self.filters

    def create_scan(self, scan_id: str, **kwargs) -> None:
        self.created_scans.append(scan_id)
        self.created_scan_filters.append(kwargs["filters"])

    def save_reports(self, scan_id: str, reports) -> None:
        pass

    def finish_scan(self, scan_id: str, **kwargs) -> None:
        pass

    def fail_scan(self, scan_id: str, **kwargs) -> None:
        pass
