from __future__ import annotations

import re
from dataclasses import replace

from ChannelsParser.models import AgeGroup, AudienceBias, SearchFilters, SortMode


SET_HELP = (
    "Формат /set:\n"
    "/set subs 100 300\n"
    "/set subs any\n"
    "/set days 7\n"
    "/set views 100\n"
    "/set views any\n"
    "/set score 35\n"
    "/set audience female\n"
    "/set age 25-34\n"
    "/set sort views"
)

VALID_AGES: set[AgeGroup] = {"any", "14-17", "18-24", "25-34", "35+"}
VALID_SORTS: set[SortMode] = {"score", "views", "subscribers", "fresh", "reactions", "comments"}


def parse_queries(raw: str) -> list[str]:
    return [part.strip() for part in re.split(r"[,;\n]+", raw) if part.strip()]


def apply_set_command(filters: SearchFilters, raw: str) -> tuple[SearchFilters, str]:
    tokens = raw.strip().split()
    if not tokens:
        raise ValueError(SET_HELP)

    key = tokens[0].lower()
    values = tokens[1:]

    if key in {"subs", "subscribers", "подписчики"}:
        return _set_subscribers(filters, values)
    if key in {"days", "fresh", "active", "дни"}:
        return _set_days(filters, values)
    if key in {"views", "просмотры"}:
        return _set_views(filters, values)
    if key in {"score", "activity", "активность"}:
        return _set_score(filters, values)
    if key in {"audience", "gender", "ца", "пол"}:
        return _set_audience(filters, values)
    if key in {"age", "возраст"}:
        return _set_age(filters, values)
    if key in {"sort", "сортировка"}:
        return _set_sort(filters, values)

    raise ValueError(f"Не знаю фильтр '{key}'.\n\n{SET_HELP}")


def _set_subscribers(filters: SearchFilters, values: list[str]) -> tuple[SearchFilters, str]:
    if _is_any(values):
        return replace(filters, min_subscribers=None, max_subscribers=None), "Подписчики: любые"

    joined = " ".join(values)
    numbers = [int(value) for value in re.findall(r"\d+", joined)]
    if len(numbers) == 1 and joined.lower().startswith(("от", "from", ">")):
        updated = replace(filters, min_subscribers=numbers[0], max_subscribers=None)
    elif len(numbers) == 1 and joined.lower().startswith(("до", "to", "<")):
        updated = replace(filters, min_subscribers=None, max_subscribers=numbers[0])
    elif len(numbers) >= 2:
        low, high = sorted(numbers[:2])
        updated = replace(filters, min_subscribers=low, max_subscribers=high)
    else:
        raise ValueError("Для подписчиков укажи диапазон: /set subs 100 300 или /set subs any")

    return updated, f"Подписчики: {_range_text(updated.min_subscribers, updated.max_subscribers)}"


def _set_days(filters: SearchFilters, values: list[str]) -> tuple[SearchFilters, str]:
    days = _single_int(values, "Укажи количество дней: /set days 7")
    if days < 1 or days > 60:
        raise ValueError("Свежесть поста должна быть от 1 до 60 дней")
    return replace(filters, max_last_post_days=days), f"Последний пост: <= {days} дн."


def _set_views(filters: SearchFilters, values: list[str]) -> tuple[SearchFilters, str]:
    if _is_any(values):
        return replace(filters, min_avg_views=None), "Средние просмотры: любые"
    views = _single_int(values, "Укажи просмотры: /set views 100 или /set views any")
    if views < 0:
        raise ValueError("Просмотры не могут быть отрицательными")
    return replace(filters, min_avg_views=views), f"Средние просмотры: >= {views}"


def _set_score(filters: SearchFilters, values: list[str]) -> tuple[SearchFilters, str]:
    if len(values) != 1:
        raise ValueError("Укажи score от 0 до 100: /set score 35")
    try:
        score = float(values[0].replace(",", "."))
    except ValueError as exc:
        raise ValueError("Score должен быть числом от 0 до 100") from exc
    if score < 0 or score > 100:
        raise ValueError("Score должен быть от 0 до 100")
    return replace(filters, min_activity_score=score), f"Минимальный score: {score:.0f}/100"


def _set_audience(filters: SearchFilters, values: list[str]) -> tuple[SearchFilters, str]:
    if len(values) != 1:
        raise ValueError("Укажи аудиторию: /set audience female|male|any")
    raw = values[0].lower()
    mapping: dict[str, AudienceBias] = {
        "female": "female",
        "women": "female",
        "woman": "female",
        "жен": "female",
        "женская": "female",
        "male": "male",
        "men": "male",
        "man": "male",
        "муж": "male",
        "мужская": "male",
        "any": "any",
        "all": "any",
        "любая": "any",
        "любой": "any",
    }
    value = mapping.get(raw)
    if value is None:
        raise ValueError("Аудитория: female, male или any")
    return replace(filters, audience_bias=value), f"Аудитория: {value}"


def _set_age(filters: SearchFilters, values: list[str]) -> tuple[SearchFilters, str]:
    if len(values) != 1:
        raise ValueError("Укажи возраст: /set age 18-24 или /set age any")
    value = values[0]
    if value not in VALID_AGES:
        raise ValueError("Возраст: any, 14-17, 18-24, 25-34 или 35+")
    return replace(filters, age_group=value), f"Возраст: {value}"


def _set_sort(filters: SearchFilters, values: list[str]) -> tuple[SearchFilters, str]:
    if len(values) != 1:
        raise ValueError("Укажи сортировку: /set sort score|views|reactions|comments|subscribers|fresh")
    value = values[0]
    if value not in VALID_SORTS:
        raise ValueError("Сортировка: score, views, reactions, comments, subscribers или fresh")
    return replace(filters, sort_by=value), f"Сортировка: {value}"


def _single_int(values: list[str], error: str) -> int:
    if len(values) != 1:
        raise ValueError(error)
    try:
        return int(values[0])
    except ValueError as exc:
        raise ValueError(error) from exc


def _is_any(values: list[str]) -> bool:
    return len(values) == 1 and values[0].lower() in {"any", "all", "любые", "любой", "любая", "нет"}


def _range_text(min_value: int | None, max_value: int | None) -> str:
    if min_value is None and max_value is None:
        return "любые"
    if min_value is None:
        return f"до {max_value}"
    if max_value is None:
        return f"от {min_value}"
    return f"{min_value}-{max_value}"
