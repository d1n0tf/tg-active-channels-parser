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
    assert latest[0].owner_username == "owner_user"
    assert latest[0].owner_display_name == "Owner Name"
    assert latest[0].title == "Деньги в декрете"
    assert storage.latest_scan_id(user_id=42) == "empty-scan"
    assert storage.latest_scan_id(user_id=42, require_reports=True) == scan_id


def test_storage_allowed_users_grant_revoke_list(tmp_path) -> None:
    storage = ChannelStorage(tmp_path / "channels.sqlite3")
    storage.init()

    assert storage.is_user_allowed(100) is False
    assert storage.grant_access(100, granted_by=1) == "created"
    assert storage.grant_access(100, granted_by=1) == "updated"
    assert storage.is_user_allowed(100) is True
    assert storage.list_allowed_users()[0][0] == 100
    assert storage.list_allowed_users()[0][3] is None  # permanent
    assert storage.revoke_access(100) is True
    assert storage.is_user_allowed(100) is False
    assert storage.revoke_access(100) is False


def test_storage_allowed_users_supports_ttl_days(tmp_path) -> None:
    from datetime import timedelta, timezone

    storage = ChannelStorage(tmp_path / "channels.sqlite3")
    storage.init()

    assert storage.grant_access(200, granted_by=1, days=7) == "created"
    assert storage.is_user_allowed(200) is True
    user_id, granted_by, created_at, expires_at = storage.list_allowed_users()[0]
    assert user_id == 200
    assert expires_at is not None
    assert expires_at > created_at
    assert (expires_at - created_at) <= timedelta(days=7, seconds=2)

    # Force-expire row and ensure access is revoked on check.
    past = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    import sqlite3

    with sqlite3.connect(tmp_path / "channels.sqlite3") as connection:
        connection.execute("UPDATE allowed_users SET expires_at = ? WHERE user_id = 200", (past,))

    assert storage.is_user_allowed(200) is False


def test_storage_renewal_reminder_window(tmp_path) -> None:
    from datetime import timedelta, timezone
    import sqlite3

    storage = ChannelStorage(tmp_path / "channels.sqlite3")
    storage.init()
    storage.grant_access(300, granted_by=1, days=1)

    soon = (datetime.now(timezone.utc) + timedelta(hours=6)).isoformat()
    with sqlite3.connect(tmp_path / "channels.sqlite3") as connection:
        connection.execute(
            "UPDATE allowed_users SET expires_at = ?, renewal_notified_at = NULL WHERE user_id = 300",
            (soon,),
        )

    pending = storage.list_users_needing_renewal_reminder(within_hours=12)
    assert len(pending) == 1
    assert pending[0][0] == 300

    storage.mark_renewal_notified(300)
    assert storage.list_users_needing_renewal_reminder(within_hours=12) == []

    # Renew clears notification flag
    storage.grant_access(300, granted_by=1, days=1)
    soon2 = (datetime.now(timezone.utc) + timedelta(hours=3)).isoformat()
    with sqlite3.connect(tmp_path / "channels.sqlite3") as connection:
        connection.execute(
            "UPDATE allowed_users SET expires_at = ?, renewal_notified_at = NULL WHERE user_id = 300",
            (soon2,),
        )
    assert len(storage.list_users_needing_renewal_reminder(within_hours=12)) == 1


def test_storage_delete_scan_removes_reports(tmp_path) -> None:
    storage = ChannelStorage(tmp_path / "channels.sqlite3")
    storage.init()
    storage.create_scan("scan-del", user_id=42, mode="discover", queries=["@src"], filters=SearchFilters())
    storage.save_reports("scan-del", [_sample_report()])
    storage.finish_scan("scan-del", total_candidates=1, total_reports=1)

    assert storage.count_reports("scan-del") == 1
    assert storage.delete_scan("scan-del", user_id=42) is True
    assert storage.get_scan("scan-del", user_id=42) is None
    assert storage.count_reports("scan-del") == 0
    assert storage.delete_scan("scan-del", user_id=42) is False


def test_storage_reports_page_and_ordinal(tmp_path) -> None:
    storage = ChannelStorage(tmp_path / "channels.sqlite3")
    storage.init()
    storage.create_scan("a", user_id=7, mode="search", queries=["q"], filters=SearchFilters())
    storage.create_scan("b", user_id=7, mode="discover", queries=["@x"], filters=SearchFilters())
    storage.save_reports("b", [_sample_report(), _sample_report_alt()])
    storage.finish_scan("b", total_candidates=2, total_reports=2)

    assert storage.scan_ordinal("b") == 2
    page = storage.reports_page("b", offset=0, limit=1)
    assert len(page) == 1
    assert storage.count_reports("b") == 2


def test_storage_records_failed_scan(tmp_path) -> None:
    storage = ChannelStorage(tmp_path / "channels.sqlite3")
    storage.init()

    storage.create_scan("failed-1", user_id=42, mode="search", queries=["x"], filters=SearchFilters())
    storage.fail_scan("failed-1", error="boom", total_candidates=2)

    scan = storage.list_scans(user_id=42)[0]
    assert scan.status == "failed"
    assert scan.error == "boom"
    assert scan.total_candidates == 2


def test_storage_recovers_interrupted_scans_on_startup(tmp_path) -> None:
    storage = ChannelStorage(tmp_path / "channels.sqlite3")
    storage.init()
    storage.create_scan("interrupted", user_id=42, mode="search", queries=["x"], filters=SearchFilters())

    assert storage.recover_interrupted_scans() == 1

    scan = storage.get_scan("interrupted", user_id=42)
    assert scan is not None
    assert scan.status == "failed"
    assert "перезапуском" in (scan.error or "")


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
        owner_username="owner_user",
        owner_display_name="Owner Name",
    )


def _sample_report_alt() -> ChannelReport:
    report = _sample_report()
    report.telegram_id = 456
    report.username = "other_channel"
    report.title = "Другой канал"
    report.link = "https://t.me/other_channel"
    return report
