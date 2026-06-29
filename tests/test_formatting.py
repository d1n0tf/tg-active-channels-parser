from __future__ import annotations

import csv
import io
from datetime import datetime, timedelta, timezone

from ChannelsParser.audience import estimate_audience
from ChannelsParser.formatting import format_filter_presets, format_scan_history, reports_to_csv
from ChannelsParser.models import ChannelReport, FilterPreset, ScanRecord, SearchFilters


def test_reports_to_csv_preserves_zero_values() -> None:
    payload = reports_to_csv([_zero_report()]).decode("utf-8-sig")
    row = next(csv.DictReader(io.StringIO(payload)))

    assert row["subscribers"] == "0"
    assert row["view_rate"] == "0"
    assert row["reaction_rate"] == "0"


def test_format_scan_history_shows_failed_error() -> None:
    scan = ScanRecord(
        scan_id="abcdef123456",
        user_id=42,
        mode="search",
        status="failed",
        queries=["семейный бюджет"],
        filters=SearchFilters(),
        total_candidates=2,
        total_reports=0,
        error="boom",
        started_at=datetime.now(timezone.utc) - timedelta(minutes=5),
    )

    assert "Ошибка: boom" in format_scan_history([scan])


def test_format_filter_presets_shows_compact_filter_summary() -> None:
    now = datetime.now(timezone.utc)
    preset = FilterPreset(
        preset_id=1,
        user_id=42,
        title="Малые женские",
        filters=SearchFilters(min_subscribers=100, max_subscribers=300, min_avg_views=50),
        created_at=now,
        updated_at=now,
    )

    text = format_filter_presets([preset])

    assert "Малые женские" in text
    assert "Подписчики: 100-300" in text
    assert "Просмотры: 50" in text


def _zero_report() -> ChannelReport:
    now = datetime(2026, 6, 29, tzinfo=timezone.utc)
    return ChannelReport(
        telegram_id=123,
        title="Zero channel",
        username="zero_channel",
        link="https://t.me/zero_channel",
        description="",
        subscribers=0,
        last_post_at=now,
        post_count_24h=0,
        post_count_7d=0,
        avg_views_recent=0,
        avg_views_24h=0,
        avg_reactions_recent=0,
        avg_comments_recent=0,
        view_rate=0,
        reaction_rate=0,
        activity_score=0,
        audience=estimate_audience("", ""),
        matched_queries=[],
        collected_at=now,
    )
