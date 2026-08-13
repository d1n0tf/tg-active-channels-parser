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
    assert "default" not in ids
    assert "work" in ids
    assert "spare" in ids


def test_pool_does_not_register_missing_legacy_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(tmp_path, monkeypatch)
    pool = AccountPool.from_settings(settings)

    assert pool.list_info() == []
    with pool._connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM parser_accounts").fetchone()[0] == 0


def test_pool_removes_stale_database_accounts_after_session_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(tmp_path, monkeypatch)
    sessions = settings.telegram_sessions_dir
    sessions.mkdir()
    old = sessions / "old.session"
    old.write_bytes(b"old")
    AccountPool.from_settings(settings)

    old.unlink()
    fresh = sessions / "fresh.session"
    fresh.write_bytes(b"fresh")
    pool = AccountPool.from_settings(settings)

    assert [info.account_id for info in pool.list_info()] == ["fresh"]
    with pool._connect() as connection:
        ids = [row[0] for row in connection.execute("SELECT account_id FROM parser_accounts")]
    assert ids == ["fresh"]


def test_mark_flood_and_rotate_cools_account(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings(tmp_path, monkeypatch)
    pool = AccountPool.from_settings(settings)

    from ChannelsParser.accounts import _AccountSlot

    a = _AccountSlot(account_id="a", session_path=tmp_path / "a.session", label="a")
    b = _AccountSlot(account_id="b", session_path=tmp_path / "b.session", label="b")
    a.client = object()  # type: ignore[assignment]
    b.client = object()  # type: ignore[assignment]
    pool._slots = [a, b]
    pool._active_id = "a"

    import asyncio

    async def run() -> None:
        lease = await pool.acquire()
        assert lease.account_id == "a"
        token = pool.bind_lease(lease)
        try:
            ok = await pool.mark_flood_and_rotate(3600, reason="test")
            assert ok is True
            assert lease.account_id == "b"
            assert a.cooldown_until is not None
            assert a.total_flood_waits == 1
            assert pool.free_account_count() == 0  # b leased
        finally:
            pool.unbind_lease(token)
            await pool.release(lease)

        assert pool.free_account_count() == 1  # only b healthy free

        lease2 = await pool.acquire()
        token2 = pool.bind_lease(lease2)
        try:
            ok = await pool.mark_flood_and_rotate(7200, reason="test")
            assert ok is False
            assert pool.healthy_slots() == []
        finally:
            pool.unbind_lease(token2)
            await pool.release(lease2)

    asyncio.run(run())


def test_rotate_lease_to_healthy_after_transient_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(tmp_path, monkeypatch)
    pool = AccountPool.from_settings(settings)
    from ChannelsParser.accounts import _AccountSlot

    a = _AccountSlot(account_id="a", session_path=tmp_path / "a.session", label="a")
    b = _AccountSlot(account_id="b", session_path=tmp_path / "b.session", label="b")
    a.client = object()  # type: ignore[assignment]
    b.client = object()  # type: ignore[assignment]
    pool._slots = [a, b]
    pool._active_id = "a"

    import asyncio

    async def run() -> None:
        lease = await pool.acquire()
        token = pool.bind_lease(lease)
        try:
            assert await pool.rotate_lease_to_healthy(reason="RpcCallFailError") is True
            assert lease.account_id == "b"
            assert a.last_error == "RpcCallFailError"
        finally:
            pool.unbind_lease(token)
            await pool.release(lease)

    asyncio.run(run())


def test_rotate_to_healthy_rebinds_current_lease(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(tmp_path, monkeypatch)
    pool = AccountPool.from_settings(settings)
    from ChannelsParser.accounts import _AccountSlot

    a = _AccountSlot(account_id="a", session_path=tmp_path / "a.session", label="a")
    b = _AccountSlot(account_id="b", session_path=tmp_path / "b.session", label="b")
    a.client = object()  # type: ignore[assignment]
    b.client = object()  # type: ignore[assignment]
    pool._slots = [a, b]
    pool._active_id = "a"

    import asyncio

    async def run() -> None:
        lease = await pool.acquire()
        token = pool.bind_lease(lease)
        try:
            assert await pool.rotate_to_healthy(exclude_id="a") is True
            assert lease.account_id == "b"
            # The former healthy lease becomes available to another scan.
            assert pool.free_account_count() == 1
        finally:
            pool.unbind_lease(token)
            await pool.release(lease)

    asyncio.run(run())


def test_release_frees_original_and_replacement_after_rotation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A rotated scan must not leave its original account locked forever."""
    settings = _settings(tmp_path, monkeypatch)
    pool = AccountPool.from_settings(settings)
    from ChannelsParser.accounts import _AccountSlot

    a = _AccountSlot(account_id="a", session_path=tmp_path / "a.session", label="a")
    b = _AccountSlot(account_id="b", session_path=tmp_path / "b.session", label="b")
    a.client = object()  # type: ignore[assignment]
    b.client = object()  # type: ignore[assignment]
    pool._slots = [a, b]
    pool._active_id = "a"

    import asyncio

    async def run() -> None:
        lease = await pool.acquire()
        token = pool.bind_lease(lease)
        try:
            assert await pool.rotate_to_healthy(exclude_id="a") is True
            assert lease.account_id == "b"
            assert pool._leased_ids == {"b"}
        finally:
            pool.unbind_lease(token)
            await pool.release(lease)

        assert pool._leased_ids == set()
        assert pool.free_account_count() == 2

    asyncio.run(run())


def test_quarantine_without_spare_is_released_after_scan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(tmp_path, monkeypatch)
    pool = AccountPool.from_settings(settings)
    from ChannelsParser.accounts import _AccountSlot

    a = _AccountSlot(account_id="a", session_path=tmp_path / "a.session", label="a")
    a.client = object()  # type: ignore[assignment]
    pool._slots = [a]
    pool._active_id = "a"

    import asyncio

    async def run() -> None:
        lease = await pool.acquire()
        token = pool.bind_lease(lease)
        try:
            assert await pool.quarantine_active_and_rotate(reason="test") is False
            assert pool._leased_ids == {"a"}
        finally:
            pool.unbind_lease(token)
            await pool.release(lease)

        assert pool._leased_ids == set()
        assert a.cooldown_until is not None

    asyncio.run(run())


def test_prime_active_when_all_cooling(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Restart must not crash if all sessions still have FloodWait cooldowns in DB."""
    settings = _settings(tmp_path, monkeypatch)
    pool = AccountPool.from_settings(settings)
    from ChannelsParser.accounts import _AccountSlot

    now = datetime.now(timezone.utc)
    a = _AccountSlot(
        account_id="a",
        session_path=tmp_path / "a.session",
        label="a",
        cooldown_until=now + timedelta(hours=1),
    )
    b = _AccountSlot(
        account_id="b",
        session_path=tmp_path / "b.session",
        label="b",
        cooldown_until=now + timedelta(hours=2),
    )
    a.client = object()  # type: ignore[assignment]
    b.client = object()  # type: ignore[assignment]
    pool._slots = [a, b]

    assert pool.healthy_slots() == []
    pool._prime_active_after_connect(authorized=2)
    assert pool._active_id == "a"  # soonest cooldown
    assert pool.seconds_until_any_available() is not None
    assert pool.seconds_until_any_available() >= 3500


def test_prime_active_prefers_free(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings(tmp_path, monkeypatch)
    pool = AccountPool.from_settings(settings)
    from ChannelsParser.accounts import _AccountSlot

    now = datetime.now(timezone.utc)
    a = _AccountSlot(
        account_id="a",
        session_path=tmp_path / "a.session",
        label="a",
        cooldown_until=now + timedelta(hours=1),
    )
    b = _AccountSlot(account_id="b", session_path=tmp_path / "b.session", label="b")
    a.client = object()  # type: ignore[assignment]
    b.client = object()  # type: ignore[assignment]
    pool._slots = [a, b]

    pool._prime_active_after_connect(authorized=2)
    assert pool._active_id == "b"
    assert pool.free_account_count() == 1


def test_two_parallel_leases(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings(tmp_path, monkeypatch)
    pool = AccountPool.from_settings(settings)
    from ChannelsParser.accounts import _AccountSlot
    import asyncio

    a = _AccountSlot(account_id="a", session_path=tmp_path / "a.session", label="a")
    b = _AccountSlot(account_id="b", session_path=tmp_path / "b.session", label="b")
    a.client = object()  # type: ignore[assignment]
    b.client = object()  # type: ignore[assignment]
    pool._slots = [a, b]

    async def run() -> None:
        l1 = await pool.acquire()
        l2 = await pool.acquire()
        assert {l1.account_id, l2.account_id} == {"a", "b"}
        with pytest.raises(RuntimeError, match="свободн|заняты|cooldown"):
            await pool.acquire()
        await pool.release(l1)
        l3 = await pool.acquire()
        assert l3.account_id == l1.account_id
        await pool.release(l2)
        await pool.release(l3)

    asyncio.run(run())
