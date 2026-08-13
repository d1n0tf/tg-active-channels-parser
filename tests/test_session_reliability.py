from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from ChannelsParser.accounts import AccountPool, _AccountSlot, _SessionProcessLock
from ChannelsParser.config import AppSettings


def _settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AppSettings:
    monkeypatch.setenv("BOT_TOKEN", "1:test")
    monkeypatch.setenv("TELEGRAM_API_ID", "123")
    monkeypatch.setenv("TELEGRAM_API_HASH", "hash")
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "db.sqlite3"))
    monkeypatch.setenv("TELEGRAM_SESSION", str(tmp_path / "legacy.session"))
    monkeypatch.setenv("TELEGRAM_SESSIONS_DIR", str(tmp_path / "sessions"))
    monkeypatch.delenv("ADMIN_USER_IDS", raising=False)
    return AppSettings.from_env(require_bot_token=True)


def test_session_process_lock_rejects_second_holder(tmp_path: Path) -> None:
    path = tmp_path / "acc.session"
    first = _SessionProcessLock(path)
    second = _SessionProcessLock(path)
    first.acquire()
    try:
        with pytest.raises(RuntimeError, match="already in use"):
            second.acquire()
    finally:
        first.release()
    second.acquire()
    second.release()


def test_health_check_records_success(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings(tmp_path, monkeypatch)
    pool = AccountPool.from_settings(settings)

    class Client:
        def is_connected(self):
            return True

        async def is_user_authorized(self):
            return True

        async def get_me(self):
            return SimpleNamespace(id=1)

    slot = _AccountSlot(account_id="a", session_path=tmp_path / "a.session", label="a")
    slot.client = Client()  # type: ignore[assignment]
    pool._slots = [slot]
    pool._active_id = "a"

    asyncio.run(pool.check_health())

    assert slot.last_checked_at is not None
    assert slot.consecutive_health_failures == 0
    assert slot.enabled is True


def test_health_check_disables_unauthorized_session(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings(tmp_path, monkeypatch)
    pool = AccountPool.from_settings(settings)

    class Client:
        def is_connected(self):
            return True

        async def is_user_authorized(self):
            return False

        async def disconnect(self):
            return None

    slot = _AccountSlot(account_id="a", session_path=tmp_path / "a.session", label="a")
    slot.client = Client()  # type: ignore[assignment]
    pool._slots = [slot]
    pool._active_id = "a"

    asyncio.run(pool.check_health())

    assert slot.enabled is False
    assert slot.client is None
    assert "session must be re-authorized" in (slot.last_error or "")


def test_health_check_quarantines_repeated_transient_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(tmp_path, monkeypatch)
    pool = AccountPool.from_settings(settings)

    class Client:
        def is_connected(self):
            return True

        async def is_user_authorized(self):
            return True

        async def get_me(self):
            raise OSError("route down")

    slot = _AccountSlot(account_id="a", session_path=tmp_path / "a.session", label="a")
    slot.client = Client()  # type: ignore[assignment]
    pool._slots = [slot]

    asyncio.run(pool.check_health())
    assert slot.consecutive_health_failures == 1
    assert slot.cooldown_until is None
    asyncio.run(pool.check_health())
    assert slot.consecutive_health_failures == 2
    assert slot.cooldown_until is not None
    assert slot.cooldown_until > datetime.now(timezone.utc)
