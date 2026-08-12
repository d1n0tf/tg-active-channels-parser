from __future__ import annotations

import asyncio
import logging
import re
import sqlite3
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from telethon import TelegramClient
from telethon.errors import FloodWaitError

from ChannelsParser.config import AppSettings
from ChannelsParser.proxy import telethon_proxy

logger = logging.getLogger(__name__)

SLUG_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_\-]{0,47}$")
TRANSIENT_ACCOUNT_COOLDOWN_SECONDS = 2 * 60

# Per-async-task lease so concurrent scans each keep their own Telethon client.
_lease_var: ContextVar["AccountLease | None"] = ContextVar("parser_account_lease", default=None)


@dataclass
class AccountInfo:
    """Public snapshot of a parser account for UI / status."""

    account_id: str
    session_path: str
    label: str
    enabled: bool
    cooldown_until: datetime | None
    last_error: str | None
    total_flood_waits: int
    last_used_at: datetime | None
    is_active: bool = False
    is_connected: bool = False
    is_authorized: bool = False

    @property
    def is_cooling(self) -> bool:
        if self.cooldown_until is None:
            return False
        return self.cooldown_until > datetime.now(timezone.utc)

    @property
    def status_label(self) -> str:
        if not self.enabled:
            return "выключен"
        if self.is_cooling:
            left = self.cooldown_until - datetime.now(timezone.utc)  # type: ignore[operator]
            return f"cooldown {_fmt_delta(left)}"
        if not self.is_authorized:
            return "не авторизован"
        if self.is_active:
            return "активен"
        return "готов"


@dataclass
class _AccountSlot:
    account_id: str
    session_path: Path
    label: str
    enabled: bool = True
    cooldown_until: datetime | None = None
    last_error: str | None = None
    total_flood_waits: int = 0
    last_used_at: datetime | None = None
    client: TelegramClient | None = field(default=None, repr=False)


@dataclass
class AccountLease:
    """Exclusive right to use one pool account for the duration of a scan/job."""

    pool: AccountPool
    slot: _AccountSlot
    # A lease may be rebound to a replacement account. Keep every account it
    # has owned so cleanup cannot strand the original one in ``_leased_ids``.
    leased_account_ids: set[str] = field(default_factory=set)

    def __post_init__(self) -> None:
        self.leased_account_ids.add(self.slot.account_id)

    @property
    def account_id(self) -> str:
        return self.slot.account_id

    @property
    def client(self) -> TelegramClient:
        if self.slot.client is None:
            raise RuntimeError(f"Аккаунт {self.slot.account_id} не подключён")
        return self.slot.client


