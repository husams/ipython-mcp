"""Versioned, bounded JSON messages for the parent/worker reliability boundary."""

from __future__ import annotations

import json
from multiprocessing.connection import Connection
from typing import Any

PROTOCOL_VERSION = 1


class IpcProtocolError(RuntimeError):
    """An IPC message was malformed, oversized, or used another version."""


def encode_message(message: dict[str, Any], maximum: int) -> bytes:
    envelope = {"version": PROTOCOL_VERSION, **message}
    try:
        encoded = json.dumps(
            envelope,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise IpcProtocolError("IPC message is not JSON-compatible") from exc
    if len(encoded) > maximum:
        raise IpcProtocolError(
            f"IPC message exceeds the configured {maximum}-byte boundary"
        )
    return encoded


def decode_message(encoded: bytes, maximum: int) -> dict[str, Any]:
    if len(encoded) > maximum:
        raise IpcProtocolError(
            f"IPC message exceeds the configured {maximum}-byte boundary"
        )
    try:
        message = json.loads(encoded)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise IpcProtocolError("IPC message is not valid UTF-8 JSON") from exc
    if not isinstance(message, dict) or message.get("version") != PROTOCOL_VERSION:
        raise IpcProtocolError("IPC protocol version mismatch")
    message = dict(message)
    message.pop("version", None)
    return message


def send_message(connection: Connection, message: dict[str, Any], maximum: int) -> None:
    connection.send_bytes(encode_message(message, maximum))


def receive_message(connection: Connection, maximum: int) -> dict[str, Any]:
    try:
        encoded = connection.recv_bytes(maxlength=maximum)
    except OSError as exc:
        raise IpcProtocolError("IPC message exceeds the configured boundary") from exc
    return decode_message(encoded, maximum)
