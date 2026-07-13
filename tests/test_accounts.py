from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from ChannelsParser.accounts import AccountPool, validate_account_id
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


def test_validate_account_id() -> None:
    assert validate_account_id("acc1") == "acc1"
    with pytest.raises(ValueError):
        validate_account_id("bad name!")


def test_pool_discovers_named_sessions(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings(tmp_path, monkeypatch)
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    (sessions / "work.session").write_bytes(b"")
    (sessions / "spare.session").write_bytes(b"")

    pool = AccountPool.from_settings(settings)
    ids = {info.account_id for info in pool.list_info()}
    assert "default" in ids
    assert "work" in ids
    assert "spare" in ids


def test_mark_flood_and_rotate_cools_account(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings(tmp_path, monkeypatch)
    pool = AccountPool.from_settings(settings)

    # Fake two authorized slots without real Telethon connect
    from ChannelsParser.accounts import _AccountSlot

    a = _AccountSlot(account_id="a", session_path=tmp_path / "a.session", label="a")
    b = _AccountSlot(account_id="b", session_path=tmp_path / "b.session", label="b")
    a.client = object()  # type: ignore[assignment]
    b.client = object()  # type: ignore[assignment]
    pool._slots = [a, b]
    pool._active_id = "a"

    import asyncio

    async def run() -> None:
        ok = await pool.mark_flood_and_rotate(3600, reason="test")
        assert ok is True
        assert pool._active_id == "b"
        assert a.cooldown_until is not None
        assert a.cooldown_until > datetime.now(timezone.utc)
        assert a.total_flood_waits == 1

        # Cool both → no healthy
        await pool.mark_flood_and_rotate(7200, reason="test")
        assert pool.healthy_slots() == []
        assert pool.seconds_until_any_available() is not None

    asyncio.run(run())