class AccountPool:
    """Pool of Telethon user sessions with leases + FloodWait rotation."""

    def __init__(self, settings: AppSettings) -> None:
        self._settings = settings
        self._slots: list[_AccountSlot] = []
        self._active_id: str | None = None  # last/default for UI
        self._leased_ids: set[str] = set()
        self._lock = asyncio.Lock()
        self._db_path = settings.database_path
        self._ensure_table()

    # ------------------------------------------------------------------ setup

    @classmethod
    def from_settings(cls, settings: AppSettings) -> AccountPool:
        pool = cls(settings)
        pool.discover_sessions()
        return pool

    def _ensure_table(self) -> None:
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS parser_accounts (
                    account_id TEXT PRIMARY KEY,
                    session_path TEXT NOT NULL,
                    label TEXT NOT NULL DEFAULT '',
                    enabled INTEGER NOT NULL DEFAULT 1,
                    cooldown_until TEXT,
                    last_error TEXT,
                    total_flood_waits INTEGER NOT NULL DEFAULT 0,
                    last_used_at TEXT
                )
                """
            )

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        return conn

    def discover_sessions(self) -> None:
        """Load sessions from sessions dir + legacy TELEGRAM_SESSION + DB rows."""
        settings = self._settings
        sessions_dir = settings.telegram_sessions_dir
        sessions_dir.mkdir(parents=True, exist_ok=True)

        found: dict[str, Path] = {}

        # Named sessions in directory (*.session, skip journal files)
        for path in sorted(sessions_dir.glob("*.session")):
            if path.name.endswith(".session-journal"):
                continue
            slug = path.stem
            if SLUG_RE.match(slug):
                found[slug] = path.resolve()

        # Legacy single session path (always register so existing installs keep working)
        legacy = Path(settings.telegram_session)
        legacy_resolved = legacy.resolve() if legacy.is_absolute() else (Path.cwd() / legacy).resolve()
        legacy_slug = "default"
        if legacy_slug not in found:
            found[legacy_slug] = legacy_resolved

        # Merge DB state
        with self._connect() as conn:
            rows = {
                row["account_id"]: row
                for row in conn.execute("SELECT * FROM parser_accounts").fetchall()
            }
            for account_id, session_path in found.items():
                row = rows.get(account_id)
                label = (row["label"] if row else "") or account_id
                enabled = bool(row["enabled"]) if row else True
                cooldown = _parse_dt(row["cooldown_until"]) if row else None
                last_error = row["last_error"] if row else None
                floods = int(row["total_flood_waits"]) if row else 0
                last_used = _parse_dt(row["last_used_at"]) if row else None
                self._slots.append(
                    _AccountSlot(
                        account_id=account_id,
                        session_path=session_path,
                        label=label,
                        enabled=enabled,
                        cooldown_until=cooldown,
                        last_error=last_error,
                        total_flood_waits=floods,
                        last_used_at=last_used,
                    )
                )
                self._upsert_db(
                    account_id=account_id,
                    session_path=str(session_path),
                    label=label,
                    enabled=enabled,
                    cooldown_until=cooldown,
                    last_error=last_error,
                    total_flood_waits=floods,
                    last_used_at=last_used,
                )

            # Sessions only in DB (path may still exist)
            for account_id, row in rows.items():
                if any(s.account_id == account_id for s in self._slots):
                    continue
                path = Path(row["session_path"])
                self._slots.append(
                    _AccountSlot(
                        account_id=account_id,
                        session_path=path,
                        label=row["label"] or account_id,
                        enabled=bool(row["enabled"]),
                        cooldown_until=_parse_dt(row["cooldown_until"]),
                        last_error=row["last_error"],
                        total_flood_waits=int(row["total_flood_waits"] or 0),
                        last_used_at=_parse_dt(row["last_used_at"]),
                    )
                )

        if not self._slots:
            # Ensure at least default slot for first login
            path = Path(settings.telegram_session)
            if not path.is_absolute():
                path = (Path.cwd() / path).resolve()
            self._slots.append(
                _AccountSlot(
                    account_id="default",
                    session_path=path,
                    label="default",
                )
            )
            self._upsert_db(
                account_id="default",
                session_path=str(path),
                label="default",
                enabled=True,
                cooldown_until=None,
                last_error=None,
                total_flood_waits=0,
                last_used_at=None,
            )

        self._slots.sort(key=lambda s: s.account_id)

    def reactivate_account(self, account_id: str) -> bool:
        """Make a successfully re-authorized session eligible for new scans."""
        for slot in self._slots:
            if slot.account_id != account_id:
                continue
            slot.enabled = True
            slot.cooldown_until = None
            slot.last_error = None
            self._persist_slot(slot)
            return True
        return False

    def _upsert_db(
        self,
        *,
        account_id: str,
        session_path: str,
        label: str,
        enabled: bool,
        cooldown_until: datetime | None,
        last_error: str | None,
        total_flood_waits: int,
        last_used_at: datetime | None,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO parser_accounts(
                    account_id, session_path, label, enabled, cooldown_until,
                    last_error, total_flood_waits, last_used_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(account_id) DO UPDATE SET
                    session_path = excluded.session_path,
                    label = excluded.label,
                    enabled = excluded.enabled,
                    cooldown_until = excluded.cooldown_until,
                    last_error = excluded.last_error,
                    total_flood_waits = excluded.total_flood_waits,
                    last_used_at = excluded.last_used_at
                """,
                (
                    account_id,
                    session_path,
                    label,
                    1 if enabled else 0,
                    _iso(cooldown_until),
                    last_error,
                    total_flood_waits,
                    _iso(last_used_at),
                ),
            )

    def _persist_slot(self, slot: _AccountSlot) -> None:
        self._upsert_db(
            account_id=slot.account_id,
            session_path=str(slot.session_path),
            label=slot.label,
            enabled=slot.enabled,
            cooldown_until=slot.cooldown_until,
            last_error=slot.last_error,
            total_flood_waits=slot.total_flood_waits,
            last_used_at=slot.last_used_at,
        )

    # ------------------------------------------------------------------ lifecycle

    def _make_client(self, slot: _AccountSlot) -> TelegramClient:
        session = str(slot.session_path)
        # Telethon adds .session itself if not present — path without suffix is fine
        if session.endswith(".session"):
            session = session[: -len(".session")]
        return TelegramClient(
            session,
            self._settings.telegram_api_id,
            self._settings.telegram_api_hash,
            proxy=telethon_proxy(self._settings.telegram_proxy_url),
        )

    async def connect(self) -> None:
        """Connect all enabled accounts; pick first healthy as active."""
        authorized = 0
        now = datetime.now(timezone.utc)
        for slot in self._slots:
            if not slot.enabled:
                continue
            client = self._make_client(slot)
            try:
                await client.connect()
                if await client.is_user_authorized():
                    me = await client.get_me()
                    slot.label = _account_label(me) or slot.label
                    slot.client = client
                    authorized += 1
                    # Drop expired cooldowns from previous runs so restart recovers cleanly.
                    if slot.cooldown_until is not None and slot.cooldown_until <= now:
                        slot.cooldown_until = None
                        if slot.last_error and "FloodWait" in (slot.last_error or ""):
                            slot.last_error = None
                    self._persist_slot(slot)
                    if slot.cooldown_until is not None and slot.cooldown_until > now:
                        left = max(1, int((slot.cooldown_until - now).total_seconds()))
                        logger.info(
                            "Account ready: %s (%s) [cooldown ~%s]",
                            slot.account_id,
                            slot.label,
                            _fmt_delta(timedelta(seconds=left)),
                        )
                    else:
                        logger.info("Account ready: %s (%s)", slot.account_id, slot.label)
                else:
                    await _disconnect(client)
                    slot.last_error = "not authorized"
                    self._persist_slot(slot)
                    logger.warning("Account %s session not authorized", slot.account_id)
            except Exception as exc:
                slot.last_error = f"{type(exc).__name__}: {exc}"
                self._persist_slot(slot)
                logger.warning("Account %s connect failed: %s", slot.account_id, exc)
                try:
                    await _disconnect(client)
                except Exception:
                    pass

        if authorized == 0:
            raise RuntimeError(
                "Нет авторизованных Telegram-аккаунтов для парсинга. "
                "Добавь: uv run tg-active-channels-login --name acc1"
            )

        self._prime_active_after_connect(authorized=authorized)

    def _prime_active_after_connect(self, *, authorized: int) -> None:
        """Pick default active after connect. Prefer free healthy; else soonest cooldown."""
        free = self.healthy_slots()
        if free:
            free.sort(key=lambda s: s.last_used_at or datetime.fromtimestamp(0, tz=timezone.utc))
            self._active_id = free[0].account_id
            logger.info(
                "Active parser account: %s (%s free of %s authorized)",
                self._active_id,
                len(free),
                authorized,
            )
            return

        # Authorized but every session still in FloodWait cooldown from DB.
        # Start the bot anyway — scans will wait until cooldowns expire.
        now = datetime.now(timezone.utc)
        cooling = [s for s in self._slots if s.enabled and s.client is not None]
        cooling.sort(
            key=lambda s: s.cooldown_until or datetime.fromtimestamp(0, tz=timezone.utc)
        )
        self._active_id = cooling[0].account_id if cooling else None
        parts: list[str] = []
        for s in cooling:
            if s.cooldown_until and s.cooldown_until > now:
                left = max(1, int((s.cooldown_until - now).total_seconds()))
                parts.append(f"{s.account_id}~{_fmt_delta(timedelta(seconds=left))}")
            else:
                parts.append(s.account_id)
        wait = self.seconds_until_any_available()
        logger.warning(
            "All %s authorized account(s) are in cooldown on startup (%s). "
            "Bot starts; next free slot in ~%ss. "
            "To force-reset (may re-hit FloodWait): "
            "sqlite3 %s \"UPDATE parser_accounts SET cooldown_until=NULL, last_error=NULL;\"",
            authorized,
            ", ".join(parts[:12]) + ("…" if len(parts) > 12 else ""),
            wait if wait is not None else "?",
            self._db_path,
        )
        if self._active_id is None:
            raise RuntimeError(
                "Не удалось выбрать аккаунт: нет подключённых сессий после connect."
            )

    async def close(self) -> None:
        for slot in self._slots:
            if slot.client is not None:
                try:
                    await _disconnect(slot.client)
                except Exception:
                    logger.debug("disconnect %s failed", slot.account_id, exc_info=True)
                slot.client = None
        self._leased_ids.clear()

    # ------------------------------------------------------------------ leases

    @property
    def client(self) -> TelegramClient:
        """Client for current task lease, or default active slot."""
        lease = _lease_var.get()
        if lease is not None:
            return lease.client
        slot = self.active_slot()
        if slot.client is None:
            raise RuntimeError(f"Аккаунт {slot.account_id} не подключён")
        return slot.client

    def active_slot(self) -> _AccountSlot:
        lease = _lease_var.get()
        if lease is not None:
            return lease.slot
        if self._active_id is None:
            raise RuntimeError("Нет активного аккаунта")
        for slot in self._slots:
            if slot.account_id == self._active_id:
                return slot
        raise RuntimeError(f"Активный аккаунт {self._active_id} не найден")

    def list_info(self) -> list[AccountInfo]:
        now = datetime.now(timezone.utc)
        result: list[AccountInfo] = []
        for slot in self._slots:
            cooldown = slot.cooldown_until
            if cooldown is not None and cooldown <= now:
                cooldown = None
            leased = slot.account_id in self._leased_ids
            result.append(
                AccountInfo(
                    account_id=slot.account_id,
                    session_path=str(slot.session_path),
                    label=slot.label,
                    enabled=slot.enabled,
                    cooldown_until=cooldown,
                    last_error=slot.last_error,
                    total_flood_waits=slot.total_flood_waits,
                    last_used_at=slot.last_used_at,
                    is_active=leased or slot.account_id == self._active_id,
                    is_connected=slot.client is not None and bool(
                        getattr(slot.client, "is_connected", lambda: False)()
                        if callable(getattr(slot.client, "is_connected", None))
                        else slot.client is not None
                    ),
                    is_authorized=slot.client is not None,
                )
            )
        return result

    def free_account_count(self) -> int:
        return len(self._free_healthy_slots())

    def _free_healthy_slots(self) -> list[_AccountSlot]:
        return [s for s in self.healthy_slots() if s.account_id not in self._leased_ids]

    def healthy_slots(self) -> list[_AccountSlot]:
        now = datetime.now(timezone.utc)
        out: list[_AccountSlot] = []
        for slot in self._slots:
            if not slot.enabled or slot.client is None:
                continue
            if slot.cooldown_until is not None and slot.cooldown_until > now:
                continue
            if slot.cooldown_until is not None and slot.cooldown_until <= now:
                slot.cooldown_until = None
                slot.last_error = None
                self._persist_slot(slot)
            out.append(slot)
        return out

    async def acquire(self) -> AccountLease:
        """Lease a free healthy account for this scan (exclusive until release)."""
        async with self._lock:
            free = self._free_healthy_slots()
            if not free:
                wait = self.seconds_until_any_available()
                if wait is not None:
                    raise RuntimeError(
                        f"Все парсер-аккаунты заняты или в cooldown "
                        f"(освободится ~через {wait}с). Подожди или добавь сессии."
                    )
                raise RuntimeError(
                    "Нет свободных парсер-аккаунтов. "
                    "Добавь: uv run tg-active-channels-login --name acc2"
                )
            free.sort(key=lambda s: s.last_used_at or datetime.fromtimestamp(0, tz=timezone.utc))
            slot = free[0]
            self._leased_ids.add(slot.account_id)
            slot.last_used_at = datetime.now(timezone.utc)
            self._active_id = slot.account_id
            self._persist_slot(slot)
            logger.info("Lease account %s (%s)", slot.account_id, slot.label)
            return AccountLease(self, slot)

    async def release(self, lease: AccountLease) -> None:
        async with self._lock:
            released_ids = tuple(lease.leased_account_ids)
            for account_id in released_ids:
                self._leased_ids.discard(account_id)
            lease.leased_account_ids.clear()
            logger.info("Release account(s) %s", ", ".join(released_ids))

    def bind_lease(self, lease: AccountLease) -> Token:
        return _lease_var.set(lease)

    def unbind_lease(self, token: Token) -> None:
        _lease_var.reset(token)

    def current_lease(self) -> AccountLease | None:
        return _lease_var.get()

    async def rotate_to_healthy(self, *, force: bool = False, exclude_id: str | None = None) -> bool:
        """Move the current lease (if any) to a free healthy account."""
        lease = _lease_var.get()
        async with self._lock:
            candidates = [
                s
                for s in self.healthy_slots()
                if s.account_id != exclude_id and s.account_id not in self._leased_ids
            ]
            if not candidates:
                return False
            candidates.sort(key=lambda s: s.last_used_at or datetime.fromtimestamp(0, tz=timezone.utc))
            chosen = candidates[0]
            if lease is not None:
                previous = lease.slot
                self._leased_ids.discard(previous.account_id)
                self._leased_ids.add(chosen.account_id)
                lease.slot = chosen
                lease.leased_account_ids.add(chosen.account_id)
            self._active_id = chosen.account_id
            chosen.last_used_at = datetime.now(timezone.utc)
            self._persist_slot(chosen)
            return True

    async def mark_flood_and_rotate(self, seconds: int, *, reason: str = "FloodWait") -> bool:
        """Cooldownoldown the account bound to this task's lease (or default active) and rebind."""
        lease = _lease_var.get()
        async with self._lock:
            if lease is not None:
                slot = lease.slot
            else:
                try:
                    slot = self.active_slot()
                except RuntimeError:
                    return False

            until = datetime.now(timezone.utc) + timedelta(seconds=max(1, seconds))
            slot.cooldown_until = until
            slot.total_flood_waits += 1
            slot.last_error = f"{reason}: wait {seconds}s until {until.isoformat()}"
            self._persist_slot(slot)
            self._leased_ids.discard(slot.account_id)
            logger.warning(
                "Account %s cooldown %ss (until %s)",
                slot.account_id,
                seconds,
                until.isoformat(),
            )
            excluded = slot.account_id

            free = [
                s
                for s in self.healthy_slots()
                if s.account_id != excluded and s.account_id not in self._leased_ids
            ]
            if not free:
                # The current scan may still attempt a transport reconnect.
                # Keep this slot exclusively leased until its finally-block
                # runs; AccountLease now remembers it and releases it safely.
                # Otherwise its cooldown could expire while this task still
                # uses the same Telethon client and a second scan would grab it.
                if lease is not None:
                    self._leased_ids.add(slot.account_id)
                self._active_id = slot.account_id
                return False

            free.sort(key=lambda s: s.last_used_at or datetime.fromtimestamp(0, tz=timezone.utc))
            new_slot = free[0]
            self._leased_ids.add(new_slot.account_id)
            new_slot.last_used_at = datetime.now(timezone.utc)
            self._active_id = new_slot.account_id
            self._persist_slot(new_slot)
            if lease is not None:
                lease.slot = new_slot
                lease.leased_account_ids.add(new_slot.account_id)
            logger.info("Rotated lease → %s (%s)", new_slot.account_id, new_slot.label)
            return True

    async def quarantine_active_and_rotate(
        self,
        *,
        seconds: int = TRANSIENT_ACCOUNT_COOLDOWN_SECONDS,
        reason: str,
    ) -> bool:
        """Temporarily remove the active account and continue on a spare one.

        A Telegram server failure can be tied to one account or route. Without
        a cooldown the released account is immediately selected by the next
        scan and the next user receives the identical failure.
        """
        lease = _lease_var.get()
        async with self._lock:
            if lease is not None:
                previous = lease.slot
            else:
                try:
                    previous = self.active_slot()
                except RuntimeError:
                    return False

            until = datetime.now(timezone.utc) + timedelta(seconds=max(1, seconds))
            previous.cooldown_until = until
            previous.last_error = reason
            self._persist_slot(previous)
            self._leased_ids.discard(previous.account_id)

            candidates = [
                slot
                for slot in self.healthy_slots()
                if slot.account_id != previous.account_id
                and slot.account_id not in self._leased_ids
            ]
            if not candidates:
                # See mark_flood_and_rotate: preserve exclusivity until the
                # current scan exits, then AccountLease.release() frees it.
                if lease is not None:
                    self._leased_ids.add(previous.account_id)
                self._active_id = previous.account_id
                return False

            candidates.sort(
                key=lambda slot: slot.last_used_at
                or datetime.fromtimestamp(0, tz=timezone.utc)
            )
            replacement = candidates[0]
            self._leased_ids.add(replacement.account_id)
            replacement.last_used_at = datetime.now(timezone.utc)
            self._active_id = replacement.account_id
            self._persist_slot(replacement)
            if lease is not None:
                lease.slot = replacement
                lease.leased_account_ids.add(replacement.account_id)
            logger.warning(
                "Quarantined account %s until %s; rotated to %s (%s)",
                previous.account_id,
                until.isoformat(),
                replacement.account_id,
                reason,
            )
            return True

    async def disable_active_session(self, *, reason: str) -> bool:
        """Disable a revoked/invalid session until it is re-authorized."""
        lease = _lease_var.get()
        async with self._lock:
            if lease is not None:
                slot = lease.slot
            else:
                try:
                    slot = self.active_slot()
                except RuntimeError:
                    return False

            slot.enabled = False
            slot.cooldown_until = None
            slot.last_error = reason
            self._persist_slot(slot)
            self._leased_ids.discard(slot.account_id)
            try:
                if slot.client is not None:
                    await _disconnect(slot.client)
            finally:
                slot.client = None

            candidates = [
                candidate
                for candidate in self.healthy_slots()
                if candidate.account_id != slot.account_id
                and candidate.account_id not in self._leased_ids
            ]
            if not candidates:
                self._active_id = None
                return False

            candidates.sort(
                key=lambda candidate: candidate.last_used_at
                or datetime.fromtimestamp(0, tz=timezone.utc)
            )
            replacement = candidates[0]
            self._leased_ids.add(replacement.account_id)
            replacement.last_used_at = datetime.now(timezone.utc)
            self._active_id = replacement.account_id
            self._persist_slot(replacement)
            if lease is not None:
                lease.slot = replacement
                lease.leased_account_ids.add(replacement.account_id)
            logger.error(
                "Disabled unusable session %s; rotated to %s (%s)",
                slot.account_id,
                replacement.account_id,
                reason,
            )
            return True

    async def ensure_connected(self) -> None:
        slot = self.active_slot()
        if slot.client is None:
            raise RuntimeError("Нет подключённого аккаунта в lease")
        if not slot.client.is_connected():
            await slot.client.connect()

    async def reconnect_active(self) -> None:
        """Recreate the transport for the account used by the current scan.

        Telethon may still report an open socket after a transient Telegram server
        failure. In that state ``ensure_connected`` is a no-op, so explicitly
        disconnect before reconnecting.
        """
        slot = self.active_slot()
        if slot.client is None:
            raise RuntimeError("Нет подключённого аккаунта в lease")
        await _disconnect(slot.client)
        await slot.client.connect()

    async def rotate_lease_to_healthy(self, *, reason: str) -> bool:
        """Backward-compatible alias for transient-error quarantine."""
        return await self.quarantine_active_and_rotate(reason=reason)

    def seconds_until_any_available(self) -> int | None:
        """Min seconds until some free healthy account exists. None if free now."""
        if self._free_healthy_slots():
            return None
        now = datetime.now(timezone.utc)
        waits: list[float] = []
        for slot in self._slots:
            if not slot.enabled or slot.client is None:
                continue
            if slot.account_id in self._leased_ids:
                continue  # busy with another job — unknown when free
            if slot.cooldown_until and slot.cooldown_until > now:
                waits.append((slot.cooldown_until - now).total_seconds())
        if not waits:
            # all leased — unknown; report short retry hint
            if self._leased_ids:
                return 30
            return None
        return max(1, int(min(waits)))


