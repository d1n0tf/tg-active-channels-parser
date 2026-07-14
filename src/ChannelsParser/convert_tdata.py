from __future__ import annotations

import argparse
import asyncio
import re
from pathlib import Path

from ChannelsParser.accounts import validate_account_id
from ChannelsParser.config import AppSettings, ConfigError


def _looks_like_tdata(path: Path) -> bool:
    if not path.is_dir():
        return False
    # Classic Telegram Desktop tdata markers
    markers = ("key_datas", "key_data", "settingss", "map0", "map1")
    if any((path / name).exists() for name in markers):
        return True
    # Some dumps only have hashed key files
    return any(path.glob("key_data*")) or any(path.glob("map*"))


def discover_tdata_folders(base: Path) -> list[tuple[str, Path]]:
    """Find (account_id, tdata_path) under data/tdata/.

    Supported layouts:
      data/tdata/acc1/          — folder is tdata itself
      data/tdata/acc1/tdata/    — nested tdata
      data/tdata/tdata/         — single dump named tdata → account_id=tdata
    """
    if not base.exists():
        return []

    found: list[tuple[str, Path]] = []
    seen_paths: set[Path] = set()

    def add(account_id: str, tdata_path: Path) -> None:
        resolved = tdata_path.resolve()
        if resolved in seen_paths:
            return
        try:
            account_id = validate_account_id(_slugify(account_id))
        except ValueError:
            account_id = validate_account_id(_slugify(f"td_{account_id}")[:48])
        seen_paths.add(resolved)
        found.append((account_id, resolved))

    if _looks_like_tdata(base):
        add(base.name if base.name != "tdata" else "from_tdata", base)

    for child in sorted(base.iterdir()):
        if not child.is_dir():
            continue
        if _looks_like_tdata(child):
            add(child.name, child)
            continue
        nested = child / "tdata"
        if nested.is_dir() and _looks_like_tdata(nested):
            add(child.name, nested)

    return found


def _slugify(name: str) -> str:
    name = name.strip().replace(" ", "_")
    name = re.sub(r"[^a-zA-Z0-9_\-]+", "_", name)
    name = re.sub(r"_+", "_", name).strip("_-")
    if not name:
        name = "account"
    if not re.match(r"^[a-zA-Z0-9]", name):
        name = f"a_{name}"
    return name[:48]


async def convert_one(
    tdata_path: Path,
    session_base: Path,
    *,
    passcode: str | None = None,
    proxy: object | None = None,
) -> list[Path]:
    """Convert one tdata folder → one or more .session files. Returns session paths."""
    try:
        from opentele.td import TDesktop
        from opentele.api import API, UseCurrentSession
        from opentele.exception import OpenTeleException
    except ImportError as exc:
        raise RuntimeError(
            "Нужен пакет opentele-ng: uv add opentele-ng"
        ) from exc

    try:
        tdesk = TDesktop(str(tdata_path), api=API.TelegramDesktop, passcode=passcode or "")
    except OpenTeleException as exc:
        raise RuntimeError(f"Не удалось открыть tdata: {exc}") from exc

    if not tdesk.isLoaded() or tdesk.accountsCount <= 0:
        raise RuntimeError("tdata пустой или не прочитался (passcode? битый dump?)")

    written: list[Path] = []
    accounts = list(getattr(tdesk, "accounts", []) or [])
    if not accounts and tdesk.mainAccount is not None:
        accounts = [tdesk.mainAccount]

    client_kwargs: dict = {}
    if proxy is not None:
        client_kwargs["proxy"] = proxy

    for index, account in enumerate(accounts):
        suffix = "" if index == 0 else f"_{index + 1}"
        session_path = session_base.parent / f"{session_base.name}{suffix}"
        # Telethon session path without .session extension
        session_str = str(session_path)
        if session_str.endswith(".session"):
            session_str = session_str[: -len(".session")]

        session_path.parent.mkdir(parents=True, exist_ok=True)
        # Remove old session to avoid merge junk
        for stale in (
            Path(session_str + ".session"),
            Path(session_str + ".session-journal"),
        ):
            if stale.exists():
                stale.unlink()

        try:
            from opentele.tl import TelegramClient as OTClient
            from telethon.errors import (
                AuthKeyDuplicatedError,
                AuthKeyError,
                SessionPasswordNeededError,
                SessionRevokedError,
                UserDeactivatedBanError,
                UserDeactivatedError,
            )

            client = await OTClient.FromTDesktop(
                account,
                session=session_str,
                flag=UseCurrentSession,
                api=API.TelegramDesktop,
                **client_kwargs,
            )
            await client.connect()
            # is_user_authorized() alone is not enough: auth_key may exist but be revoked.
            try:
                from telethon.tl.functions.users import GetUsersRequest
                from telethon.tl.types import InputUserSelf

                users = await client(GetUsersRequest([InputUserSelf()]))
                me = users[0] if users else None
            except SessionRevokedError as exc:
                await client.disconnect()
                raise RuntimeError(
                    "tdata/session отозвана (SessionRevokedError): на аккаунте сбросили "
                    "сессии («Завершить все сеансы») или ключ уже недействителен. "
                    "Конвертер тут ни при чём — нужен свежий tdata / новый логин."
                ) from exc
            except (AuthKeyError, AuthKeyDuplicatedError) as exc:
                await client.disconnect()
                raise RuntimeError(
                    f"ключ авторизации битый/невалидный ({type(exc).__name__}). "
                    "Часто битый dump tdata."
                ) from exc
            except (UserDeactivatedBanError, UserDeactivatedError) as exc:
                await client.disconnect()
                raise RuntimeError(
                    f"аккаунт заблокирован/деактивирован ({type(exc).__name__})"
                ) from exc
            except SessionPasswordNeededError as exc:
                await client.disconnect()
                raise RuntimeError(
                    "нужен облачный 2FA-пароль (не local passcode tdata). "
                    "Перелогинь аккаунт через login CLI."
                ) from exc
            except Exception as exc:
                # Fall back to get_me / authorized flag with richer message
                me = await client.get_me()
                if me is None and not await client.is_user_authorized():
                    await client.disconnect()
                    raise RuntimeError(
                        f"сессия не авторизована после конвертации "
                        f"({type(exc).__name__}: {exc}). "
                        "Частые причины: отозванный tdata, неполный dump, бан."
                    ) from exc

            if me is None:
                me = await client.get_me()
            if me is None:
                await client.disconnect()
                raise RuntimeError(
                    "сессия после конвертации не авторизована "
                    "(get_me=None). Обычно tdata уже невалидна."
                )
            label = (
                f"@{me.username}"
                if getattr(me, "username", None)
                else str(getattr(me, "id", "?"))
            )
            await client.disconnect()
        except OpenTeleException as exc:
            raise RuntimeError(f"конвертация: {exc}") from exc

        out = Path(session_str + ".session")
        if not out.exists():
            raise RuntimeError(f"файл сессии не создан: {out}")
        written.append(out)
        print(f"  OK  {out.name}  ←  {tdata_path}  ({label})")

    return written


