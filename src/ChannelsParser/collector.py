from __future__ import annotations

import asyncio
import inspect
import re
from dataclasses import dataclass
from collections.abc import Iterable, Sequence
from datetime import datetime, timedelta, timezone
from statistics import mean
from typing import Any, cast

from telethon import TelegramClient, functions, types, utils
from telethon.errors import (
    ChannelInvalidError,
    FloodWaitError,
    MsgIdInvalidError,
    RPCError,
    TimedOutError,
    UsernameInvalidError,
    UsernameNotOccupiedError,
)

from ChannelsParser.accounts import AccountPool
from ChannelsParser.audience import estimate_audience
from ChannelsParser.config import AppSettings
from ChannelsParser.models import ChannelReport, SearchFilters, SearchRunResult
from ChannelsParser.scoring import activity_score, matches_filters, sort_reports


USERNAME_RE = re.compile(r"(?<![\w/])@([A-Za-z][A-Za-z0-9_]{3,31})")
TELEGRAM_LINK_RE = re.compile(
    r"(?:https?://)?(?:www\.)?(?:t\.me|telegram\.me)/([A-Za-z][A-Za-z0-9_]{3,31})(?:\b|/)",
    re.IGNORECASE,
)


@dataclass(slots=True, eq=False)
class CandidateSource:
    label: str
    owner_username: str | None = None
    owner_display_name: str | None = None

    def __eq__(self, other: object) -> bool:
        if isinstance(other, str):
            return self.label == other
        if not isinstance(other, CandidateSource):
            return False
        return (
            self.label == other.label
            and self.owner_username == other.owner_username
            and self.owner_display_name == other.owner_display_name
        )


