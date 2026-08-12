from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import Sequence

from telethon.errors import (
    AuthKeyError,
    FloodWaitError,
    RPCError,
    ServerError,
    UnauthorizedError,
)
from telethon.errors.common import AuthKeyNotFound


_TECHNICAL_MARKERS = (
    "rpc",
    "telethon",
    "caused by",
    "traceback",
    "sqlite",
    "database",
    "timeout",
    "connection",
    "servererror",
    "authkey",
    "sessionrevoked",
    "sessionexpired",
)

_SESSION_MARKERS = (
    "authkey",
    "unauthorized",
    "sessionrevoked",
    "sessionexpired",
    "userdeactivated",
    "auth key",
)


def operation_error_text(exc: BaseException) -> str:
    """Return a short, safe explanation suitable for a customer-facing chat."""
    if isinstance(exc, ValueError):
        text = _clean(str(exc))
        if _looks_like_public_input_error(text):
            return text

    if isinstance(exc, FloodWaitError):
        return "Telegram временно ограничил запросы. Попробуй ещё раз чуть позже."
    if isinstance(exc, (AuthKeyNotFound, AuthKeyError, UnauthorizedError)):
        return "Один Telegram-аккаунт парсера переподключается. Попробуй ещё раз через минуту."
    if isinstance(exc, (ServerError, RPCError, ConnectionError, OSError, asyncio.TimeoutError)):
        return "Telegram временно не отвечает. Подключение уже восстанавливается — попробуй ещё раз через минуту."
    if isinstance(exc, sqlite3.Error):
        return "База результатов временно занята. Попробуй повторить запрос через несколько секунд."
    return "Не удалось завершить запрос из-за временной технической ошибки. Попробуй ещё раз."


def scan_errors_text(errors: Sequence[str], *, limit: int = 3) -> str:
    """Compress collector diagnostics into user-safe scan notes."""
    notes: list[str] = []
    for error in errors:
        note = _scan_error_note(error)
        if note and note not in notes:
            notes.append(note)
        if len(notes) >= limit:
            break
    return "; ".join(notes)


def stored_scan_error_text(error: str | None) -> str:
    """Redact raw persisted exception text before it reaches scan history."""
    if not error:
        return ""
    text = _clean(error)
    if _looks_like_public_input_error(text):
        return text
    return "Технический сбой. Запусти скан повторно."


def _scan_error_note(raw: str) -> str:
    text = _clean(raw)
    lower = text.lower()
    if not text:
        return ""
    if "остановлено пользователем" in lower:
        return "Скан остановлен пользователем"
    if "обход постов завершён досрочно" in lower:
        return "Обход постов завершён досрочно"
    if "достигнут лимит кандидатов" in lower:
        return "Достигнут лимит кандидатов"
    if "flood" in lower:
        return "Telegram временно ограничил часть запросов"
    if any(marker in lower for marker in _SESSION_MARKERS):
        return "Один аккаунт парсера переподключается; часть запросов пропущена"
    if any(marker in lower for marker in _TECHNICAL_MARKERS):
        return "Telegram временно не ответил на часть запросов"
    if any(marker in lower for marker in ("username", "channelinvalid", "valueerror")):
        return "Часть ссылок оказалась недоступна"
    return "Часть каналов не удалось проверить"


def _looks_like_public_input_error(text: str) -> bool:
    if not text or len(text) > 300:
        return False
    lower = text.lower()
    if any(marker in lower for marker in _TECHNICAL_MARKERS):
        return False
    return any("а" <= char.lower() <= "я" or char.lower() == "ё" for char in text)


def _clean(value: str) -> str:
    return " ".join(value.split())
