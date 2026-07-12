from __future__ import annotations

import re
from dataclasses import dataclass

from ChannelsParser.models import ChannelKind, ChannelReport


COMMERCIAL_KEYWORDS = (
    "агентств",
    "ателье",
    "бренд",
    "бутик",
    "доставк",
    "заказ",
    "каталог",
    "магазин",
    "маркетинг",
    "наличи",
    "одежд",
    "опт",
    "пиар",
    "продаж",
    "реклам",
    "розниц",
    "салон",
    "сервис",
    "студия",
    "услуг",
    "шоурум",
    "agency",
    "brand",
    "boutique",
    "shop",
    "showroom",
    "smm",
)

PROMO_SERVICE_KEYWORDS = (
    "ads",
    "advertising",
    "pr",
    "promo",
    "smm",
    "взаимопиар",
    "закуп реклам",
    "коллаб",
    "маркировк",
    "медиакит",
    "менеджер",
    "пиар",
    "посев",
    "прайс",
    "продвиж",
    "реклам",
    "сотруднич",
)

THEMATIC_KEYWORDS = (
    "афиша",
    "бизнес",
    "бюджет",
    "гайд",
    "дизайн",
    "журнал",
    "инвест",
    "канал",
    "космет",
    "кредит",
    "маркетплейс",
    "медиа",
    "мод",
    "налог",
    "новост",
    "обзор",
    "образован",
    "одежд",
    "подбор",
    "работ",
    "сообщество",
    "стил",
    "уход",
    "финанс",
    "эконом",
    "beauty",
    "fashion",
    "media",
)

PERSONAL_PHRASES = (
    "блог о жизни",
    "будни",
    "дневник",
    "личное",
    "личный блог",
    "мой блог",
    "моя жизнь",
    "мысли вслух",
    "про жизнь",
    "семейный дневник",
    "заметки",
)

COMMON_NAME_WORDS = {
    "алена",
    "анастасия",
    "аня",
    "вика",
    "дарья",
    "девочка",
    "девочки",
    "екатерина",
    "катя",
    "ксения",
    "лена",
    "маша",
    "наташа",
    "настя",
    "оля",
    "полина",
    "саша",
    "юля",
}

QUERY_STOPWORDS = {
    "для",
    "или",
    "как",
    "под",
    "при",
    "про",
    "что",
    "это",
    "with",
}


@dataclass(frozen=True, slots=True)
class ChannelQuality:
    commercial_score: int
    promo_service_score: int
    title_promo_service_score: int
    thematic_score: int
    query_score: int
    personal_score: int
    name_signal: bool

    @property
    def business_score(self) -> int:
        return self.commercial_score + self.thematic_score + self.query_score


def matches_channel_kind(report: ChannelReport, channel_kind: ChannelKind) -> bool:
    if channel_kind == "any":
        return True

    quality = estimate_channel_quality(report)
    if channel_kind == "commercial":
        return quality.commercial_score > 0 and not _looks_personal_only(quality)

    return (
        quality.business_score > 0
        and not _looks_personal_only(quality)
        and not _looks_promo_service_only(quality)
    )


def estimate_channel_quality(report: ChannelReport) -> ChannelQuality:
    title = _normalize(report.title)
    text = _normalize(f"{report.title} {report.description}")
    commercial_score = _keyword_score(text, COMMERCIAL_KEYWORDS)
    promo_service_score = _keyword_score(text, PROMO_SERVICE_KEYWORDS)
    title_promo_service_score = _keyword_score(title, PROMO_SERVICE_KEYWORDS)
    thematic_score = _keyword_score(text, THEMATIC_KEYWORDS)
    query_score = _query_score(text, report.matched_queries)
    personal_score = _keyword_score(text, PERSONAL_PHRASES)
    name_signal = _has_name_signal(title)

    return ChannelQuality(
        commercial_score=commercial_score,
        promo_service_score=promo_service_score,
        title_promo_service_score=title_promo_service_score,
        thematic_score=thematic_score,
        query_score=query_score,
        personal_score=personal_score,
        name_signal=name_signal,
    )


def _looks_personal_only(quality: ChannelQuality) -> bool:
    if quality.commercial_score > 0:
        return False
    if quality.name_signal and quality.thematic_score <= 1:
        return True
    return quality.personal_score > 0 and quality.thematic_score <= 1


def _looks_promo_service_only(quality: ChannelQuality) -> bool:
    if quality.title_promo_service_score > 0 and quality.thematic_score == 0:
        return True
    return quality.promo_service_score >= 2 and quality.thematic_score <= 1


def _keyword_score(text: str, keywords: tuple[str, ...]) -> int:
    return sum(1 for keyword in keywords if keyword in text)


def _query_score(text: str, queries: list[str]) -> int:
    tokens: set[str] = set()
    for query in queries:
        tokens.update(
            token
            for token in re.findall(r"[a-zа-я0-9]+", _normalize(query))
            if len(token) >= 4 and token not in QUERY_STOPWORDS
        )
    return sum(1 for token in tokens if token in text)


def _has_name_signal(title: str) -> bool:
    words = re.findall(r"[a-zа-я]+", title)
    if not words:
        return False
    if words[0] in COMMON_NAME_WORDS:
        return True
    return any(word in COMMON_NAME_WORDS for word in words[:2]) and "|" in title


def _normalize(value: str) -> str:
    value = value.lower().replace("ё", "е")
    return re.sub(r"\s+", " ", value)
