from __future__ import annotations

from ChannelsParser.user_errors import scan_errors_text, stored_scan_error_text
from ChannelsParser.user_errors import operation_error_text
from telethon.errors import RpcCallFailError, SessionRevokedError


def test_scan_errors_hide_telethon_implementation_details() -> None:
    text = scan_errors_text(
        [
            "untitled77777: RpcCallFailError: RPCError 500: INTERNAL",
            "comments:42: FloodWait 900s",
        ]
    )

    assert "RpcCallFailError" not in text
    assert "untitled77777" not in text
    assert "Telegram временно не ответил" in text
    assert "Telegram временно ограничил" in text


def test_stored_scan_error_hides_raw_technical_error() -> None:
    assert (
        stored_scan_error_text("RPCError 500: INTERNAL (caused by ResolveUsernameRequest)")
        == "Технический сбой. Запусти скан повторно."
    )


def test_stored_scan_error_keeps_clear_input_feedback() -> None:
    assert stored_scan_error_text("Некорректный username канала") == "Некорректный username канала"


def test_operation_error_hides_telegram_implementation_details() -> None:
    text = operation_error_text(RpcCallFailError(None))

    assert "RpcCallFailError" not in text
    assert "Telegram" in text


def test_session_error_is_described_without_internal_name() -> None:
    text = operation_error_text(SessionRevokedError(None))

    assert "SessionRevokedError" not in text
    assert "переподключ" in text

    scan_text = scan_errors_text(["source: SessionRevokedError: AUTH_KEY_UNREGISTERED"])
    assert "SessionRevokedError" not in scan_text
    assert "аккаунт парсера" in scan_text
