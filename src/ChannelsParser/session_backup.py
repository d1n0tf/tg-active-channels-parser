from __future__ import annotations

import argparse
import hashlib
import hmac
import os
import struct
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from ChannelsParser.accounts import _SessionProcessLock, session_path_for, validate_account_id
from ChannelsParser.config import AppSettings, ConfigError

_MAGIC = b"TGSPBK01"
_SALT_SIZE = 16
_NONCE_SIZE = 16
_TAG_SIZE = 32
_ITERATIONS = 600_000
_CHUNK_SIZE = 1024 * 1024


class BackupError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class BackupRecord:
    account_id: str
    path: Path
    size: int
    created_at: datetime


def backup_path_for(settings: AppSettings, account_id: str) -> Path:
    account_id = validate_account_id(account_id)
    return (settings.telegram_backups_dir / f"{account_id}.tgbackup").resolve()


def backup_session(
    settings: AppSettings, *, account_id: str, passphrase: str | None = None
) -> BackupRecord:
    source = session_path_for(settings, validate_account_id(account_id))
    if not source.is_file():
        raise BackupError(f"Session file not found: {source}")
    secret = _secret(passphrase, settings.session_backup_key)
    target = backup_path_for(settings, account_id)
    target.parent.mkdir(parents=True, exist_ok=True)
    lock = _SessionProcessLock(source)
    lock.acquire()
    try:
        payload = source.read_bytes()
        encrypted = _seal(payload, secret)
        temp = target.with_suffix(target.suffix + ".tmp")
        try:
            temp.write_bytes(encrypted)
            os.replace(temp, target)
        finally:
            temp.unlink(missing_ok=True)
    finally:
        lock.release()
    stat = target.stat()
    return BackupRecord(
        account_id=account_id,
        path=target,
        size=stat.st_size,
        created_at=datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc),
    )


def verify_backup(
    settings: AppSettings, *, account_id: str, passphrase: str | None = None
) -> BackupRecord:
    target = backup_path_for(settings, account_id)
    if not target.is_file():
        raise BackupError(f"Backup file not found: {target}")
    _open(target.read_bytes(), _secret(passphrase, settings.session_backup_key))
    stat = target.stat()
    return BackupRecord(
        account_id=account_id,
        path=target,
        size=stat.st_size,
        created_at=datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc),
    )


def restore_session(
    settings: AppSettings,
    *,
    account_id: str,
    passphrase: str | None = None,
    overwrite: bool = False,
) -> Path:
    account_id = validate_account_id(account_id)
    source = backup_path_for(settings, account_id)
    if not source.is_file():
        raise BackupError(f"Backup file not found: {source}")
    target = session_path_for(settings, account_id)
    if target.exists() and not overwrite:
        raise BackupError(f"Refusing to overwrite existing session: {target}")
    # A backup is solely disaster recovery for local loss/corruption. It does
    # not revive a server-revoked Telegram authorization.
    payload = _open(source.read_bytes(), _secret(passphrase, settings.session_backup_key))
    target.parent.mkdir(parents=True, exist_ok=True)
    lock = _SessionProcessLock(target)
    lock.acquire()
    try:
        temp = target.with_suffix(target.suffix + ".restore.tmp")
        try:
            temp.write_bytes(payload)
            os.replace(temp, target)
        finally:
            temp.unlink(missing_ok=True)
    finally:
        lock.release()
    return target


def list_backups(settings: AppSettings) -> list[BackupRecord]:
    directory = settings.telegram_backups_dir
    if not directory.exists():
        return []
    records: list[BackupRecord] = []
    for path in sorted(directory.glob("*.tgbackup")):
        try:
            account_id = validate_account_id(path.stem)
        except ValueError:
            continue
        stat = path.stat()
        records.append(
            BackupRecord(
                account_id=account_id,
                path=path.resolve(),
                size=stat.st_size,
                created_at=datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc),
            )
        )
    return records


def _secret(passphrase: str | None, configured: str | None) -> bytes:
    raw = (passphrase or configured or "").strip()
    if not raw:
        raise BackupError(
            "Set SESSION_BACKUP_KEY or pass --passphrase; do not store it beside backups."
        )
    return raw.encode("utf-8")


