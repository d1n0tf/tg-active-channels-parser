from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class QueryPreset:
    key: str
    title: str
    queries: tuple[str, ...]


QUERY_PRESETS: dict[str, QueryPreset] = {
    "finance_female": QueryPreset(
        key="finance_female",
        title="Финансы + женская ЦА",
        queries=(
            "семейный бюджет",
            "финансы для женщин",
            "деньги в декрете",
            "экономия для семьи",
            "финансовая грамотность",
            "заработок для мам",
            "маркетплейсы для женщин",
            "рассрочки и кредитки",
            "скидки для семьи",
            "мамы бизнес",
            "самозанятость для женщин",
            "домашняя бухгалтерия",
        ),
    ),
    "finance_general": QueryPreset(
        key="finance_general",
        title="Финансы общие",
        queries=(
            "финансовая грамотность",
            "личные финансы",
            "семейный бюджет",
            "инвестиции для начинающих",
            "налоги самозанятых",
            "кредитная история",
            "экономия денег",
        ),
    ),
    "marketplaces": QueryPreset(
        key="marketplaces",
        title="Маркетплейсы",
        queries=(
            "wildberries для новичков",
            "ozon бизнес",
            "маркетплейсы для женщин",
            "селлеры wildberries",
            "заработок на маркетплейсах",
            "самозанятость маркетплейсы",
        ),
    ),
}


def get_preset(key: str) -> QueryPreset | None:
    return QUERY_PRESETS.get(key)
