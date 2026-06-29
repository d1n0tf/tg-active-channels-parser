from __future__ import annotations

import asyncio
from types import SimpleNamespace

from ChannelsParser.bot import (
    BotState,
    filter_section_keyboard,
    filters_keyboard,
    format_filter_dashboard,
    run_scan,
    split_text,
)
from ChannelsParser.models import SearchFilters, SearchRunResult


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
    assert rows[0] == ["Подписчики: 100-5k", "Посты: <= 7 дн."]
    assert ["Сортировка: score"] in rows
    assert len([button for row in rows for button in row]) == 10


def test_filter_section_keyboard_shows_only_one_group() -> None:
    markup = filter_section_keyboard("subs", SearchFilters())
    buttons = [button.text for row in markup.inline_keyboard for button in row]

    assert buttons == ["100-300", "300-1k", "1k-5k", "5k-20k", "От 20k", "Любые", "К фильтрам"]


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


class FakeMessage:
    def __init__(self) -> None:
        self.answers: list[str] = []

    async def answer(self, text: str, **kwargs) -> None:
        self.answers.append(text)


class FakeCollector:
    async def search_channels(self, queries, filters) -> SearchRunResult:
        await asyncio.sleep(0.01)
        return SearchRunResult(reports=[], total_candidates=0)


class FakeStorage:
    def __init__(self) -> None:
        self.created_scans: list[str] = []

    def get_user_filters(self, user_id: int) -> SearchFilters:
        return SearchFilters()

    def create_scan(self, scan_id: str, **kwargs) -> None:
        self.created_scans.append(scan_id)

    def save_reports(self, scan_id: str, reports) -> None:
        pass

    def finish_scan(self, scan_id: str, **kwargs) -> None:
        pass

    def fail_scan(self, scan_id: str, **kwargs) -> None:
        pass
