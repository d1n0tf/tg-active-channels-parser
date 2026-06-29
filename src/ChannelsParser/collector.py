from __future__ import annotations

import asyncio
import inspect
import re
from collections.abc import Iterable, Sequence
from datetime import datetime, timedelta, timezone
from statistics import mean
from typing import Any, cast

from telethon import TelegramClient, functions, types, utils
from telethon.errors import (
    ChannelInvalidError,
    FloodWaitError,
    RPCError,
    UsernameInvalidError,
    UsernameNotOccupiedError,
)

from ChannelsParser.audience import estimate_audience
from ChannelsParser.config import AppSettings
from ChannelsParser.models import ChannelReport, SearchFilters, SearchRunResult
from ChannelsParser.scoring import activity_score, matches_filters, sort_reports


class TelegramChannelCollector:
    def __init__(self, settings: AppSettings):
        self._settings = settings
        self._client = TelegramClient(
            settings.telegram_session,
            settings.telegram_api_id,
            settings.telegram_api_hash,
        )

    async def connect(self) -> None:
        await self._client.connect()
        if not await self._client.is_user_authorized():
            await self._disconnect()
            raise RuntimeError(
                "Telegram user session is not authorized. Run: uv run python -m ChannelsParser.login"
            )

    async def close(self) -> None:
        await self._disconnect()

    async def _disconnect(self) -> None:
        result: object = self._client.disconnect()
        if inspect.isawaitable(result):
            await result

    async def search_channels(
        self, queries: list[str], filters: SearchFilters
    ) -> SearchRunResult:
        now = datetime.now(timezone.utc)
        reports_by_id: dict[int, ChannelReport] = {}
        total_candidates = 0
        inspected_channels = 0
        skipped_channels = 0
        errors: list[str] = []

        for query in _clean_queries(queries):
            try:
                found = await self._with_short_flood_retry(
                    self._search_public_chats, query
                )
            except FloodWaitError as exc:
                errors.append(f"{query}: Telegram flood wait {exc.seconds}s")
                continue
            except RPCError as exc:
                errors.append(f"{query}: {type(exc).__name__}")
                continue
            except Exception as exc:
                errors.append(f"{query}: {type(exc).__name__}: {exc}")
                continue

            total_candidates += len(found)
            for chat in found:
                if not _is_public_broadcast_channel(chat):
                    skipped_channels += 1
                    continue

                try:
                    report = await self._with_short_flood_retry(
                        self.inspect_channel,
                        chat,
                        matched_query=query,
                        now=now,
                    )
                    inspected_channels += 1
                except FloodWaitError as exc:
                    errors.append(
                        f"{getattr(chat, 'username', chat.id)}: Telegram flood wait {exc.seconds}s"
                    )
                    skipped_channels += 1
                    continue
                except RPCError as exc:
                    errors.append(
                        f"{getattr(chat, 'username', chat.id)}: {type(exc).__name__}"
                    )
                    skipped_channels += 1
                    continue
                except Exception as exc:
                    errors.append(
                        f"{getattr(chat, 'username', chat.id)}: {type(exc).__name__}: {exc}"
                    )
                    skipped_channels += 1
                    continue

                existing = reports_by_id.get(report.telegram_id)
                if existing:
                    if query not in existing.matched_queries:
                        existing.matched_queries.append(query)
                    continue
                reports_by_id[report.telegram_id] = report

        filtered = [
            report
            for report in reports_by_id.values()
            if matches_filters(report, filters, now=now)
        ]
        return SearchRunResult(
            reports=sort_reports(filtered, filters),
            total_candidates=total_candidates,
            inspected_channels=inspected_channels,
            skipped_channels=skipped_channels,
            errors=errors,
        )

    async def _with_short_flood_retry(self, func, *args, **kwargs):
        try:
            return await func(*args, **kwargs)
        except FloodWaitError as exc:
            if exc.seconds > self._settings.flood_sleep_limit_seconds:
                raise
            await asyncio.sleep(exc.seconds)
            return await func(*args, **kwargs)

    async def inspect_channel_identifier(
        self, identifier: str, *, matched_query: str = "manual"
    ) -> ChannelReport:
        username = normalize_channel_identifier(identifier)
        try:
            entity = await self._with_short_flood_retry(
                self._client.get_entity, username
            )
        except (
            UsernameInvalidError,
            UsernameNotOccupiedError,
            ChannelInvalidError,
        ) as exc:
            raise ValueError(f"Не смог найти канал: {identifier}") from exc
        if not isinstance(entity, types.Channel) or not _is_public_broadcast_channel(
            entity
        ):
            raise ValueError("Это не публичный broadcast-канал Telegram")
        return await self._with_short_flood_retry(
            self.inspect_channel, entity, matched_query=matched_query
        )

    async def inspect_channel(
        self,
        channel: types.Channel,
        *,
        matched_query: str,
        now: datetime | None = None,
    ) -> ChannelReport:
        now = now or datetime.now(timezone.utc)
        input_channel = cast(types.TypeInputChannel, utils.get_input_channel(channel))
        full = await self._client(
            functions.channels.GetFullChannelRequest(channel=input_channel)
        )
        full_chat = full.full_chat

        description = getattr(full_chat, "about", "") or ""
        subscribers = getattr(full_chat, "participants_count", None) or getattr(
            channel, "participants_count", None
        )
        messages = await self._client.get_messages(
            channel, limit=self._settings.history_limit
        )
        posts = [
            message
            for message in _message_list(messages)
            if _looks_like_channel_post(message)
        ]

        last_post_at = _message_datetime(posts[0]) if posts else None
        day_ago = now - timedelta(hours=24)
        week_ago = now - timedelta(days=7)
        posts_24h = [
            message
            for message in posts
            if _message_at_or_after(message, day_ago)
        ]
        posts_7d = [
            message
            for message in posts
            if _message_at_or_after(message, week_ago)
        ]

        recent_posts = posts[: min(12, len(posts))]
        recent_views = _message_views(recent_posts)
        day_views = _message_views(posts_24h)
        recent_reactions = [_reaction_count(message) for message in recent_posts]
        recent_comments = [_comment_count(message) for message in recent_posts]

        avg_views_recent = _avg(recent_views)
        avg_views_24h = _avg(day_views)
        avg_reactions_recent = _avg(recent_reactions)
        avg_comments_recent = _avg(recent_comments)
        view_rate = (avg_views_recent / subscribers) if subscribers else None
        reaction_rate = (
            (avg_reactions_recent / avg_views_recent) if avg_views_recent else None
        )

        audience = estimate_audience(channel.title or "", description)
        score = activity_score(
            last_post_at=last_post_at,
            post_count_24h=len(posts_24h),
            post_count_7d=len(posts_7d),
            avg_views_recent=avg_views_recent,
            avg_reactions_recent=avg_reactions_recent,
            avg_comments_recent=avg_comments_recent,
            subscribers=subscribers,
            now=now,
        )

        username = getattr(channel, "username", None)
        link = f"https://t.me/{username}" if username else None

        return ChannelReport(
            telegram_id=channel.id,
            title=channel.title or "Untitled channel",
            username=username,
            link=link,
            description=description,
            subscribers=subscribers,
            last_post_at=last_post_at,
            post_count_24h=len(posts_24h),
            post_count_7d=len(posts_7d),
            avg_views_recent=avg_views_recent,
            avg_views_24h=avg_views_24h,
            avg_reactions_recent=avg_reactions_recent,
            avg_comments_recent=avg_comments_recent,
            view_rate=view_rate,
            reaction_rate=reaction_rate,
            activity_score=score,
            audience=audience,
            matched_queries=[matched_query],
            collected_at=now,
        )

    async def _search_public_chats(self, query: str) -> list[types.Channel]:
        result = await self._client(
            functions.contacts.SearchRequest(
                q=query,
                limit=self._settings.search_limit_per_query,
                broadcasts=True,
            )
        )
        return [chat for chat in result.chats if isinstance(chat, types.Channel)]


