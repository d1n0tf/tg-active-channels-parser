from __future__ import annotations

import argparse
import asyncio
import uuid
from pathlib import Path

from ChannelsParser.collector import TelegramChannelCollector
from ChannelsParser.commands import parse_queries
from ChannelsParser.config import AppSettings, ConfigError, database_path_from_env
from ChannelsParser.formatting import (
    format_discovery_stats,
    format_report,
    format_reports,
    format_scan_done,
    format_scan_history,
    reports_to_csv,
)
from ChannelsParser.models import SearchFilters
from ChannelsParser.storage import ChannelStorage


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    try:
        asyncio.run(run(args))
    except (ConfigError, RuntimeError, ValueError) as exc:
        raise SystemExit(str(exc)) from exc


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Find and audit active Telegram channels for ad buying research")
    subparsers = parser.add_subparsers(dest="command", required=True)

    search = subparsers.add_parser("search", help="search public channels")
    search.add_argument("queries", nargs="*", help="one or more queries; commas inside an argument are also supported")
    add_filter_args(search)
    search.add_argument("--limit", type=int, default=20, help="how many reports to print")
    search.add_argument("--csv", type=Path, help="write all matched reports to CSV")

    discover = subparsers.add_parser("discover", help="discover channels from comments of a source channel")
    discover.add_argument("channel", help="@username or https://t.me/username")
    discover.add_argument("--posts", type=int, default=100, help="how many latest source posts to inspect")
    discover.add_argument("--comments-per-post", type=int, default=100, help="max comments to inspect per post")
    discover.add_argument("--profile-limit", type=int, default=500, help="max commenter profiles to inspect")
    discover.add_argument("--candidate-limit", type=int, default=300, help="max candidate channels to inspect")
    discover.add_argument("--gift-limit", type=int, default=10, help="max public gifts to inspect per profile; 0 disables")
    add_filter_args(discover, subs_min_default=0, subs_max_default=0, channel_kind_default="any", audience_default="any")
    discover.add_argument("--limit", type=int, default=20, help="how many reports to print")
    discover.add_argument("--csv", type=Path, help="write all matched reports to CSV")

    check = subparsers.add_parser("check", help="inspect one public channel")
    check.add_argument("channel", help="@username or https://t.me/username")
    check.add_argument("--csv", type=Path, help="write report to CSV")

    history = subparsers.add_parser("history", help="show saved scan history")
    history.add_argument("--limit", type=int, default=20)

    return parser


def add_filter_args(
    parser: argparse.ArgumentParser,
    *,
    subs_min_default: int = 1000,
    subs_max_default: int = 50000,
    channel_kind_default: str = "thematic",
    audience_default: str = "female",
) -> None:
    parser.add_argument("--subs-min", type=int, default=subs_min_default, help="minimum subscribers; 0 disables")
    parser.add_argument("--subs-max", type=int, default=subs_max_default, help="maximum subscribers; 0 disables")
    parser.add_argument("--days", type=int, default=7, help="max days since last post")
    parser.add_argument("--views-min", type=int)
    parser.add_argument("--score-min", type=float, default=35)
    parser.add_argument("--channel-kind", choices=["any", "thematic", "commercial"], default=channel_kind_default)
    parser.add_argument("--audience", choices=["any", "female", "male"], default=audience_default)
    parser.add_argument("--age", choices=["any", "14-17", "18-24", "25-34", "35+"], default="any")
    parser.add_argument("--sort", choices=["score", "views", "subscribers", "fresh", "reactions", "comments"], default="score")


async def run(args: argparse.Namespace) -> None:
    if args.command == "history":
        if args.limit < 1:
            raise ValueError("--limit must be >= 1")
        storage = ChannelStorage(database_path_from_env())
        storage.init()
        print(format_scan_history(storage.list_scans(limit=args.limit)))
        return

    settings = AppSettings.from_env(require_bot_token=False)
    storage = ChannelStorage(settings.database_path)
    storage.init()
    recovered = storage.recover_interrupted_scans()
    if recovered:
        print(f"Помечено прерванных сканов после прошлого запуска: {recovered}")

    collector = TelegramChannelCollector(settings)
    await collector.connect()
    try:
        if args.command == "search":
            await run_search(args, collector, storage)
        elif args.command == "discover":
            await run_discover(args, collector, storage)
        elif args.command == "check":
            await run_check(args, collector, storage)
    finally:
        await collector.close()


async def run_search(args: argparse.Namespace, collector: TelegramChannelCollector, storage: ChannelStorage) -> None:
    queries = parse_cli_queries(args.queries)
    if not queries:
        raise ValueError("Provide at least one query, for example: search 'семейный бюджет, финансы для женщин'")

    _validate_search_args(args)
    filters = SearchFilters(
        min_subscribers=_none_if_non_positive(args.subs_min),
        max_subscribers=_none_if_non_positive(args.subs_max),
        max_last_post_days=args.days,
        min_activity_score=args.score_min,
        min_avg_views=args.views_min,
        channel_kind=args.channel_kind,
        audience_bias=args.audience,
        age_group=args.age,
        sort_by=args.sort,
    )
    scan_id = uuid.uuid4().hex
    storage.create_scan(scan_id, user_id=None, mode="search", queries=queries, filters=filters)

    try:
        result = await collector.search_channels(queries, filters)
        storage.save_reports(scan_id, result.reports)
        storage.finish_scan(scan_id, total_candidates=result.total_candidates, total_reports=len(result.reports))
    except Exception as exc:
        storage.fail_scan(scan_id, error=str(exc))
        raise

    print(format_scan_done(scan_id, result.total_candidates, len(result.reports), result.errors))
    print()
    print(format_reports(result.reports, limit=args.limit))

    if args.csv:
        args.csv.parent.mkdir(parents=True, exist_ok=True)
        args.csv.write_bytes(reports_to_csv(result.reports))
        print(f"\nCSV saved: {args.csv}")


