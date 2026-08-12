from __future__ import annotations

import csv
import io
from datetime import datetime, timezone

from ChannelsParser.models import ChannelReport, FilterPreset, ScanRecord, SearchFilters, SearchRunResult
from ChannelsParser.user_errors import scan_errors_text, stored_scan_error_text


def format_filters(filters: SearchFilters) -> str:
    subs = _range(filters.min_subscribers, filters.max_subscribers)
    min_views = filters.min_avg_views if filters.min_avg_views is not None else "любые"
    return (
        "Текущие фильтры:\n"
        f"• Подписчики: {subs}\n"
        f"• Последний пост: <= {filters.max_last_post_days} дн.\n"
        f"• Мин. score: {filters.min_activity_score:.0f}/100\n"
        f"• Мин. средние просмотры: {min_views}\n"
        f"• Тип канала: {_channel_kind(filters.channel_kind)}\n"
        f"• Аудитория: {_audience(filters.audience_bias)}\n"
        f"• Возраст: {filters.age_group}\n"
        f"• Сортировка: {filters.sort_by}"
    )


def format_filter_presets(presets: list[FilterPreset]) -> str:
    if not presets:
        return (
            "💾 Своих пресетов фильтров пока нет.\n\n"
            "Сохрани текущие фильтры кнопкой ниже или командой:\n"
            "/savefilter Название"
        )

    chunks = [f"💾 Мои пресеты фильтров ({len(presets)}):"]
    for index, preset in enumerate(presets, start=1):
        chunks.append(f"{index}. {preset.title}\n{_filter_summary(preset.filters)}")
    return "\n\n".join(chunks)


def format_reports(reports: list[ChannelReport], *, limit: int = 10) -> str:
    if not reports:
        return (
            "😕 Ничего не нашёл по текущим фильтрам.\n\n"
            "Что попробовать:\n"
            "• расширить диапазон подписчиков\n"
            "• поставить ЦА «любая»\n"
            "• снизить минимальный score\n"
            "• добавить больше ключевых слов"
        )

    shown = reports[:limit]
    extra = len(reports) - len(shown)
    header = f"✅ Нашёл активные каналы: {len(reports)}"
    if extra > 0:
        header += f" (показываю топ {len(shown)})"
    chunks = [header]
    for index, report in enumerate(shown, start=1):
        chunks.append(format_report(report, index=index))
    return "\n\n".join(chunks)


def format_scan_history(
    scans: list[ScanRecord],
    *,
    ordinals: dict[str, int] | None = None,
) -> str:
    if not scans:
        return (
            "🗂 Истории пока нет.\n\n"
            "Запусти Discovery / поиск / check — сканы появятся здесь.\n"
            "Нажми кнопку записи, чтобы открыть её результат."
        )

    chunks = [
        "🗂 История сканов",
        "Жми кнопку ниже, чтобы открыть результат записи.",
        "",
    ]
    for index, scan in enumerate(scans, start=1):
        ordinal = (ordinals or {}).get(scan.scan_id, index)
        source = scan_source_label(scan)
        chunks.append(
            f"#{ordinal} · {_mode_label(scan.mode)} · {_status_label(scan.status)}\n"
            f"🎯 {source}\n"
            f"📊 {scan.total_reports} в выдаче / {scan.total_candidates} канд. · "
            f"{_relative_time(scan.started_at)}"
            f"{_scan_error_suffix(scan.error)}"
        )
    return "\n\n".join(chunks)


def scan_source_label(scan: ScanRecord) -> str:
    """Human label for history: channel @ or first query."""
    if not scan.queries:
        return "—"
    first = scan.queries[0]
    if scan.mode in {"discover", "audit"}:
        return first
    # search: show first queries compact
    bits = [q for q in scan.queries[:2] if not q.startswith(("posts:", "comments:", "profile:", "gifts:", "subs:"))]
    if not bits:
        bits = scan.queries[:2]
    text = ", ".join(bits)
    if len(scan.queries) > 2:
        text += "…"
    return text if len(text) <= 48 else text[:45] + "…"


def format_scan_done(scan_id: str, total_candidates: int, total_reports: int, errors: list[str]) -> str:
    text = (
        f"✅ Скан завершён: {total_reports} каналов из {total_candidates} кандидатов "
        f"прошли фильтры.\nscan_id: {scan_id[:8]}"
    )
    if errors:
        preview = scan_errors_text(errors)
        text += f"\n⚠️ Часть каналов пропущена из-за ошибок Telegram: {preview}"
    return text


RESULTS_PAGE_SIZE = 8


