from __future__ import annotations

from datetime import datetime, timedelta, timezone

from ChannelsParser.audience import estimate_audience
from ChannelsParser.models import ChannelReport, SearchFilters
from ChannelsParser.scoring import activity_score, matches_filters


def test_estimate_audience_detects_female_finance_context() -> None:
    estimate = estimate_audience(
        "Деньги в декрете",
        "Финансовая грамотность для мам, семейный бюджет и заработок на маркетплейсах",
    )

    assert estimate.bias == "female"
    assert estimate.confidence > 0.6
    assert estimate.age_group == "25-34"


def test_activity_score_rewards_fresh_active_channels() -> None:
    now = datetime(2026, 6, 29, tzinfo=timezone.utc)

    score = activity_score(
        last_post_at=now - timedelta(hours=2),
        post_count_24h=3,
        post_count_7d=10,
        avg_views_recent=120,
        avg_reactions_recent=12,
        avg_comments_recent=4,
        subscribers=500,
        now=now,
    )

    assert score >= 75


def test_matches_filters_rejects_stale_or_wrong_audience() -> None:
    now = datetime(2026, 6, 29, tzinfo=timezone.utc)
    audience = estimate_audience("Авто и ставки", "Мужской канал про спорт")
    report = ChannelReport(
        telegram_id=1,
        title="Авто и ставки",
        username="cars",
        link="https://t.me/cars",
        description="Мужской канал про спорт",
        subscribers=250,
        last_post_at=now - timedelta(days=8),
        post_count_24h=0,
        post_count_7d=0,
        avg_views_recent=50,
        avg_views_24h=0,
        avg_reactions_recent=1,
        avg_comments_recent=0,
        view_rate=0.2,
        reaction_rate=0.02,
        activity_score=40,
        audience=audience,
        matched_queries=["финансы"],
        collected_at=now,
    )

    assert not matches_filters(report, SearchFilters(audience_bias="female"), now=now)


def test_matches_filters_rejects_personal_blog_by_default() -> None:
    now = datetime(2026, 6, 29, tzinfo=timezone.utc)
    report = _report(
        title="Маша | личный блог",
        description="Моя жизнь, будни и мысли вслух для девушек",
        subscribers=2500,
        now=now,
    )

    assert not matches_filters(report, SearchFilters(), now=now)
    assert matches_filters(report, SearchFilters(channel_kind="any"), now=now)


def test_matches_filters_allows_commercial_clothing_channel() -> None:
    now = datetime(2026, 6, 29, tzinfo=timezone.utc)
    report = _report(
        title="Шоурум женской одежды",
        description="Каталог, заказ, доставка, бренд женской одежды",
        subscribers=12000,
        now=now,
    )

    assert matches_filters(report, SearchFilters(), now=now)
    assert matches_filters(report, SearchFilters(channel_kind="commercial"), now=now)


def test_matches_filters_allows_specific_query_match() -> None:
    now = datetime(2026, 6, 29, tzinfo=timezone.utc)
    report = _report(
        title="Свадебные ведущие Самара",
        description="Мероприятия, даты и контакты",
        subscribers=7000,
        now=now,
    )
    report.matched_queries = ["свадебные ведущие"]

    assert matches_filters(report, SearchFilters(audience_bias="any"), now=now)


def test_matches_filters_rejects_promo_service_channel_as_thematic() -> None:
    now = datetime(2026, 6, 29, tzinfo=timezone.utc)
    report = _report(
        title="Advertising agency",
        description="ads, promo, smm, price",
        subscribers=7000,
        now=now,
    )

    assert not matches_filters(report, SearchFilters(audience_bias="any"), now=now)
    assert matches_filters(report, SearchFilters(audience_bias="any", channel_kind="any"), now=now)
    assert matches_filters(report, SearchFilters(audience_bias="any", channel_kind="commercial"), now=now)


def _report(title: str, description: str, subscribers: int, now: datetime) -> ChannelReport:
    return ChannelReport(
        telegram_id=123,
        title=title,
        username="sample_channel",
        link="https://t.me/sample_channel",
        description=description,
        subscribers=subscribers,
        last_post_at=now - timedelta(hours=2),
        post_count_24h=2,
        post_count_7d=10,
        avg_views_recent=500,
        avg_views_24h=600,
        avg_reactions_recent=10,
        avg_comments_recent=2,
        view_rate=0.2,
        reaction_rate=0.02,
        activity_score=70,
        audience=estimate_audience(title, description),
        matched_queries=["женская одежда"],
        collected_at=now,
    )
