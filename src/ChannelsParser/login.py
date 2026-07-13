from __future__ import annotations

import argparse
import asyncio
import inspect
from pathlib import Path

from telethon import TelegramClient

from ChannelsParser.accounts import AccountPool, session_path_for, validate_account_id
from ChannelsParser.config import AppSettings, ConfigError
from ChannelsParser.proxy import telethon_proxy


async def login(*, account_id: str, phone: str | None) -> None:
    settings = AppSettings.from_env(require_bot_token=False)
    account_id = validate_account_id(account_id)
    phone = phone or settings.telegram_phone
    if phone is None:
        raise ConfigError("Укажи телефон: --phone +79... или TELEGRAM_PHONE в .env")

    path = session_path_for(settings, account_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    session = str(path)
    if session.endswith(".session"):
        session = session[: -len(".session")]

    client = TelegramClient(
        session,
        settings.telegram_api_id,
        settings.telegram_api_hash,
        proxy=telethon_proxy(settings.telegram_proxy_url),
    )
    start_result: object = client.start(phone=phone)
    if inspect.isawaitable(start_result):
        await start_result
    me = await client.get_me()
    username = (
        getattr(me, "username", None)
        or getattr(me, "phone", None)
        or getattr(me, "id", "unknown")
    )
    print(f"OK: аккаунт '{account_id}' готов → {username}")
    print(f"    session: {path}")
    disconnect_result: object = client.disconnect()
    if inspect.isawaitable(disconnect_result):
        await disconnect_result

    # Refresh pool registry so DB knows about the new session
    pool = AccountPool.from_settings(settings)
    print(f"Всего аккаунтов в пуле: {len(pool.list_info())}")
    for info in pool.list_info():
        mark = "*" if info.account_id == account_id else " "
        print(f"  {mark} {info.account_id} · {info.label} · {info.session_path}")


async def list_accounts() -> None:
    settings = AppSettings.from_env(require_bot_token=False)
    pool = AccountPool.from_settings(settings)
    infos = pool.list_info()
    if not infos:
        print("Аккаунтов нет. Добавь: uv run tg-active-channels-login --name acc1 --phone +79...")
        return
    print(f"Аккаунты парсера ({len(infos)}):")
    for info in infos:
        print(
            f"  · {info.account_id} · {info.label} · {info.status_label}\n"
            f"      {info.session_path}"
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Авторизация user-аккаунта Telethon для пула парсера"
    )
    parser.add_argument(
        "--name",
        default="default",
        help="Имя аккаунта в пуле (default, acc1, work, ...)",
    )
    parser.add_argument("--phone", default=None, help="Номер телефона (+79...)")
    parser.add_argument(
        "--list",
        action="store_true",
        help="Показать зарегистрированные аккаунты и выйти",
    )
    return parser


def main() -> None:
    args = build_parser().parse_args()
    try:
        if args.list:
            asyncio.run(list_accounts())
            return
        asyncio.run(login(account_id=args.name, phone=args.phone))
    except (ConfigError, ValueError) as exc:
        raise SystemExit(str(exc)) from exc


if __name__ == "__main__":
    main()
