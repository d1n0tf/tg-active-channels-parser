from __future__ import annotations

import csv
import io
from datetime import datetime, timedelta, timezone

from ChannelsParser.audience import estimate_audience
from ChannelsParser.formatting import (
    format_compact_channel_entry,
    format_compact_results_page,
    format_discovery_stats,
    format_filter_presets,
    format_report,
    format_scan_history,
    reports_to_csv,
)
from ChannelsParser.models import ChannelReport, FilterPreset, ScanRecord, SearchFilters, SearchRunResult


def test_reports_to_csv_preserves_zero_values() -> None:
    payload = reports_to_csv([_zero_report()]).decode("utf-8-sig")
    row = next(csv.DictReader(io.StringIO(payload)))

    assert row["subscribers"] == "0"
    assert row["view_rate"] == "0"
    assert row["reaction_rate"] == "0"


def test_format_report_and_csv_include_owner_profile() -> None:
    report = _zero_report()
    report.owner_username = "owner_user"
    report.owner_display_name = "Owner Name"

    text = format_report(report)
    payload = reports_to_csv([report]).decode("utf-8-sig")
    row = next(csv.DictReader(io.StringIO(payload)))

    assert "Владелец/профиль: @owner_user (Owner Name)" in text
    assert row["owner_username"] == "owner_user"
    assert row["owner_display_name"] == "Owner Name"


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

    text = format_scan_history([scan], ordinals={scan.scan_id: 7})
    assert "Ошибка: boom" in text
    assert "#7" in text
    assert "семейный бюджет" in text


def test_scan_source_label_prefers_channel_for_discover() -> None:
    from ChannelsParser.formatting import scan_source_label

    scan = ScanRecord(
        scan_id="x",
        user_id=1,
        mode="discover",
        status="done",
        queries=["@donor", "posts:200", "comments:on"],
        filters=SearchFilters(),
        total_candidates=10,
        total_reports=3,
        error=None,
        started_at=datetime.now(timezone.utc),
    )
    assert scan_source_label(scan) == "@donor"


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


def test_format_compact_results_page_matches_reference_layout() -> None:
    report = _zero_report()
    report.owner_username = "kraitts"
    report.owner_display_name = "Dari_krr_"
    report.username = "kraitts"
    report.subscribers = 146

    text = format_compact_results_page(
        ordinal=148,
        source_label="@lunarnii",
        total_reports=24,
        page=1,
        page_size=8,
        reports=[report],
    )

    assert "ЗАПИСЬ #148" in text
    assert "Источник: @lunarnii" in text
    assert "Найдено всего: 24 каналов" in text
    assert "Страница: 1 из 3" in text
    assert "Dari_krr_ (@kraitts)" in text or "@kraitts" in text
    assert "@kraitts" in text
    assert "146" in text
    assert "Score:" not in text


def test_format_compact_channel_entry_without_owner() -> None:
    report = _zero_report()
    text = format_compact_channel_entry(report)
    assert "Канал:" in text
    assert "подп." in text


def test_format_discovery_stats_shows_funnel() -> None:
    text = format_discovery_stats(
        SearchRunResult(
            reports=[],
            total_candidates=3,
            inspected_channels=2,
            stats={
                "posts_seen": 10,
                "posts_with_replies": 4,
                "discussion_posts": 3,
                "direct_reply_invalid": 2,
                "discussion_missing": 1,
                "comment_fetch_errors": 1,
                "comments_seen": 120,
                "profiles_seen": 50,
                "profiles_skipped_by_limit": 7,
                "comment_refs": 1,
                "bio_refs": 1,
                "personal_channel_refs": 1,
                "gift_profiles_checked": 3,
                "gift_refs": 2,
                "gift_fetch_errors": 1,
                "channel_commenter_refs": 0,
            },
        )
    )

    assert "Комментарии просмотрены: 120" in text
    assert "discussion найден: 3" in text
    assert "discussion не найден: 1" in text
    assert "Кандидаты из personal channel: 1" in text
    assert "Подарки проверены: 3, кандидаты из подарков: 2" in text
    assert "Всего кандидатов: 3, проверено: 2" in text


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
