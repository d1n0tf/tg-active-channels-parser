from __future__ import annotations

from pathlib import Path

import pytest

from ChannelsParser.config import AppSettings
from ChannelsParser.provisioning import ProvisioningRegistry, ProvisioningState
from ChannelsParser.session_backup import BackupError, backup_session, restore_session, verify_backup


def _settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AppSettings:
    monkeypatch.setenv("TELEGRAM_API_ID", "123")
    monkeypatch.setenv("TELEGRAM_API_HASH", "hash")
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "db.sqlite3"))
    monkeypatch.setenv("TELEGRAM_SESSION", str(tmp_path / "legacy.session"))
    monkeypatch.setenv("TELEGRAM_SESSIONS_DIR", str(tmp_path / "sessions"))
    monkeypatch.setenv("TELEGRAM_BACKUPS_DIR", str(tmp_path / "backups"))
    monkeypatch.setenv("SESSION_BACKUP_KEY", "test secret")
    return AppSettings.from_env(require_bot_token=False)


def test_encrypted_session_backup_round_trip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings(tmp_path, monkeypatch)
    session = settings.telegram_sessions_dir / "acc1.session"
    session.parent.mkdir(parents=True)
    original = b"sqlite session bytes\x00\x01"
    session.write_bytes(original)

    record = backup_session(settings, account_id="acc1")
    assert record.path.exists()
    assert record.path.read_bytes() != original
    verify_backup(settings, account_id="acc1")

    session.unlink()
    restored = restore_session(settings, account_id="acc1")
    assert restored.read_bytes() == original


def test_backup_rejects_bad_key_and_overwrite(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings(tmp_path, monkeypatch)
    session = settings.telegram_sessions_dir / "acc1.session"
    session.parent.mkdir(parents=True)
    session.write_bytes(b"original")
    backup_session(settings, account_id="acc1")

    with pytest.raises(BackupError, match="authentication failed"):
        verify_backup(settings, account_id="acc1", passphrase="wrong")
    with pytest.raises(BackupError, match="Refusing to overwrite"):
        restore_session(settings, account_id="acc1")


def test_provisioning_registry_stores_metadata_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings(tmp_path, monkeypatch)
    registry = ProvisioningRegistry(settings.database_path)
    registry.init()

    request = registry.create_or_update_draft(
        account_id="acc3",
        phone_hint="+7999***1234",
        has_two_factor=True,
        has_recovery_email=True,
        note="operator-owned",
    )
    assert request.state is ProvisioningState.DRAFT
    assert request.phone_hint == "+7999***1234"
    assert request.has_two_factor is True

    updated = registry.set_state("acc3", ProvisioningState.AWAITING_OPERATOR)
    assert updated.state is ProvisioningState.AWAITING_OPERATOR

    with pytest.raises(ValueError, match="masked"):
        registry.create_or_update_draft(account_id="bad", phone_hint="+79991234567")