def format_compact_results_page(
    *,
    ordinal: int,
    source_label: str,
    total_reports: int,
    page: int,
    page_size: int = RESULTS_PAGE_SIZE,
    reports: list[ChannelReport],
) -> str:
    """Compact discovery/search results layout (paginated list)."""
    total_pages = max(1, (total_reports + page_size - 1) // page_size) if total_reports else 1
    page = max(1, min(page, total_pages))
    lines = [
        f"📋 ЗАПИСЬ #{ordinal}",
        f"📣 Источник: {source_label}",
        f"👥 Найдено всего: {total_reports} каналов",
        f"📄 Страница: {page} из {total_pages}",
        "",
    ]
    if not reports:
        lines.append("На этой странице пусто.")
        return "\n".join(lines)

    for report in reports:
        lines.append(format_compact_channel_entry(report))
        lines.append("")
    return "\n".join(lines).rstrip()


def format_compact_channel_entry(report: ChannelReport) -> str:
    owner = report.owner_label
    if owner:
        owner_line = f"👤 {owner}"
    else:
        owner_line = f"👤 {report.title}"
    channel = report.display_link
    if report.username:
        channel = f"@{report.username}"
    subs = _num(report.subscribers) if report.subscribers is not None else "?"
    return f"{owner_line}\n📣 Канал: {channel} ({subs} подп.)"


def source_label_from_scan(scan: ScanRecord) -> str:
    if not scan.queries:
        return "—"
    first = scan.queries[0]
    if first.startswith("posts:") or ":" in first and first.split(":", 1)[0] in {
        "comments",
        "profile",
        "gifts",
        "subs",
    }:
        return first
    return first


def format_discovery_stats(result: SearchRunResult) -> str:
    stats = result.stats
    if not stats:
        return ""
    return (
        "📊 Воронка discovery:\n"
        f"Посты: {stats.get('posts_seen', 0)}, с reply-счетчиком: {stats.get('posts_with_replies', 0)}, "
        f"discussion найден: {stats.get('discussion_posts', 0)}\n"
        f"Прямой reply_to недоступен: {stats.get('direct_reply_invalid', 0)}, "
        f"discussion не найден: {stats.get('discussion_missing', 0)}\n"
        f"Комментарии просмотрены: {stats.get('comments_seen', 0)}\n"
        f"Ошибки чтения комментариев: {stats.get('comment_fetch_errors', 0)}\n"
        f"Профили просмотрены: {stats.get('profiles_seen', 0)}, "
        f"пропущены по лимиту: {stats.get('profiles_skipped_by_limit', 0)}\n"
        f"Кандидаты из текста/bio: {stats.get('comment_refs', 0) + stats.get('bio_refs', 0)}\n"
        f"Кандидаты из personal channel: {stats.get('personal_channel_refs', 0)}\n"
        f"Подарки проверены: {stats.get('gift_profiles_checked', 0)}, "
        f"кандидаты из подарков: {stats.get('gift_refs', 0)}, "
        f"ошибки подарков: {stats.get('gift_fetch_errors', 0)}\n"
        f"Кандидаты от имени каналов: {stats.get('channel_commenter_refs', 0)}\n"
        f"Всего кандидатов: {result.total_candidates}, проверено: {result.inspected_channels}, "
        f"пропущено inspect: {result.skipped_channels}, "
        f"отсеяно фильтрами: {stats.get('filter_dropped', 0)}, "
        f"в выдаче: {stats.get('filter_passed', len(result.reports))}"
    )


def format_report(report: ChannelReport, *, index: int | None = None) -> str:
    medals = {1: "🥇", 2: "🥈", 3: "🥉"}
    if index is None:
        prefix = ""
    elif index in medals:
        prefix = f"{medals[index]} "
    else:
        prefix = f"{index}. "
    subscribers = _num(report.subscribers) if report.subscribers is not None else "?"
    views = _num(int(report.avg_views_recent))
    views_24h = _num(int(report.avg_views_24h))
    view_rate = f"{report.view_rate * 100:.1f}%" if report.view_rate is not None else "?"
    reaction_rate = f"{report.reaction_rate * 100:.1f}%" if report.reaction_rate is not None else "?"
    last_post = _relative_time(report.last_post_at)
    audience = _audience(report.audience.bias)
    confidence = f"{report.audience.confidence * 100:.0f}%"
    queries = ", ".join(report.matched_queries) or "—"
    owner = report.owner_label
    owner_line = f"👤 Владелец/профиль: {owner}\n" if owner else ""
    score_bar = _score_bar(report.activity_score)

    return (
        f"{prefix}{report.title}\n"
        f"🔗 {report.display_link}\n"
        f"{owner_line}"
        f"⚡ Score: {report.activity_score:.1f}/100 {score_bar}\n"
        f"👥 Подписчики: {subscribers}\n"
        f"📝 Посты: 24ч {report.post_count_24h} · 7д {report.post_count_7d} · последний {last_post}\n"
        f"👁 Просмотры: средн. {views} · 24ч {views_24h} · VR {view_rate}\n"
        f"❤️ Реакции: {report.avg_reactions_recent:.1f}/пост · 💬 {report.avg_comments_recent:.1f}/пост · RR {reaction_rate}\n"
        f"🎯 ЦА: {audience} ({confidence}) · возраст {report.audience.age_group}\n"
        f"🏷 Запросы: {queries}"
    )


def _score_bar(score: float, *, width: int = 10) -> str:
    filled = min(width, max(0, round(width * score / 100)))
    return f"[{'█' * filled}{'░' * (width - filled)}]"


def _mode_label(mode: str) -> str:
    return {
        "search": "поиск",
        "discover": "discovery",
        "audit": "check",
    }.get(mode, mode)


def _status_label(status: str) -> str:
    return {
        "running": "⏳ идёт",
        "done": "✅ готово",
        "failed": "❌ ошибка",
    }.get(status, status)


def reports_to_csv(reports: list[ChannelReport]) -> bytes:
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(
        [
            "title",
            "link",
            "username",
            "subscribers",
            "activity_score",
            "last_post_at",
            "post_count_24h",
            "post_count_7d",
            "avg_views_recent",
            "avg_views_24h",
            "view_rate",
            "avg_reactions_recent",
            "avg_comments_recent",
            "reaction_rate",
            "audience_bias",
            "audience_confidence",
            "age_group",
            "matched_queries",
            "owner_username",
            "owner_display_name",
            "description",
            "collected_at",
        ]
    )
    for report in reports:
        writer.writerow(
            [
                report.title,
                report.display_link,
                report.username or "",
                _csv_optional(report.subscribers),
                report.activity_score,
                _iso(report.last_post_at),
                report.post_count_24h,
                report.post_count_7d,
                report.avg_views_recent,
                report.avg_views_24h,
                _csv_optional(report.view_rate),
                report.avg_reactions_recent,
                report.avg_comments_recent,
                _csv_optional(report.reaction_rate),
                report.audience.bias,
                report.audience.confidence,
                report.audience.age_group,
                ", ".join(report.matched_queries),
                report.owner_username or "",
                report.owner_display_name or "",
                report.description,
                _iso(report.collected_at),
            ]
        )
    return buffer.getvalue().encode("utf-8-sig")


def _range(min_value: int | None, max_value: int | None) -> str:
    if min_value is None and max_value is None:
        return "любые"
    if min_value is None:
        return f"до {_num(max_value)}"
    if max_value is None:
        return f"от {_num(min_value)}"
    return f"{_num(min_value)}-{_num(max_value)}"


def _num(value: int | None) -> str:
    if value is None:
        return "?"
    return f"{value:,}".replace(",", " ")


def _filter_summary(filters: SearchFilters) -> str:
    min_views = filters.min_avg_views if filters.min_avg_views is not None else "любые"
    return (
        f"Подписчики: {_range(filters.min_subscribers, filters.max_subscribers)} | "
        f"пост <= {filters.max_last_post_days} дн. | score >= {filters.min_activity_score:.0f}\n"
        f"Просмотры: {min_views} | тип: {_channel_kind(filters.channel_kind)} | "
        f"ЦА: {_audience(filters.audience_bias)} | возраст: {filters.age_group} | sort: {filters.sort_by}"
    )


def _audience(value: str) -> str:
    if value == "female":
        return "преимущественно женская"
    if value == "male":
        return "преимущественно мужская"
    return "любая/не определена"


def _channel_kind(value: str) -> str:
    if value == "thematic":
        return "тематические/не личные"
    if value == "commercial":
        return "коммерческие"
    return "любые"


def _relative_time(value: datetime | None) -> str:
    if value is None:
        return "нет данных"
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    delta = datetime.now(timezone.utc) - value.astimezone(timezone.utc)
    if delta.total_seconds() < -60:
        return "в будущем"
    if delta.total_seconds() < 3600:
        return f"{max(1, int(delta.total_seconds() // 60))} мин. назад"
    if delta.days < 1:
        return f"{int(delta.total_seconds() // 3600)} ч. назад"
    return f"{delta.days} дн. назад"


def _iso(value: datetime | None) -> str:
    if value is None:
        return ""
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def _csv_optional(value: object | None) -> object:
    return "" if value is None else value


def _scan_error_suffix(error: str | None) -> str:
    if not error:
        return ""
    return f"\nОшибка: {stored_scan_error_text(error)}"