def _seal(plaintext: bytes, secret: bytes) -> bytes:
    salt = os.urandom(_SALT_SIZE)
    nonce = os.urandom(_NONCE_SIZE)
    key = hashlib.pbkdf2_hmac("sha256", secret, salt, _ITERATIONS, dklen=64)
    ciphertext = _xor_stream(plaintext, key[:32], nonce)
    header = _MAGIC + salt + nonce + struct.pack(">Q", len(plaintext))
    tag = hmac.new(key[32:], header + ciphertext, hashlib.sha256).digest()
    return header + tag + ciphertext


def _open(blob: bytes, secret: bytes) -> bytes:
    min_size = len(_MAGIC) + _SALT_SIZE + _NONCE_SIZE + 8 + _TAG_SIZE
    if len(blob) < min_size or not blob.startswith(_MAGIC):
        raise BackupError("Invalid backup format")
    offset = len(_MAGIC)
    salt = blob[offset : offset + _SALT_SIZE]
    offset += _SALT_SIZE
    nonce = blob[offset : offset + _NONCE_SIZE]
    offset += _NONCE_SIZE
    (original_size,) = struct.unpack(">Q", blob[offset : offset + 8])
    offset += 8
    tag = blob[offset : offset + _TAG_SIZE]
    offset += _TAG_SIZE
    ciphertext = blob[offset:]
    key = hashlib.pbkdf2_hmac("sha256", secret, salt, _ITERATIONS, dklen=64)
    expected = hmac.new(key[32:], blob[: offset - _TAG_SIZE] + ciphertext, hashlib.sha256).digest()
    if not hmac.compare_digest(tag, expected):
        raise BackupError("Backup authentication failed: wrong passphrase or damaged file")
    plaintext = _xor_stream(ciphertext, key[:32], nonce)
    if len(plaintext) != original_size:
        raise BackupError("Backup payload size mismatch")
    return plaintext


def _xor_stream(data: bytes, key: bytes, nonce: bytes) -> bytes:
    out = bytearray(len(data))
    block_index = 0
    for offset in range(0, len(data), _CHUNK_SIZE):
        chunk = data[offset : offset + _CHUNK_SIZE]
        stream = bytearray()
        while len(stream) < len(chunk):
            stream.extend(
                hashlib.sha256(key + nonce + block_index.to_bytes(8, "big")).digest()
            )
            block_index += 1
        out[offset : offset + len(chunk)] = bytes(
            a ^ b for a, b in zip(chunk, stream, strict=False)
        )
    return bytes(out)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Encrypted local backup/restore for Telethon session files"
    )
    actions = parser.add_subparsers(dest="action", required=True)
    for name in ("create", "verify", "restore"):
        command = actions.add_parser(name)
        command.add_argument("--name", required=True, help="Parser account id")
        command.add_argument("--passphrase", default=None, help="Overrides SESSION_BACKUP_KEY")
    actions.choices["restore"].add_argument(
        "--overwrite", action="store_true", help="Replace an existing local session"
    )
    actions.add_parser("list")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    try:
        settings = AppSettings.from_env(require_bot_token=False)
        if args.action == "create":
            record = backup_session(settings, account_id=args.name, passphrase=args.passphrase)
            print(f"Backup created: {record.path} ({record.size} bytes)")
        elif args.action == "verify":
            record = verify_backup(settings, account_id=args.name, passphrase=args.passphrase)
            print(f"Backup verified: {record.path} ({record.size} bytes)")
        elif args.action == "restore":
            path = restore_session(
                settings,
                account_id=args.name,
                passphrase=args.passphrase,
                overwrite=args.overwrite,
            )
            print(f"Session restored: {path}")
        else:
            records = list_backups(settings)
            if not records:
                print("No backups found.")
            for record in records:
                stamp = record.created_at.strftime("%Y-%m-%d %H:%M UTC")
                print(f"{record.account_id}\t{stamp}\t{record.size}\t{record.path}")
    except (BackupError, ConfigError, ValueError) as exc:
        raise SystemExit(str(exc)) from exc


if __name__ == "__main__":
    main()
