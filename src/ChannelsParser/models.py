from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal


AudienceBias = Literal["any", "female", "male"]
AgeGroup = Literal["any", "14-17", "18-24", "25-34", "35+"]
SortMode = Literal["score", "views", "subscribers", "fresh", "reactions", "comments"]
ScanMode = Literal["search", "audit"]
ScanStatus = Literal["running", "done", "failed"]


@dataclass(slots=True)
class SearchFilters:
    min_subscribers: int | None = 100
    max_subscribers: int | None = 5000
    max_last_post_days: int = 7
    min_activity_score: float = 35.0
    min_avg_views: int | None = None
    audience_bias: AudienceBias = "female"
    age_group: AgeGroup = "any"
    sort_by: SortMode = "score"


@dataclass(slots=True)
class FilterPreset:
    preset_id: int
    user_id: int
    title: str
    filters: SearchFilters
    created_at: datetime
    updated_at: datetime


@dataclass(slots=True)
class AudienceEstimate:
    bias: AudienceBias
    confidence: float
    age_group: AgeGroup
    matched_keywords: list[str] = field(default_factory=list)
    note: str = "Heuristic estimate from public text, not Telegram demographic data."


@dataclass(slots=True)
class ChannelReport:
    telegram_id: int
    title: str
    username: str | None
    link: str | None
    description: str
    subscribers: int | None
    last_post_at: datetime | None
    post_count_24h: int
    post_count_7d: int
    avg_views_recent: float
    avg_views_24h: float
    avg_reactions_recent: float
    avg_comments_recent: float
    view_rate: float | None
    reaction_rate: float | None
    activity_score: float
    audience: AudienceEstimate
    matched_queries: list[str] = field(default_factory=list)
    collected_at: datetime | None = None

    @property
    def display_link(self) -> str:
        if self.link:
            return self.link
        if self.username:
            return f"@{self.username}"
        return "private/public id only"


@dataclass(slots=True)
class SearchRunResult:
    reports: list[ChannelReport]
    total_candidates: int = 0
    inspected_channels: int = 0
    skipped_channels: int = 0
    errors: list[str] = field(default_factory=list)


@dataclass(slots=True)
class ScanRecord:
    scan_id: str
    user_id: int | None
    mode: ScanMode
    status: ScanStatus
    queries: list[str]
    filters: SearchFilters
    total_candidates: int
    total_reports: int
    error: str | None
    started_at: datetime
    finished_at: datetime | None = None