def session_path_for(settings: AppSettings, account_id: str) -> Path:
    if account_id == "default":
        path = Path(settings.telegram_session)
        return path if path.is_absolute() else (Path.cwd() / path).resolve()
    return (settings.telegram_sessions_dir / f"{account_id}.session").resolve()


def validate_account_id(account_id: str) -> str:
    account_id = account_id.strip()
    if not SLUG_RE.match(account_id):
        raise ValueError(
            "Имя аккаунта: латиница/цифры/_/-, 1–48 символов, например acc1 или work_phone"
        )
    return account_id


def _account_label(me: Any) -> str | None:
    username = getattr(me, "username", None)
    if username:
        return f"@{username}"
    phone = getattr(me, "phone", None)
    if phone:
        return f"+{phone}" if not str(phone).startswith("+") else str(phone)
    user_id = getattr(me, "id", None)
    return str(user_id) if user_id is not None else None


async def _disconnect(client: TelegramClient) -> None:
    result: object = client.disconnect()
    if asyncio.iscoroutine(result) or asyncio.isfuture(result):
        await result  # type: ignore[misc]


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def _parse_dt(value: object) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _fmt_delta(delta: timedelta) -> str:
    total = max(0, int(delta.total_seconds()))
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    if hours >= 24:
        days, hours = divmod(hours, 24)
        return f"{days}д {hours}ч"
    if hours:
        return f"{hours}ч {minutes}м"
    if minutes:
        return f"{minutes}м {secs}с"
    return f"{secs}с"
