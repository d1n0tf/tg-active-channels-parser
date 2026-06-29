from __future__ import annotations

import pytest

from ChannelsParser.cli import _validate_search_args, parse_cli_queries
from ChannelsParser.commands import apply_set_command, parse_queries
from ChannelsParser.models import SearchFilters


def test_parse_queries_splits_commas_semicolons_and_lines() -> None:
    assert parse_queries("семейный бюджет, финансы для женщин\nдекрет; маркетплейсы") == [
        "семейный бюджет",
        "финансы для женщин",
        "декрет",
        "маркетплейсы",
    ]


def test_parse_cli_queries_keeps_space_arguments_separate() -> None:
    assert parse_cli_queries(["семейный бюджет", "финансы для женщин, деньги в декрете"]) == [
        "семейный бюджет",
        "финансы для женщин",
        "деньги в декрете",
    ]


def test_apply_set_command_updates_subscribers_and_views() -> None:
    filters, _ = apply_set_command(SearchFilters(), "subs 100 300")
    assert filters.min_subscribers == 100
    assert filters.max_subscribers == 300

    filters, _ = apply_set_command(filters, "views any")
    assert filters.min_avg_views is None


def test_apply_set_command_rejects_bad_score() -> None:
    with pytest.raises(ValueError):
        apply_set_command(SearchFilters(), "score 150")


def test_cli_validation_rejects_invalid_ranges() -> None:
    class Args:
        subs_min = 500
        subs_max = 100
        days = 7
        views_min = None
        score_min = 35
        limit = 10

    with pytest.raises(ValueError):
        _validate_search_args(Args())  # type: ignore
