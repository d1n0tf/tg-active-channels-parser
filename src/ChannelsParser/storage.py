from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, fields
from datetime import datetime, timezone
from pathlib import Path

from ChannelsParser.models import AudienceEstimate, ChannelReport, FilterPreset, ScanRecord, SearchFilters


MAX_FILTER_PRESET_TITLE_LENGTH = 64


class ChannelStorage:
    def __init__(self, path: Path):
        self._path = path
        self._path.parent.mkdir(parents=True, exist_ok=True)

    def init(self) -> None:
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS scans (
                    scan_id TEXT PRIMARY KEY,
                    user_id INTEGER,
                    mode TEXT NOT NULL,
                    status TEXT NOT NULL,
                    queries TEXT NOT NULL,
                    filters TEXT NOT NULL,
                    total_candidates INTEGER NOT NULL DEFAULT 0,
                    total_reports INTEGER NOT NULL DEFAULT 0,
                    error TEXT,
                    started_at TEXT NOT NULL,
                    finished_at TEXT
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS user_filters (
                    user_id INTEGER PRIMARY KEY,
                    filters TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS user_filter_presets (
                    preset_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    title TEXT NOT NULL,
                    filters TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(user_id, title)
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS channel_reports (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    scan_id TEXT NOT NULL,
                    telegram_id INTEGER NOT NULL,
                    title TEXT NOT NULL,
                    username TEXT,
                    link TEXT,
                    description TEXT,
                    subscribers INTEGER,
                    last_post_at TEXT,
                    post_count_24h INTEGER NOT NULL,
                    post_count_7d INTEGER NOT NULL,
                    avg_views_recent REAL NOT NULL,
                    avg_views_24h REAL NOT NULL,
                    avg_reactions_recent REAL NOT NULL,
                    avg_comments_recent REAL NOT NULL,
                    view_rate REAL,
                    reaction_rate REAL,
                    activity_score REAL NOT NULL,
                    audience_bias TEXT NOT NULL,
                    audience_confidence REAL NOT NULL,
                    audience_age_group TEXT NOT NULL,
                    audience_keywords TEXT NOT NULL,
                    matched_queries TEXT NOT NULL,
                    collected_at TEXT NOT NULL,
                    owner_username TEXT,
                    owner_display_name TEXT
                )
                """
            )
            _ensure_column(connection, "channel_reports", "owner_username", "TEXT")
            _ensure_column(connection, "channel_reports", "owner_display_name", "TEXT")
            connection.execute(
                """
                DELETE FROM channel_reports
                WHERE id NOT IN (
                    SELECT MAX(id)
                    FROM channel_reports
                    GROUP BY scan_id, telegram_id
                )
                """
            )
            connection.execute("CREATE INDEX IF NOT EXISTS idx_scans_user_started ON scans(user_id, started_at DESC)")
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_channel_reports_scan ON channel_reports(scan_id, activity_score DESC)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_channel_reports_collected ON channel_reports(collected_at DESC)"
            )
            connection.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_channel_reports_scan_channel ON channel_reports(scan_id, telegram_id)"
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_user_filter_presets_user_updated
                ON user_filter_presets(user_id, updated_at DESC)
                """
            )

    def get_user_filters(self, user_id: int) -> SearchFilters:
        with self._connect() as connection:
            row = connection.execute("SELECT filters FROM user_filters WHERE user_id = ?", (user_id,)).fetchone()
        if not row:
            return SearchFilters()
        return _filters_from_json(row["filters"])

    def save_user_filters(self, user_id: int, filters: SearchFilters) -> None:
        now = _dt(datetime.now(timezone.utc))
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO user_filters(user_id, filters, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET filters = excluded.filters, updated_at = excluded.updated_at
                """,
                (user_id, _filters_json(filters), now),
            )

    def reset_user_filters(self, user_id: int) -> SearchFilters:
        filters = SearchFilters()
        self.save_user_filters(user_id, filters)
        return filters

    def save_filter_preset(self, user_id: int, title: str, filters: SearchFilters) -> FilterPreset:
        title = _normalize_filter_preset_title(title)
        now = _dt(datetime.now(timezone.utc))
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO user_filter_presets(user_id, title, filters, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(user_id, title) DO UPDATE SET
                    filters = excluded.filters,
                    updated_at = excluded.updated_at
                """,
                (user_id, title, _filters_json(filters), now, now),
            )
            row = connection.execute(
                """
                SELECT *
                FROM user_filter_presets
                WHERE user_id = ? AND title = ?
                """,
                (user_id, title),
            ).fetchone()
        if row is None:
            raise RuntimeError("Failed to save filter preset")
        return _row_filter_preset(row)

    def list_filter_presets(self, user_id: int, *, limit: int = 10) -> list[FilterPreset]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT *
                FROM user_filter_presets
                WHERE user_id = ?
                ORDER BY updated_at DESC
                LIMIT ?
                """,
                (user_id, limit),
            ).fetchall()
        return [_row_filter_preset(row) for row in rows]

    def get_filter_preset(self, user_id: int, preset_id: int) -> FilterPreset | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT *
                FROM user_filter_presets
                WHERE user_id = ? AND preset_id = ?
                """,
                (user_id, preset_id),
            ).fetchone()
        return _row_filter_preset(row) if row else None

    def delete_filter_preset(self, user_id: int, preset_id: int) -> bool:
        with self._connect() as connection:
            cursor = connection.execute(
                """
                DELETE FROM user_filter_presets
                WHERE user_id = ? AND preset_id = ?
                """,
                (user_id, preset_id),
            )
        return cursor.rowcount > 0

    def create_scan(
        self,
        scan_id: str,
        *,
        user_id: int | None,
        mode: str,
        queries: list[str],
        filters: SearchFilters,
    ) -> None:
        now = _dt(datetime.now(timezone.utc))
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO scans(
                    scan_id, user_id, mode, status, queries, filters,
                    total_candidates, total_reports, started_at
                )
                VALUES (?, ?, ?, 'running', ?, ?, 0, 0, ?)
                """,
                (scan_id, user_id, mode, json.dumps(queries, ensure_ascii=False), _filters_json(filters), now),
            )

    def finish_scan(self, scan_id: str, *, total_candidates: int, total_reports: int) -> None:
        now = _dt(datetime.now(timezone.utc))
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE scans
                SET status = 'done', total_candidates = ?, total_reports = ?, finished_at = ?, error = NULL
                WHERE scan_id = ?
                """,
                (total_candidates, total_reports, now, scan_id),
            )

    def fail_scan(self, scan_id: str, *, error: str, total_candidates: int = 0, total_reports: int = 0) -> None:
        now = _dt(datetime.now(timezone.utc))
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE scans
                SET status = 'failed', total_candidates = ?, total_reports = ?, error = ?, finished_at = ?
                WHERE scan_id = ?
                """,
                (total_candidates, total_reports, error[:1000], now, scan_id),
            )

    def save_reports(self, scan_id: str, reports: list[ChannelReport]) -> None:
        if not reports:
            return
        with self._connect() as connection:
            connection.executemany(
                """
                INSERT OR REPLACE INTO channel_reports (
                    scan_id, telegram_id, title, username, link, description, subscribers,
                    last_post_at, post_count_24h, post_count_7d, avg_views_recent, avg_views_24h,
                    avg_reactions_recent, avg_comments_recent, view_rate, reaction_rate,
                    activity_score, audience_bias, audience_confidence, audience_age_group,
                    audience_keywords, matched_queries, collected_at,
                    owner_username, owner_display_name
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [_report_row(scan_id, report) for report in reports],
            )

    def latest_reports(
        self,
        *,
        scan_id: str | None = None,
        user_id: int | None = None,
        limit: int = 20,
    ) -> list[ChannelReport]:
        query = "SELECT * FROM channel_reports"
        params: list[object] = []
        if scan_id:
            query += " WHERE scan_id = ?"
            params.append(scan_id)
        elif user_id is not None:
            query += """
                WHERE scan_id = (
                    SELECT scan_id FROM scans
                    WHERE user_id = ? AND status = 'done' AND total_reports > 0
                    ORDER BY started_at DESC
                    LIMIT 1
                )
            """
            params.append(user_id)
        query += " ORDER BY activity_score DESC, avg_views_recent DESC LIMIT ?"
        params.append(limit)

        with self._connect() as connection:
            rows = connection.execute(query, params).fetchall()
        return [_row_report(row) for row in rows]

    def latest_scan_id(self, *, user_id: int, only_done: bool = True, require_reports: bool = False) -> str | None:
        query = "SELECT scan_id FROM scans WHERE user_id = ?"
        params: list[object] = [user_id]
        if only_done:
            query += " AND status = 'done'"
        if require_reports:
            query += " AND total_reports > 0"
        query += " ORDER BY started_at DESC LIMIT 1"
        with self._connect() as connection:
            row = connection.execute(query, params).fetchone()
        return row["scan_id"] if row else None

    def list_scans(self, *, user_id: int | None = None, limit: int = 10) -> list[ScanRecord]:
        query = "SELECT * FROM scans"
        params: list[object] = []
        if user_id is not None:
            query += " WHERE user_id = ?"
            params.append(user_id)
        query += " ORDER BY started_at DESC LIMIT ?"
        params.append(limit)

        with self._connect() as connection:
            rows = connection.execute(query, params).fetchall()
        return [_row_scan(row) for row in rows]

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._path, timeout=30)
        connection.row_factory = sqlite3.Row
        return connection


