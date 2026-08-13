from __future__ import annotations

"""Prepared, non-operational provisioning boundary.

This module intentionally contains no Telegram login implementation. It defines the
minimal local record and workflow states so a future owner-controlled onboarding
flow can be added without mixing recovery factors into the parser runtime.
"""

import argparse
import json
import sqlite3
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from typing import Any

from ChannelsParser.accounts import validate_account_id
from ChannelsParser.config import AppSettings, ConfigError


class ProvisioningState(StrEnum):
    DRAFT = "draft"
    AWAITING_OPERATOR = "awaiting_operator"
    COMPLETE = "complete"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class ProvisioningRequest:
    account_id: str
    state: ProvisioningState
    phone_hint: str | None
    has_two_factor: bool
    has_recovery_email: bool
    created_at: datetime
    updated_at: datetime
    note: str | None = None


class ProvisioningRegistry:
    """Metadata-only registry for future authorised account onboarding.

    It never stores a phone number, password, OTP, 2FA secret, email credential,
    session string, or any other authentication factor. Supply sensitive factors
    only at the future interactive, owner-controlled authorization step.
    """

    def __init__(self, database_path: Path) -> None:
        self._path = database_path
        self._path.parent.mkdir(parents=True, exist_ok=True)

    def init(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS parser_provisioning_requests (
                    account_id TEXT PRIMARY KEY,
                    state TEXT NOT NULL,
                    phone_hint TEXT,
                    has_two_factor INTEGER NOT NULL DEFAULT 0,
                    has_recovery_email INTEGER NOT NULL DEFAULT 0,
                    note TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )

    def create_or_update_draft(
        self,
        *,
        account_id: str,
        phone_hint: str | None = None,
        has_two_factor: bool = False,
        has_recovery_email: bool = False,
        note: str | None = None,
    ) -> ProvisioningRequest:
        account_id = validate_account_id(account_id)
        now = _iso(datetime.now(timezone.utc))
        normalized_hint = _phone_hint(phone_hint)
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO parser_provisioning_requests(
                    account_id, state, phone_hint, has_two_factor,
                    has_recovery_email, note, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(account_id) DO UPDATE SET
                    state = excluded.state,
                    phone_hint = excluded.phone_hint,
                    has_two_factor = excluded.has_two_factor,
                    has_recovery_email = excluded.has_recovery_email,
                    note = excluded.note,
                    updated_at = excluded.updated_at
                """,
                (
                    account_id,
                    ProvisioningState.DRAFT.value,
                    normalized_hint,
                    int(has_two_factor),
                    int(has_recovery_email),
                    _clean_note(note),
                    now,
                    now,
                ),
            )
        request = self.get(account_id)
        if request is None:
            raise RuntimeError("Failed to create provisioning draft")
        return request

    def set_state(self, account_id: str, state: ProvisioningState) -> ProvisioningRequest:
        account_id = validate_account_id(account_id)
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE parser_provisioning_requests
                SET state = ?, updated_at = ?
                WHERE account_id = ?
                """,
                (state.value, _iso(datetime.now(timezone.utc)), account_id),
            )
        if cursor.rowcount != 1:
            raise ValueError(f"No provisioning draft for: {account_id}")
        request = self.get(account_id)
        if request is None:
            raise RuntimeError("Provisioning request disappeared")
        return request

    def get(self, account_id: str) -> ProvisioningRequest | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM parser_provisioning_requests WHERE account_id = ?",
                (validate_account_id(account_id),),
            ).fetchone()
        return _row_to_request(row) if row else None

    def list(self) -> list[ProvisioningRequest]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM parser_provisioning_requests ORDER BY updated_at DESC"
            ).fetchall()
        return [_row_to_request(row) for row in rows]

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=30000")
        return connection


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Metadata-only preparation for future owner-controlled session provisioning"
    )
    actions = parser.add_subparsers(dest="action", required=True)
    draft = actions.add_parser("draft", help="Create/update a metadata-only draft")
    draft.add_argument("--name", required=True)
    draft.add_argument("--phone-hint", default=None, help="Masked hint only, e.g. +7999•••1234")
    draft.add_argument("--has-2fa", action="store_true")
    draft.add_argument("--has-recovery-email", action="store_true")
    draft.add_argument("--note", default=None)
    state = actions.add_parser("state", help="Update workflow state")
    state.add_argument("--name", required=True)
    state.add_argument("--value", required=True, choices=[item.value for item in ProvisioningState])
    actions.add_parser("list", help="List non-sensitive provisioning drafts")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    try:
        settings = AppSettings.from_env(require_bot_token=False)
        registry = ProvisioningRegistry(settings.database_path)
        registry.init()
        if args.action == "draft":
            request = registry.create_or_update_draft(
                account_id=args.name,
                phone_hint=args.phone_hint,
                has_two_factor=args.has_2fa,
                has_recovery_email=args.has_recovery_email,
                note=args.note,
            )
            print(_format_request(request))
            print(
                "Draft only: authenticate interactively with the account owner, "
                "then use tg-active-channels-login to create the session."
            )
        elif args.action == "state":
            request = registry.set_state(args.name, ProvisioningState(args.value))
            print(_format_request(request))
        else:
            requests = registry.list()
            if not requests:
                print("No provisioning drafts.")
            for request in requests:
                print(_format_request(request))
    except (ConfigError, ValueError) as exc:
        raise SystemExit(str(exc)) from exc


def _row_to_request(row: sqlite3.Row) -> ProvisioningRequest:
    return ProvisioningRequest(
        account_id=str(row["account_id"]),
        state=ProvisioningState(row["state"]),
        phone_hint=row["phone_hint"],
        has_two_factor=bool(row["has_two_factor"]),
        has_recovery_email=bool(row["has_recovery_email"]),
        note=row["note"],
        created_at=_parse_iso(row["created_at"]),
        updated_at=_parse_iso(row["updated_at"]),
    )


def _format_request(request: ProvisioningRequest) -> str:
    return json.dumps(
        {
            **asdict(request),
            "state": request.state.value,
            "created_at": _iso(request.created_at),
            "updated_at": _iso(request.updated_at),
        },
        ensure_ascii=False,
    )


def _phone_hint(value: str | None) -> str | None:
    if not value:
        return None
    normalized = "".join(value.split())
    if len(normalized) > 32:
        raise ValueError("phone hint must be at most 32 characters")
    # Reject what looks like a full, unmasked phone number: this registry is
    # deliberately metadata-only.
    digits = sum(character.isdigit() for character in normalized)
    if digits >= 8 and not any(character in normalized for character in "*•xX"):
        raise ValueError("use a masked phone hint; full phone numbers are not stored here")
    return normalized


def _clean_note(value: str | None) -> str | None:
    if not value:
        return None
    return " ".join(value.split())[:200] or None


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _parse_iso(value: Any) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return datetime.now(timezone.utc)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


if __name__ == "__main__":
    main()