class TelegramChannelCollector:
    def __init__(self, settings: AppSettings, pool: AccountPool | None = None):
        self._settings = settings
        self._pool = pool or AccountPool.from_settings(settings)

    @property
    def pool(self) -> AccountPool:
        return self._pool

    @property
    def _client(self) -> TelegramClient:
        """Always the currently active account client (after rotation)."""
        return self._pool.client

    async def connect(self) -> None:
        await self._pool.connect()

    async def close(self) -> None:
        await self._pool.close()

    async def search_channels(
        self, queries: list[str], filters: SearchFilters, *, should_stop=None
    ) -> SearchRunResult:
        now = datetime.now(timezone.utc)
        reports_by_id: dict[int, ChannelReport] = {}
        total_candidates = 0
        inspected_channels = 0
        skipped_channels = 0
        errors: list[str] = []

        for query in _clean_queries(queries):
            if _should_stop(should_stop):
                errors.append("Остановлено пользователем")
                break
            try:
                found = await self._with_short_flood_retry(lambda: self._search_public_chats(query))
            except FloodWaitError as exc:
                wait_h = exc.seconds / 3600
                errors.append(
                    f"{query}: FloodWait {exc.seconds}s (~{wait_h:.1f}ч), аккаунты исчерпаны"
                )
                continue
            except RPCError as exc:
                errors.append(f"{query}: {type(exc).__name__}")
                continue
            except Exception as exc:
                errors.append(f"{query}: {type(exc).__name__}: {exc}")
                continue

            total_candidates += len(found)
            for chat in found:
                if _should_stop(should_stop):
                    errors.append("Остановлено пользователем")
                    break
                if not _is_public_broadcast_channel(chat):
                    skipped_channels += 1
                    continue

                try:
                    report = await self._with_short_flood_retry(
                        lambda: self.inspect_channel(chat, matched_query=query, now=now)
                    )
                    inspected_channels += 1
                except FloodWaitError as exc:
                    errors.append(
                        f"{getattr(chat, 'username', chat.id)}: FloodWait {exc.seconds}s, нет свободных аккаунтов"
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

    async def discover_channels_from_comments(
        self,
        source_identifier: str,
        filters: SearchFilters,
        *,
        post_limit: int,
        comments_per_post: int,
        profile_limit: int,
        candidate_limit: int,
        gift_limit: int,
        include_comment_links: bool = True,
        include_profile_refs: bool = True,
        should_stop=None,
        should_finish_collection=None,
        progress_callback=None,
    ) -> SearchRunResult:
        now = datetime.now(timezone.utc)
        errors: list[str] = []
        reports_by_id: dict[int, ChannelReport] = {}
        candidates: dict[str, CandidateSource] = {}
        seen_sender_ids: set[int] = set()
        stats: dict[str, int] = {
            "posts_seen": 0,
            "posts_processed": 0,
            "posts_with_replies": 0,
            "discussion_posts": 0,
            "direct_reply_invalid": 0,
            "discussion_missing": 0,
            "comment_fetch_errors": 0,
            "comments_seen": 0,
            "profiles_seen": 0,
            "profiles_skipped_by_limit": 0,
            "comment_refs": 0,
            "bio_refs": 0,
            "personal_channel_refs": 0,
            "gift_profiles_checked": 0,
            "gift_fetch_errors": 0,
            "gift_refs": 0,
            "channel_commenter_refs": 0,
            "inspected_channels": 0,
            "skipped_channels": 0,
            "reports_found": 0,
            "collection_finished_early": 0,
            "phase": 1,  # 1 = posts/comments, 2 = candidate inspection
            "candidates_total": 0,
            "candidates_done": 0,
            "network_retries": 0,
        }
        skipped_channels = 0
        inspected_channels = 0

        source_username = normalize_channel_identifier(source_identifier)
        try:
            source = await self._with_short_flood_retry(
                lambda: self._client.get_entity(source_username)
            )
        except FloodWaitError as exc:
            wait_h = max(1, exc.seconds // 3600)
            raise RuntimeError(
                f"Telegram FloodWait ~{exc.seconds}s (~{wait_h} ч). "
                f"Все парсер-аккаунты в cooldown. Подожди или добавь ещё: "
                f"uv run tg-active-channels-login --name acc2"
            ) from exc
        except (UsernameInvalidError, UsernameNotOccupiedError, ChannelInvalidError) as exc:
            raise ValueError(f"Не смог найти донорский канал: {source_identifier}") from exc
        if not isinstance(source, types.Channel) or not _is_public_broadcast_channel(source):
            raise ValueError("Донор должен быть публичным broadcast-каналом Telegram")

        posts_raw = await self._client.get_messages(source, limit=post_limit)
        posts = [message for message in _message_list(posts_raw) if _looks_like_channel_post(message)]
        stats["posts_seen"] = len(posts)
        stats["posts_with_replies"] = sum(1 for message in posts if _comment_count(message) > 0)
        await _notify_progress(progress_callback, stats)

        collection_stopped_early = False
        for post in posts:
            # Soft finish / hard cancel: stop browsing posts and comments.
            if _should_stop(should_finish_collection) or _should_stop(should_stop):
                collection_stopped_early = True
                stats["collection_finished_early"] = 1
                if _should_stop(should_stop):
                    errors.append("Остановлено пользователем")
                else:
                    errors.append("Обход постов завершён досрочно, обрабатываю собранных кандидатов")
                await _notify_progress(progress_callback, stats)
                break

            try:
                stats["comment_refs"] += _collect_candidate_refs(candidates, getattr(post, "message", None), "пост донора")
                async for comment in self._iter_post_comments(
                    source,
                    post,
                    limit=comments_per_post,
                    stats=stats,
                ):
                    stats["comments_seen"] += 1
                    if (
                        _should_stop(should_finish_collection)
                        or _should_stop(should_stop)
                        or len(candidates) >= candidate_limit
                    ):
                        break
                    await self._collect_sender_refs(
                        candidates,
                        comment,
                        seen_sender_ids=seen_sender_ids,
                        limit=candidate_limit,
                        profile_limit=profile_limit,
                        gift_limit=gift_limit,
                        include_comment_links=include_comment_links,
                        include_profile_refs=include_profile_refs,
                        stats=stats,
                    )
            except FloodWaitError as exc:
                errors.append(f"comments:{post.id}: Telegram flood wait {exc.seconds}s")
                continue
            except MsgIdInvalidError:
                stats["comment_fetch_errors"] += 1
                continue
            except RPCError as exc:
                stats["comment_fetch_errors"] += 1
                if len(errors) < 5:
                    errors.append(f"comments:{post.id}: {type(exc).__name__}")
                continue
            except Exception as exc:
                stats["comment_fetch_errors"] += 1
                if len(errors) < 5:
                    errors.append(f"comments:{post.id}: {type(exc).__name__}: {exc}")
                continue
            finally:
                stats["posts_processed"] += 1
                await _notify_progress(progress_callback, stats)

            if _should_stop(should_finish_collection) or _should_stop(should_stop) or len(candidates) >= candidate_limit:
                if len(candidates) >= candidate_limit:
                    errors.append(f"Достигнут лимит кандидатов: {candidate_limit}")
                elif not collection_stopped_early and (
                    _should_stop(should_finish_collection) or _should_stop(should_stop)
                ):
                    collection_stopped_early = True
                    stats["collection_finished_early"] = 1
                    if _should_stop(should_stop):
                        errors.append("Остановлено пользователем")
                    else:
                        errors.append("Обход постов завершён досрочно, обрабатываю собранных кандидатов")
                break

        # Hard cancel skips candidate inspection. Soft finish still processes collected candidates.
        to_inspect = [
            (identifier, source)
            for identifier, source in list(candidates.items())[:candidate_limit]
            if identifier.lower() != source_username.lower()
        ]
        stats["phase"] = 2
        stats["candidates_total"] = len(to_inspect)
        stats["candidates_done"] = 0
        await _notify_progress(progress_callback, stats)

        for identifier, source in to_inspect:
            if _should_stop(should_stop):
                if "Остановлено пользователем" not in errors:
                    errors.append("Остановлено пользователем")
                break
            try:
                report = await self.inspect_channel_identifier(identifier, matched_query=f"discover:{source.label}")
                inspected_channels += 1
                stats["inspected_channels"] = inspected_channels
            except (ValueError, RPCError) as exc:
                skipped_channels += 1
                stats["skipped_channels"] = skipped_channels
                if len(errors) < 20:
                    errors.append(f"{identifier}: {type(exc).__name__}")
                stats["candidates_done"] = min(stats["candidates_total"], stats["candidates_done"] + 1)
                await _notify_progress(progress_callback, stats)
                continue
            except Exception as exc:
                skipped_channels += 1
                stats["skipped_channels"] = skipped_channels
                if len(errors) < 20:
                    errors.append(f"{identifier}: {type(exc).__name__}: {exc}")
                stats["candidates_done"] = min(stats["candidates_total"], stats["candidates_done"] + 1)
                await _notify_progress(progress_callback, stats)
                continue

            existing = reports_by_id.get(report.telegram_id)
            if existing:
                for query in report.matched_queries:
                    if query not in existing.matched_queries:
                        existing.matched_queries.append(query)
                _apply_report_owner(existing, source)
                stats["reports_found"] = len(reports_by_id)
            else:
                _apply_report_owner(report, source)
                reports_by_id[report.telegram_id] = report
                stats["reports_found"] = len(reports_by_id)
            stats["candidates_done"] = min(stats["candidates_total"], stats["candidates_done"] + 1)
            await _notify_progress(progress_callback, stats)

        filtered = []
        filter_drop = 0
        for report in reports_by_id.values():
            if matches_filters(report, filters, now=now):
                filtered.append(report)
            else:
                filter_drop += 1
        stats["filter_dropped"] = filter_drop
        stats["filter_passed"] = len(filtered)
        return SearchRunResult(
            reports=sort_reports(filtered, filters),
            total_candidates=len(candidates),
            inspected_channels=inspected_channels,
            skipped_channels=skipped_channels,
            errors=errors,
            stats=stats,
        )

    async def _iter_post_comments(self, source: types.Channel, post: types.Message, *, limit: int, stats: dict[str, int]):
        try:
            async for comment in self._client.iter_messages(source, reply_to=post.id, limit=limit):
                yield comment
            return
        except MsgIdInvalidError:
            stats["direct_reply_invalid"] += 1

        discussion = await self._discussion_target(source, post)
        if discussion is None:
            stats["discussion_missing"] += 1
            return
        discussion_peer, discussion_msg_id = discussion
        stats["discussion_posts"] += 1
        async for comment in self._client.iter_messages(discussion_peer, reply_to=discussion_msg_id, limit=limit):
            yield comment

    async def _discussion_target(self, source: types.Channel, post: types.Message) -> tuple[types.TypeChat, int] | None:
        try:
            discussion = await self._discussion_request(source, post)
        except MsgIdInvalidError:
            return None

        messages = [message for message in getattr(discussion, "messages", []) if isinstance(message, types.Message)]
        if not messages:
            return None

        source_id = getattr(source, "id", None)
        discussion_message = next(
            (
                message
                for message in messages
                if _peer_id_value(getattr(message, "peer_id", None)) not in {None, source_id}
            ),
            messages[-1],
        )
        peer_id = _peer_id_value(getattr(discussion_message, "peer_id", None))
        for chat in getattr(discussion, "chats", []) or []:
            if getattr(chat, "id", None) == peer_id:
                return chat, discussion_message.id
        return None

    async def _collect_sender_refs(
        self,
        candidates: dict[str, CandidateSource],
        message: types.Message,
        *,
        seen_sender_ids: set[int],
        limit: int,
        profile_limit: int,
        gift_limit: int,
        include_comment_links: bool,
        include_profile_refs: bool,
        stats: dict[str, int],
    ) -> None:
        if len(candidates) >= limit:
            return
        sender = getattr(message, "sender", None)
        if sender is None and hasattr(message, "get_sender"):
            sender = await message.get_sender()
        if isinstance(sender, types.Channel):
            username = getattr(sender, "username", None)
            if include_comment_links:
                if username and _is_public_broadcast_channel(sender):
                    if _add_candidate(candidates, username, "комментатор-канал"):
                        stats["channel_commenter_refs"] += 1
                stats["comment_refs"] += _collect_candidate_refs(
                    candidates,
                    getattr(message, "message", None),
                    "комментарий",
                    owner_username=username,
                    owner_display_name=getattr(sender, "title", None),
                )
            return
        if not isinstance(sender, types.User):
            if include_comment_links:
                stats["comment_refs"] += _collect_candidate_refs(
                    candidates,
                    getattr(message, "message", None),
                    "комментарий",
                )
            return
        owner_username = getattr(sender, "username", None)
        owner_display_name = _user_display_name(sender)
        if include_comment_links:
            stats["comment_refs"] += _collect_candidate_refs(
                candidates,
                getattr(message, "message", None),
                "комментарий",
                owner_username=owner_username,
                owner_display_name=owner_display_name,
            )
        if not include_profile_refs and gift_limit <= 0:
            return
        sender_id = getattr(sender, "id", None)
        if sender_id is not None:
            if sender_id in seen_sender_ids:
                return
            seen_sender_ids.add(sender_id)
        if include_profile_refs:
            if stats["profiles_seen"] >= profile_limit:
                stats["profiles_skipped_by_limit"] += 1
                include_profile_refs = False
            else:
                try:
                    input_user = cast(types.TypeInputUser, utils.get_input_user(sender))
                    full = await self._with_resilient_call(
                        lambda: self._client(functions.users.GetFullUserRequest(id=input_user))
                    )
                except FloodWaitError:
                    raise
                except (TypeError, ValueError, RPCError):
                    full = None

                if full is None:
                    include_profile_refs = False
                else:
                    stats["profiles_seen"] += 1
                    full_user = getattr(full, "full_user", None)
                    about = getattr(full_user, "about", None)
        profile_refs = 0
        bio_refs = 0
        personal_channel_refs = 0
        if include_profile_refs:
            bio_refs = _collect_candidate_refs(
                candidates,
                about,
                "bio комментатора",
                owner_username=owner_username,
                owner_display_name=owner_display_name,
            )
            personal_channel_refs = _collect_personal_channel_ref(
                candidates,
                full,
                owner_username=owner_username,
                owner_display_name=owner_display_name,
            )
        stats["bio_refs"] += bio_refs
        stats["personal_channel_refs"] += personal_channel_refs
        profile_refs += bio_refs + personal_channel_refs

        if (not include_profile_refs or profile_refs == 0) and gift_limit > 0 and len(candidates) < limit:
            stats["gift_refs"] += await self._collect_gift_refs(
                candidates,
                sender,
                gift_limit=gift_limit,
                stats=stats,
            )

    async def _collect_gift_refs(
        self,
        candidates: dict[str, CandidateSource],
        sender: types.User,
        *,
        gift_limit: int,
        stats: dict[str, int],
    ) -> int:
        stats["gift_profiles_checked"] += 1
        try:
            input_peer = cast(types.TypeInputPeer, utils.get_input_peer(sender))
            saved_gifts = await self._with_resilient_call(
                lambda: self._client(
                    functions.payments.GetSavedStarGiftsRequest(
                        peer=input_peer,
                        offset="",
                        limit=gift_limit,
                    )
                )
            )
        except FloodWaitError:
            raise
        except (TypeError, ValueError, RPCError):
            stats["gift_fetch_errors"] += 1
            return 0

        return _collect_gift_channel_refs(candidates, saved_gifts)

    async def _with_short_flood_retry(self, func, *args, **kwargs):
        return await self._with_resilient_call(func, *args, **kwargs)

    async def _ensure_connected(self) -> None:
        await self._pool.ensure_connected()

    async def _with_resilient_call(self, func, *args, attempts: int = 6, **kwargs):
        """Retry short FloodWait; on long FloodWait rotate to another account and retry.

        `func` should re-read the active client when possible (e.g. lambdas using self._client).
        """
        last_exc: BaseException | None = None
        switch_threshold = int(getattr(self._settings, "flood_switch_threshold_seconds", 60) or 60)
        sleep_limit = int(getattr(self._settings, "flood_sleep_limit_seconds", 60) or 60)
        try:
            pool_size = len(self._pool.list_info())
        except Exception:
            pool_size = 1
        max_attempts = max(attempts, pool_size + 2)

        for attempt in range(max_attempts):
            try:
                return await func(*args, **kwargs)
            except FloodWaitError as exc:
                last_exc = exc
                # Short wait — sleep on the same account.
                if exc.seconds <= sleep_limit and exc.seconds <= switch_threshold:
                    await asyncio.sleep(exc.seconds)
                    continue
                # Long wait — quarantine this account and switch.
                switched = await self._pool.mark_flood_and_rotate(
                    exc.seconds,
                    reason=f"FloodWait via {getattr(func, '__name__', type(func).__name__)}",
                )
                if switched:
                    continue
                wait = self._pool.seconds_until_any_available()
                if wait is not None and wait <= sleep_limit:
                    await asyncio.sleep(wait)
                    if await self._pool.rotate_to_healthy(force=True):
                        continue
                raise
            except (TimedOutError, ConnectionError, OSError, asyncio.TimeoutError) as exc:
                last_exc = exc
                await self._ensure_connected()
                await asyncio.sleep(min(2**attempt, 12))
            except RPCError as exc:
                name = type(exc).__name__
                if name in {"TimedOutError", "ServerError", "TimeoutError", "NetworkMigrateError"} or "timeout" in str(exc).lower():
                    last_exc = exc
                    await self._ensure_connected()
                    await asyncio.sleep(min(2**attempt, 12))
                    continue
                raise
        assert last_exc is not None
        raise last_exc

    async def inspect_channel_identifier(
        self, identifier: str, *, matched_query: str = "manual"
    ) -> ChannelReport:
        username = normalize_channel_identifier(identifier)
        try:
            entity = await self._with_short_flood_retry(
                lambda: self._client.get_entity(username)
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
            lambda: self.inspect_channel(entity, matched_query=matched_query)
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
        full = await self._with_resilient_call(
            lambda: self._client(
                functions.channels.GetFullChannelRequest(channel=input_channel)
            )
        )
        full_chat = full.full_chat

        description = getattr(full_chat, "about", "") or ""
        subscribers = getattr(full_chat, "participants_count", None) or getattr(
            channel, "participants_count", None
        )
        messages = await self._with_resilient_call(
            lambda: self._client.get_messages(channel, limit=self._settings.history_limit)
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

    async def _discussion_request(self, source: types.Channel, post: types.Message):
        return await self._with_resilient_call(
            lambda: self._client(
                functions.messages.GetDiscussionMessageRequest(peer=source, msg_id=post.id)
            )
        )


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


def extract_channel_references(text: str | None) -> list[str]:
    if not text:
        return []

    refs: list[str] = []
    for match in TELEGRAM_LINK_RE.finditer(text):
        ref = match.group(1)
        if ref not in refs:
            refs.append(ref)
    for match in USERNAME_RE.finditer(text):
        ref = match.group(1)
        if ref not in refs:
            refs.append(ref)
    return refs


def _collect_candidate_refs(
    candidates: dict[str, CandidateSource],
    text: str | None,
    source_label: str,
    *,
    owner_username: str | None = None,
    owner_display_name: str | None = None,
) -> int:
    added = 0
    for ref in extract_channel_references(text):
        if _add_candidate(
            candidates,
            ref,
            source_label,
            owner_username=owner_username,
            owner_display_name=owner_display_name,
        ):
            added += 1
    return added


def _collect_personal_channel_ref(
    candidates: dict[str, CandidateSource],
    full_user_result: object,
    *,
    owner_username: str | None = None,
    owner_display_name: str | None = None,
) -> int:
    full_user = getattr(full_user_result, "full_user", None)
    personal_channel_id = getattr(full_user, "personal_channel_id", None)
    if not personal_channel_id:
        return 0

    for chat in getattr(full_user_result, "chats", []) or []:
        if (
            isinstance(chat, types.Channel)
            and chat.id == personal_channel_id
            and _is_public_broadcast_channel(chat)
            and getattr(chat, "username", None)
        ):
            return (
                1
                if _add_candidate(
                    candidates,
                    chat.username,
                    "личный канал профиля",
                    owner_username=owner_username,
                    owner_display_name=owner_display_name,
                )
                else 0
            )
    return 0


def _collect_gift_channel_refs(candidates: dict[str, CandidateSource], saved_gifts_result: object) -> int:
    channels_by_id = {
        chat.id: chat
        for chat in getattr(saved_gifts_result, "chats", []) or []
        if isinstance(chat, types.Channel)
    }
    added = 0
    for gift in getattr(saved_gifts_result, "gifts", []) or []:
        peer_id = _peer_id_value(getattr(gift, "from_id", None))
        channel = channels_by_id.get(peer_id)
        if channel is None or not _is_public_broadcast_channel(channel):
            continue
        if _add_candidate(candidates, getattr(channel, "username", None), "подарок от канала"):
            added += 1
    return added


def _add_candidate(
    candidates: dict[str, CandidateSource],
    ref: str | None,
    source_label: str,
    *,
    owner_username: str | None = None,
    owner_display_name: str | None = None,
) -> bool:
    if not ref:
        return False
    if ref in candidates:
        source = candidates[ref]
        if owner_username and not source.owner_username:
            source.owner_username = owner_username
        if owner_display_name and not source.owner_display_name:
            source.owner_display_name = owner_display_name
        return False
    candidates[ref] = CandidateSource(source_label, owner_username, owner_display_name)
    return True


def _apply_report_owner(report: ChannelReport, source: CandidateSource) -> None:
    if source.owner_username and not report.owner_username:
        report.owner_username = source.owner_username
    if source.owner_display_name and not report.owner_display_name:
        report.owner_display_name = source.owner_display_name


def _user_display_name(user: types.User) -> str | None:
    parts = [
        getattr(user, "first_name", None),
        getattr(user, "last_name", None),
    ]
    name = " ".join(part.strip() for part in parts if isinstance(part, str) and part.strip())
    if name:
        return name
    username = getattr(user, "username", None)
    return f"@{username}" if username else None


def _should_stop(callback) -> bool:
    return bool(callback and callback())


async def _notify_progress(callback, stats: dict[str, int]) -> None:
    if callback is None:
        return
    result = callback(dict(stats))
    if inspect.isawaitable(result):
        await result


def _peer_id_value(peer: object) -> int | None:
    if isinstance(peer, types.PeerChannel):
        return peer.channel_id
    if isinstance(peer, types.PeerChat):
        return peer.chat_id
    if isinstance(peer, types.PeerUser):
        return peer.user_id
    return None


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