def _report_row(scan_id: str, report: ChannelReport) -> tuple[object, ...]:
    collected_at = report.collected_at or datetime.now(timezone.utc)
    return (
        scan_id,
        report.telegram_id,
        report.title,
        report.username,
        report.link,
        report.description,
        report.subscribers,
        _dt(report.last_post_at),
        report.post_count_24h,
        report.post_count_7d,
        report.avg_views_recent,
        report.avg_views_24h,
        report.avg_reactions_recent,
        report.avg_comments_recent,
        report.view_rate,
        report.reaction_rate,
        report.activity_score,
        report.audience.bias,
        report.audience.confidence,
        report.audience.age_group,
        json.dumps(report.audience.matched_keywords, ensure_ascii=False),
        json.dumps(report.matched_queries, ensure_ascii=False),
        _dt(collected_at),
        report.owner_username,
        report.owner_display_name,
    )


def _row_scan(row: sqlite3.Row) -> ScanRecord:
    return ScanRecord(
        scan_id=row["scan_id"],
        user_id=row["user_id"],
        mode=row["mode"],
        status=row["status"],
        queries=_json_list(row["queries"]),
        filters=_filters_from_json(row["filters"]),
        total_candidates=row["total_candidates"],
        total_reports=row["total_reports"],
        error=row["error"],
        started_at=_parse_dt(row["started_at"]) or datetime.now(timezone.utc),
        finished_at=_parse_dt(row["finished_at"]),
    )


