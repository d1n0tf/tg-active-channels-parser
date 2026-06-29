from __future__ import annotations

from datetime import datetime, timezone
import sqlite3

import pytest

from ChannelsParser.audience import estimate_audience
from ChannelsParser.models import ChannelReport, SearchFilters
from ChannelsParser.storage import ChannelStorage


def test_storage_persists_filters_scans_and_reports(tmp_path) -> None:
    storage = ChannelStorage(tmp_path / "channels.sqlite3")
    storage.init()

    filters = SearchFilters(min_subscribers=100, max_subscribers=300, audience_bias="female")
    storage.save_user_filters(42, filters)
    assert storage.get_user_filters(42).max_subscribers == 300

    scan_id = "scan-1"
    storage.create_scan(scan_id, user_id=42, mode="search", queries=["семейный бюджет"], filters=filters)
    report = _sample_report()
    storage.save_reports(scan_id, [report])
    storage.save_reports(scan_id, [report])
    storage.finish_scan(scan_id, total_candidates=3, total_reports=1)

    storage.create_scan("empty-scan", user_id=42, mode="search", queries=["пусто"], filters=filters)
    storage.finish_scan("empty-scan", total_candidates=0, total_reports=0)

    scans = storage.list_scans(user_id=42)
    assert len(scans) == 2
    assert scans[0].scan_id == "empty-scan"
    assert scans[1].status == "done"
    assert scans[1].queries == ["семейный бюджет"]

    latest = storage.latest_reports(user_id=42)
    assert len(latest) == 1
    assert latest[0].title == "Деньги в декрете"
    assert storage.latest_scan_id(user_id=42) == "empty-scan"
    assert storage.latest_scan_id(user_id=42, require_reports=True) == scan_id


def test_storage_records_failed_scan(tmp_path) -> None:
    storage = ChannelStorage(tmp_path / "channels.sqlite3")
    storage.init()

    storage.create_scan("failed-1", user_id=42, mode="search", queries=["x"], filters=SearchFilters())
    storage.fail_scan("failed-1", error="boom", total_candidates=2)

    scan = storage.list_scans(user_id=42)[0]
    assert scan.status == "failed"
    assert scan.error == "boom"
    assert scan.total_candidates == 2


def test_storage_falls_back_from_corrupt_filter_json(tmp_path) -> None:
    db_path = tmp_path / "channels.sqlite3"
    storage = ChannelStorage(db_path)
    storage.init()

    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "INSERT INTO user_filters(user_id, filters, updated_at) VALUES (?, ?, ?)",
            (42, "{bad json", "2026-06-29T00:00:00+00:00"),
        )

    assert storage.get_user_filters(42) == SearchFilters()


def test_storage_manages_user_filter_presets(tmp_path) -> None:
    storage = ChannelStorage(tmp_path / "channels.sqlite3")
    storage.init()

    filters = SearchFilters(min_subscribers=100, max_subscribers=300, audience_bias="female")
    preset = storage.save_filter_preset(42, " Малые   женские ", filters)

    assert preset.title == "Малые женские"
    assert preset.filters.max_subscribers == 300
    assert storage.list_filter_presets(42)[0].preset_id == preset.preset_id

    updated_filters = SearchFilters(min_subscribers=300, max_subscribers=1000, min_avg_views=100, audience_bias="any")
    updated = storage.save_filter_preset(42, "Малые женские", updated_filters)

    assert updated.preset_id == preset.preset_id
    assert updated.filters.min_avg_views == 100
    assert storage.get_filter_preset(99, preset.preset_id) is None
    assert not storage.delete_filter_preset(99, preset.preset_id)
    assert storage.delete_filter_preset(42, preset.preset_id)
    assert storage.list_filter_presets(42) == []


def test_storage_rejects_empty_filter_preset_title(tmp_path) -> None:
    storage = ChannelStorage(tmp_path / "channels.sqlite3")
    storage.init()

    with pytest.raises(ValueError):
        storage.save_filter_preset(42, "   ", SearchFilters())


def test_storage_tolerates_corrupt_report_json_and_dates(tmp_path) -> None:
    db_path = tmp_path / "channels.sqlite3"
    storage = ChannelStorage(db_path)
    storage.init()
    storage.create_scan("scan-1", user_id=42, mode="search", queries=["x"], filters=SearchFilters())

    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            INSERT INTO channel_reports (
                scan_id, telegram_id, title, username, link, description, subscribers,
                last_post_at, post_count_24h, post_count_7d, avg_views_recent, avg_views_24h,
                avg_reactions_recent, avg_comments_recent, view_rate, reaction_rate,
                activity_score, audience_bias, audience_confidence, audience_age_group,
                audience_keywords, matched_queries, collected_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "scan-1",
                999,
                "Broken",
                "broken",
                "https://t.me/broken",
                "",
                10,
                "not-a-date",
                0,
                0,
                0,
                0,
                0,
                0,
                None,
                None,
                0,
                "any",
                0,
                "any",
                "{bad",
                "{bad",
                "not-a-date",
            ),
        )
        connection.execute("UPDATE scans SET status = 'done', total_reports = 1 WHERE scan_id = 'scan-1'")

    report = storage.latest_reports(scan_id="scan-1")[0]
    assert report.last_post_at is None
    assert report.matched_queries == []
    assert report.audience.matched_keywords == []


def _sample_report() -> ChannelReport:
    now = datetime(2026, 6, 29, tzinfo=timezone.utc)
    return ChannelReport(
        telegram_id=123,
        title="Деньги в декрете",
        username="money_mama",
        link="https://t.me/money_mama",
        description="Финансовая грамотность для мам",
        subscribers=250,
        last_post_at=now,
        post_count_24h=2,
        post_count_7d=8,
        avg_views_recent=100,
        avg_views_24h=120,
        avg_reactions_recent=10,
        avg_comments_recent=3,
        view_rate=0.4,
        reaction_rate=0.1,
        activity_score=82,
        audience=estimate_audience("Деньги в декрете", "Финансовая грамотность для мам"),
        matched_queries=["семейный бюджет"],
        collected_at=now,
    )