async def convert_all(
    *,
    tdata_dir: Path,
    sessions_dir: Path,
    passcode: str | None = None,
    only: str | None = None,
    proxy: object | None = None,
    proxy_url: str | None = None,
) -> int:
    items = discover_tdata_folders(tdata_dir)
    if only:
        only_slug = _slugify(only)
        items = [(aid, p) for aid, p in items if aid == only_slug or p.name == only]

    if not items:
        print(f"Не найдено tdata в {tdata_dir}")
        print("Ожидается:")
        print(f"  {tdata_dir}/acc1/          # сама tdata")
        print(f"  {tdata_dir}/acc1/tdata/    # или вложенная")
        return 1

    sessions_dir.mkdir(parents=True, exist_ok=True)
    print(f"Найдено tdata: {len(items)}")
    print(f"Выход: {sessions_dir}")
    if proxy_url:
        print(f"Прокси: {proxy_url}")
    elif proxy is not None:
        print("Прокси: задан")
    else:
        print("Прокси: нет (TELEGRAM_PROXY_URL / PROXY_URL)")
    ok = 0
    fail = 0
    for account_id, tdata_path in items:
        session_base = sessions_dir / account_id
        print(f"\n→ {account_id}  ({tdata_path})")
        try:
            await convert_one(
                tdata_path,
                session_base,
                passcode=passcode,
                proxy=proxy,
            )
            ok += 1
        except Exception as exc:
            fail += 1
            print(f"  FAIL  {exc}")

    print(f"\nГотово: ok={ok}, fail={fail}")
    if ok:
        print("Перезапусти бота или: uv run tg-active-channels-login --list")
    return 0 if fail == 0 else 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Конвертер Telegram Desktop tdata → Telethon .session для пула парсера"
    )
    parser.add_argument(
        "--tdata-dir",
        default=None,
        help="Папка с tdata-дампами (default: data/tdata)",
    )
    parser.add_argument(
        "--sessions-dir",
        default=None,
        help="Куда писать .session (default: TELEGRAM_SESSIONS_DIR / data/sessions)",
    )
    parser.add_argument(
        "--passcode",
        default=None,
        help="Local passcode tdata, если стоит пароль на Telegram Desktop",
    )
    parser.add_argument(
        "--only",
        default=None,
        help="Конвертировать только одну папку по имени",
    )
    return parser


def main() -> None:
    args = build_parser().parse_args()
    try:
        settings = AppSettings.from_env(require_bot_token=False)
    except ConfigError as exc:
        # Allow running with minimal env for conversion only
        print(f"Предупреждение config: {exc}")
        settings = None

    tdata_dir = Path(args.tdata_dir) if args.tdata_dir else Path("data/tdata")
    if args.sessions_dir:
        sessions_dir = Path(args.sessions_dir)
    elif settings is not None:
        sessions_dir = settings.telegram_sessions_dir
    else:
        sessions_dir = Path("data/sessions")

    proxy = None
    proxy_url = None
    if settings is not None:
        from ChannelsParser.proxy import telethon_proxy

        proxy_url = settings.telegram_proxy_url
        proxy = telethon_proxy(proxy_url)

    raise SystemExit(
        asyncio.run(
            convert_all(
                tdata_dir=tdata_dir,
                sessions_dir=sessions_dir,
                passcode=args.passcode,
                only=args.only,
                proxy=proxy,
                proxy_url=proxy_url,
            )
        )
    )


if __name__ == "__main__":
    main()