def _row_filter_preset(row: sqlite3.Row) -> FilterPreset:
    created_at = _parse_dt(row["created_at"]) or datetime.now(timezone.utc)
    updated_at = _parse_dt(row["updated_at"]) or created_at
    return FilterPreset(
        preset_id=row["preset_id"],
        user_id=row["user_id"],
        title=row["title"],
        filters=_filters_from_json(row["filters"]),
        created_at=created_at,
        updated_at=updated_at,
    )


def _row_report(row: sqlite3.Row) -> ChannelReport:
    audience = AudienceEstimate(
        bias=row["audience_bias"],
        confidence=row["audience_confidence"],
        age_group=row["audience_age_group"],
        matched_keywords=_json_list(row["audience_keywords"]),
    )
    return ChannelReport(
        telegram_id=row["telegram_id"],
        title=row["title"],
        username=row["username"],
        link=row["link"],
        description=row["description"] or "",
        subscribers=row["subscribers"],
        last_post_at=_parse_dt(row["last_post_at"]),
        post_count_24h=row["post_count_24h"],
        post_count_7d=row["post_count_7d"],
        avg_views_recent=row["avg_views_recent"],
        avg_views_24h=row["avg_views_24h"],
        avg_reactions_recent=row["avg_reactions_recent"],
        avg_comments_recent=row["avg_comments_recent"],
        view_rate=row["view_rate"],
        reaction_rate=row["reaction_rate"],
        activity_score=row["activity_score"],
        audience=audience,
        matched_queries=_json_list(row["matched_queries"]),
        collected_at=_parse_dt(row["collected_at"]),
        owner_username=row["owner_username"],
        owner_display_name=row["owner_display_name"],
    )


def _dt(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _filters_json(filters: SearchFilters) -> str:
    return json.dumps(asdict(filters), ensure_ascii=False)


def _filters_from_json(value: str) -> SearchFilters:
    try:
        raw = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return SearchFilters()
    if not isinstance(raw, dict):
        return SearchFilters()

    allowed = {field.name for field in fields(SearchFilters)}
    defaults = asdict(SearchFilters())
    defaults.update({key: raw[key] for key in raw.keys() & allowed})
    return SearchFilters(**defaults)


def _json_list(value: str | None) -> list[str]:
    if not value:
        return []
    try:
        raw = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return []
    if not isinstance(raw, list):
        return []
    return [str(item) for item in raw]


def _ensure_column(connection: sqlite3.Connection, table: str, column: str, declaration: str) -> None:
    columns = {row["name"] for row in connection.execute(f"PRAGMA table_info({table})")}
    if column not in columns:
        connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")


def _normalize_filter_preset_title(title: str) -> str:
    normalized = " ".join(title.split())
    if not normalized:
        raise ValueError("Укажи название пресета: /savefilter Малые женские каналы")
    if len(normalized) > MAX_FILTER_PRESET_TITLE_LENGTH:
        normalized = normalized[:MAX_FILTER_PRESET_TITLE_LENGTH].rstrip()
    return normalized
