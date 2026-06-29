from __future__ import annotations

from datetime import datetime, timezone

from ChannelsParser.models import ChannelReport, SearchFilters


def activity_score(
    *,
    last_post_at: datetime | None,
    post_count_24h: int,
    post_count_7d: int,
    avg_views_recent: float,
    avg_reactions_recent: float,
    avg_comments_recent: float,
    subscribers: int | None,
    now: datetime | None = None,
) -> float:
    now = now or datetime.now(timezone.utc)

    if last_post_at is None:
        recency_score = 0.0
    else:
        age_hours = max((now - _aware(last_post_at)).total_seconds() / 3600, 0)
        recency_score = max(0.0, 1 - age_hours / (24 * 7))

    frequency_score = min(post_count_7d / 7, 1.0) * 0.75 + min(post_count_24h / 3, 1.0) * 0.25

    if subscribers and subscribers > 0:
        view_rate = avg_views_recent / subscribers
        view_score = min(view_rate / 0.25, 1.0)
    else:
        view_score = min(avg_views_recent / 500, 1.0)

    reaction_score = min(avg_reactions_recent / 20, 1.0)
    comments_score = min(avg_comments_recent / 8, 1.0)

    score = (
        recency_score * 35
        + frequency_score * 20
        + view_score * 25
        + reaction_score * 10
        + comments_score * 10
    )
    return round(score, 1)


def matches_filters(report: ChannelReport, filters: SearchFilters, now: datetime | None = None) -> bool:
    now = now or datetime.now(timezone.utc)

    if filters.min_subscribers is not None and (report.subscribers is None or report.subscribers < filters.min_subscribers):
        return False
    if filters.max_subscribers is not None and (report.subscribers is None or report.subscribers > filters.max_subscribers):
        return False
    if report.last_post_at is None:
        return False

    days_since_last_post = (now - _aware(report.last_post_at)).total_seconds() / 86400
    if days_since_last_post > filters.max_last_post_days:
        return False
    if report.activity_score < filters.min_activity_score:
        return False
    if filters.min_avg_views is not None and report.avg_views_recent < filters.min_avg_views:
        return False
    if filters.audience_bias != "any" and report.audience.bias != filters.audience_bias:
        return False
    if filters.age_group != "any" and report.audience.age_group != filters.age_group:
        return False
    return True


def sort_reports(reports: list[ChannelReport], filters: SearchFilters) -> list[ChannelReport]:
    if filters.sort_by == "views":
        key = lambda report: (report.avg_views_recent, report.activity_score)
    elif filters.sort_by == "reactions":
        key = lambda report: (report.avg_reactions_recent, report.activity_score)
    elif filters.sort_by == "comments":
        key = lambda report: (report.avg_comments_recent, report.activity_score)
    elif filters.sort_by == "subscribers":
        key = lambda report: (report.subscribers or 0, report.activity_score)
    elif filters.sort_by == "fresh":
        key = lambda report: (report.last_post_at is not None, report.last_post_at or datetime.min.replace(tzinfo=timezone.utc))
    else:
        key = lambda report: (report.activity_score, report.avg_views_recent)
    return sorted(reports, key=key, reverse=True)


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