async def run_discover(args: argparse.Namespace, collector: TelegramChannelCollector, storage: ChannelStorage) -> None:
    _validate_discover_args(args)
    filters = SearchFilters(
        min_subscribers=_none_if_non_positive(args.subs_min),
        max_subscribers=_none_if_non_positive(args.subs_max),
        max_last_post_days=args.days,
        min_activity_score=args.score_min,
        min_avg_views=args.views_min,
        channel_kind=args.channel_kind,
        audience_bias=args.audience,
        age_group=args.age,
        sort_by=args.sort,
    )
    scan_id = uuid.uuid4().hex
    queries = [args.channel, f"posts:{args.posts}"]
    storage.create_scan(scan_id, user_id=None, mode="discover", queries=queries, filters=filters)

    try:
        result = await collector.discover_channels_from_comments(
            args.channel,
            filters,
            post_limit=args.posts,
            comments_per_post=args.comments_per_post,
            profile_limit=args.profile_limit,
            candidate_limit=args.candidate_limit,
            gift_limit=args.gift_limit,
        )
        storage.save_reports(scan_id, result.reports)
        storage.finish_scan(scan_id, total_candidates=result.total_candidates, total_reports=len(result.reports))
    except Exception as exc:
        storage.fail_scan(scan_id, error=str(exc))
        raise

    summary = format_scan_done(scan_id, result.total_candidates, len(result.reports), result.errors)
    discovery_stats = format_discovery_stats(result)
    if discovery_stats:
        summary = f"{summary}\n\n{discovery_stats}"
    print(summary)
    print()
    print(format_reports(result.reports, limit=args.limit))

    if args.csv:
        args.csv.parent.mkdir(parents=True, exist_ok=True)
        args.csv.write_bytes(reports_to_csv(result.reports))
        print(f"\nCSV saved: {args.csv}")


async def run_check(args: argparse.Namespace, collector: TelegramChannelCollector, storage: ChannelStorage) -> None:
    filters = SearchFilters(min_subscribers=None, max_subscribers=None, audience_bias="any", min_activity_score=0)
    scan_id = uuid.uuid4().hex
    storage.create_scan(scan_id, user_id=None, mode="audit", queries=[args.channel], filters=filters)

    try:
        report = await collector.inspect_channel_identifier(args.channel)
        storage.save_reports(scan_id, [report])
        storage.finish_scan(scan_id, total_candidates=1, total_reports=1)
    except Exception as exc:
        storage.fail_scan(scan_id, error=str(exc), total_candidates=1)
        raise

    print(f"scan_id: {scan_id[:8]}\n")
    print(format_report(report))

    if args.csv:
        args.csv.parent.mkdir(parents=True, exist_ok=True)
        args.csv.write_bytes(reports_to_csv([report]))
        print(f"\nCSV saved: {args.csv}")


def parse_cli_queries(values: list[str]) -> list[str]:
    queries: list[str] = []
    for value in values:
        for query in parse_queries(value):
            if query not in queries:
                queries.append(query)
    return queries


def _none_if_non_positive(value: int | None) -> int | None:
    if value is None or value <= 0:
        return None
    return value


def _validate_search_args(args: argparse.Namespace) -> None:
    if args.subs_min < 0 or args.subs_max < 0:
        raise ValueError("--subs-min and --subs-max must be >= 0")
    if args.subs_min > 0 and args.subs_max > 0 and args.subs_min > args.subs_max:
        raise ValueError("--subs-min cannot be greater than --subs-max")
    if args.days < 1 or args.days > 60:
        raise ValueError("--days must be between 1 and 60")
    if args.views_min is not None and args.views_min < 0:
        raise ValueError("--views-min must be >= 0")
    if args.score_min < 0 or args.score_min > 100:
        raise ValueError("--score-min must be between 0 and 100")
    if args.limit < 1:
        raise ValueError("--limit must be >= 1")


def _validate_discover_args(args: argparse.Namespace) -> None:
    _validate_search_args(args)
    if args.posts < 1 or args.posts > 500:
        raise ValueError("--posts must be between 1 and 500")
    if args.comments_per_post < 1:
        raise ValueError("--comments-per-post must be >= 1")
    if args.profile_limit < 1:
        raise ValueError("--profile-limit must be >= 1")
    if args.candidate_limit < 1:
        raise ValueError("--candidate-limit must be >= 1")
    if args.gift_limit < 0:
        raise ValueError("--gift-limit must be >= 0")


if __name__ == "__main__":
    main()
