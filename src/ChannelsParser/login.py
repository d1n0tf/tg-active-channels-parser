from __future__ import annotations

import asyncio
import inspect

from telethon import TelegramClient

from ChannelsParser.config import AppSettings, ConfigError


async def login() -> None:
    settings = AppSettings.from_env(require_bot_token=False)
    phone = settings.telegram_phone
    if phone is None:
        raise ConfigError("TELEGRAM_PHONE is required for login")

    client = TelegramClient(
        settings.telegram_session,
        settings.telegram_api_id,
        settings.telegram_api_hash,
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
    print(f"Telegram session is ready: {username}")
    disconnect_result: object = client.disconnect()
    if inspect.isawaitable(disconnect_result):
        await disconnect_result


def main() -> None:
    try:
        asyncio.run(login())
    except ConfigError as exc:
        raise SystemExit(str(exc)) from exc


if __name__ == "__main__":
    main()