def _clean_queries(queries: list[str]) -> list[str]:
    cleaned: list[str] = []
    for query in queries:
        query = query.strip()
        if query and query not in cleaned:
            cleaned.append(query)
    return cleaned


def normalize_channel_identifier(identifier: str) -> str:
    value = identifier.strip()
    value = re.sub(r"^https?://", "", value, flags=re.IGNORECASE)
    value = re.sub(r"^(www\.)?(t\.me|telegram\.me)/", "", value, flags=re.IGNORECASE)
    value = value.split("?", 1)[0].strip("/")
    value = value.removeprefix("@")
    if value.startswith(("joinchat/", "+", "c/")):
        raise ValueError(
            "Поддерживаются только публичные каналы с username, не приватные или invite-ссылки"
        )
    if value.startswith("s/"):
        value = value.removeprefix("s/")
    if not value:
        raise ValueError("Укажи канал: /check @channel или /check https://t.me/channel")
    if "/" in value:
        value = value.split("/", 1)[0]
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{3,31}", value):
        raise ValueError("Некорректный username канала")
    return value


def _is_public_broadcast_channel(chat: types.Channel) -> bool:
    return bool(getattr(chat, "broadcast", False)) and bool(
        getattr(chat, "username", None)
    )


def _looks_like_channel_post(message: types.Message) -> bool:
    if not isinstance(message, types.Message):
        return False
    return bool(getattr(message, "message", None) or getattr(message, "media", None))


def _message_list(value: object) -> list[types.Message]:
    if value is None:
        return []
    if isinstance(value, types.Message):
        return [value]
    if not isinstance(value, Iterable):
        return []
    return [message for message in value if isinstance(message, types.Message)]


def _message_datetime(message: types.Message) -> datetime | None:
    value = getattr(message, "date", None)
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _message_at_or_after(message: types.Message, cutoff: datetime) -> bool:
    value = _message_datetime(message)
    return value is not None and value >= cutoff


def _views(message: types.Message) -> int | None:
    value = _safe_int(getattr(message, "views", None))
    if value is None:
        return None
    return max(value, 0)


def _message_views(messages: Sequence[types.Message]) -> list[int]:
    views: list[int] = []
    for message in messages:
        value = _views(message)
        if value is not None:
            views.append(value)
    return views


def _reaction_count(message: types.Message) -> int:
    reactions = getattr(message, "reactions", None)
    results = getattr(reactions, "results", None) or []
    return sum(
        max(_safe_int(getattr(reaction, "count", 0)) or 0, 0) for reaction in results
    )


def _comment_count(message: types.Message) -> int:
    replies = getattr(message, "replies", None)
    return max(_safe_int(getattr(replies, "replies", 0)) or 0, 0)


def _avg(values: Sequence[int | float]) -> float:
    if not values:
        return 0.0
    return round(float(mean(values)), 1)


def _safe_int(value: object) -> int | None:
    if value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return None
    try:
        return int(cast(Any, value))
    except (TypeError, ValueError):
        return None
