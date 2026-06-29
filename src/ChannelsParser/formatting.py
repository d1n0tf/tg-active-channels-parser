from __future__ import annotations

import csv
import io
from datetime import datetime, timezone

from ChannelsParser.models import ChannelReport, FilterPreset, ScanRecord, SearchFilters


def format_filters(filters: SearchFilters) -> str:
    subs = _range(filters.min_subscribers, filters.max_subscribers)
    min_views = filters.min_avg_views if filters.min_avg_views is not None else "любые"
    return (
        "Текущие фильтры:\n"
        f"Подписчики: {subs}\n"
        f"Последний пост: <= {filters.max_last_post_days} дн.\n"
        f"Мин. активность: {filters.min_activity_score:.0f}/100\n"
        f"Мин. средние просмотры: {min_views}\n"
        f"Аудитория: {_audience(filters.audience_bias)}\n"
        f"Возраст: {filters.age_group}\n"
        f"Сортировка: {filters.sort_by}"
    )


def format_filter_presets(presets: list[FilterPreset]) -> str:
    if not presets:
        return (
            "Своих пресетов фильтров пока нет.\n\n"
            "Сохрани текущие фильтры кнопкой ниже или командой /savefilter Название."
        )

    chunks = ["Мои пресеты фильтров:"]
    for index, preset in enumerate(presets, start=1):
        chunks.append(f"{index}. {preset.title}\n{_filter_summary(preset.filters)}")
    return "\n\n".join(chunks)


def format_reports(reports: list[ChannelReport], *, limit: int = 10) -> str:
    if not reports:
        return (
            "Ничего не нашел по текущим фильтрам.\n"
            "Попробуй расширить подписчиков, поставить аудиторию 'любая' или дать больше ключевых слов."
        )

    chunks = ["Нашел активные каналы:"]
    for index, report in enumerate(reports[:limit], start=1):
        chunks.append(format_report(report, index=index))
    return "\n\n".join(chunks)


def format_scan_history(scans: list[ScanRecord]) -> str:
    if not scans:
        return "Истории пока нет. Запусти /find или /check."

    chunks = ["Последние сканы:"]
    for index, scan in enumerate(scans, start=1):
        queries = ", ".join(scan.queries[:3]) if scan.queries else "-"
        if len(scan.queries) > 3:
            queries += f" и еще {len(scan.queries) - 3}"
        chunks.append(
            f"{index}. {scan.mode} | {scan.status} | найдено {scan.total_reports}/{scan.total_candidates}\n"
            f"{_relative_time(scan.started_at)} | {queries}\n"
            f"id: {scan.scan_id[:8]}{_scan_error_suffix(scan.error)}"
        )
    return "\n\n".join(chunks)


def format_scan_done(scan_id: str, total_candidates: int, total_reports: int, errors: list[str]) -> str:
    text = f"Скан завершен: {total_reports} каналов из {total_candidates} кандидатов прошли фильтры.\nscan_id: {scan_id[:8]}"
    if errors:
        preview = "; ".join(errors[:3])
        text += f"\nЧасть каналов пропущена из-за ошибок Telegram: {preview}"
    return text


def format_report(report: ChannelReport, *, index: int | None = None) -> str:
    prefix = f"{index}. " if index else ""
    subscribers = _num(report.subscribers) if report.subscribers is not None else "?"
    views = _num(int(report.avg_views_recent))
    views_24h = _num(int(report.avg_views_24h))
    view_rate = f"{report.view_rate * 100:.1f}%" if report.view_rate is not None else "?"
    reaction_rate = f"{report.reaction_rate * 100:.1f}%" if report.reaction_rate is not None else "?"
    last_post = _relative_time(report.last_post_at)
    audience = _audience(report.audience.bias)
    confidence = f"{report.audience.confidence * 100:.0f}%"
    queries = ", ".join(report.matched_queries) or "-"

    return (
        f"{prefix}{report.title}\n"
        f"{report.display_link}\n"
        f"Score: {report.activity_score:.1f}/100 | Подписчики: {subscribers}\n"
        f"Посты: 24ч {report.post_count_24h}, 7д {report.post_count_7d} | последний: {last_post}\n"
        f"Просмотры: средн. {views}, за 24ч {views_24h} | VR: {view_rate}\n"
        f"Реакции: {report.avg_reactions_recent:.1f}/пост | комменты: {report.avg_comments_recent:.1f}/пост | RR: {reaction_rate}\n"
        f"Аудитория: {audience}, уверенность {confidence}, возраст: {report.audience.age_group}\n"
        f"Запросы: {queries}"
    )


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
        f"Просмотры: {min_views} | ЦА: {_audience(filters.audience_bias)} | "
        f"возраст: {filters.age_group} | sort: {filters.sort_by}"
    )


def _audience(value: str) -> str:
    if value == "female":
        return "преимущественно женская"
    if value == "male":
        return "преимущественно мужская"
    return "любая/не определена"


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
    return f"\nОшибка: {error[:160]}"
