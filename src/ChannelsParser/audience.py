from __future__ import annotations

import re
from collections import Counter

from ChannelsParser.models import AgeGroup, AudienceBias, AudienceEstimate


FEMALE_KEYWORDS = {
    "жен": 1.4,
    "девуш": 1.2,
    "мама": 1.4,
    "мамы": 1.4,
    "материн": 1.2,
    "декрет": 1.5,
    "беремен": 1.3,
    "семья": 0.9,
    "дети": 0.8,
    "ребен": 0.8,
    "отношен": 0.8,
    "красота": 1.3,
    "космет": 1.2,
    "уход": 0.9,
    "мода": 1.0,
    "стиль": 0.8,
    "плать": 1.0,
    "рецепт": 0.9,
    "психолог": 0.7,
    "дом": 0.5,
    "маркетплейс": 0.6,
    "самозанят": 0.5,
}

MALE_KEYWORDS = {
    "муж": 1.3,
    "авто": 1.1,
    "футбол": 1.2,
    "хоккей": 1.0,
    "рыбал": 1.1,
    "ставк": 1.2,
    "бетт": 1.2,
    "крипт": 0.9,
    "трейд": 0.9,
    "желез": 0.7,
    "оруж": 1.1,
    "спорт": 0.6,
}

AGE_KEYWORDS: dict[AgeGroup, tuple[str, ...]] = {
    "14-17": ("школ", "егэ", "огэ", "подрост", "тинейдж", "колледж"),
    "18-24": ("студент", "универ", "карьер", "первая работа", "переезд", "сессия"),
    "25-34": ("мама", "декрет", "ипотек", "семейн", "дети", "маркетплейс", "самозанят"),
    "35+": ("пенси", "дача", "здоровье", "родител", "сад", "внук"),
}


def estimate_audience(title: str, description: str) -> AudienceEstimate:
    text = _normalize(f"{title} {description}")

    female_score, female_matches = _weighted_score(text, FEMALE_KEYWORDS)
    male_score, male_matches = _weighted_score(text, MALE_KEYWORDS)

    if female_score == 0 and male_score == 0:
        bias: AudienceBias = "any"
        confidence = 0.0
    elif female_score >= male_score:
        bias = "female"
        confidence = _confidence(female_score, male_score)
    else:
        bias = "male"
        confidence = _confidence(male_score, female_score)

    age_group = _estimate_age(text)
    matches = [*female_matches, *male_matches]

    return AudienceEstimate(
        bias=bias,
        confidence=round(confidence, 2),
        age_group=age_group,
        matched_keywords=matches[:12],
    )


def _normalize(value: str) -> str:
    value = value.lower().replace("ё", "е")
    return re.sub(r"\s+", " ", value)


def _weighted_score(text: str, keywords: dict[str, float]) -> tuple[float, list[str]]:
    score = 0.0
    matches: list[str] = []
    for keyword, weight in keywords.items():
        if keyword in text:
            score += weight
            matches.append(keyword)
    return score, matches


def _confidence(winner: float, loser: float) -> float:
    total = winner + loser
    if total <= 0:
        return 0.0
    margin = (winner - loser) / total
    volume_bonus = min(winner / 5, 0.35)
    return min(0.95, 0.5 + margin * 0.35 + volume_bonus)


def _estimate_age(text: str) -> AgeGroup:
    scores: Counter[AgeGroup] = Counter()
    for group, keywords in AGE_KEYWORDS.items():
        for keyword in keywords:
            if keyword in text:
                scores[group] += 1
    if not scores:
        return "any"
    return scores.most_common(1)[0][0]
