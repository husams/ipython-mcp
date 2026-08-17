"""Configuration for the trusted, local IPython runtime."""

from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass, field
from pathlib import Path


def _split(value: str) -> tuple[str, ...]:
    return tuple(part.strip() for part in value.split(",") if part.strip())


@dataclass(frozen=True)
class ServerConfig:
    """Startup and response limits for one server-owned shell."""

    library_paths: tuple[Path, ...] = ()
    preload_modules: tuple[str, ...] = ()
    module_aliases: dict[str, str] = field(default_factory=dict)
    max_text_chars: int = 8_192
    max_repr_chars: int = 1_024
    max_traceback_chars: int = 4_096
    max_results: int = 100
    max_display_items: int = 20
    max_json_depth: int = 6
    max_tool_name_chars: int = 64
    max_tool_description_chars: int = 1_024
    max_dynamic_tools: int = 100
    operation_timeout_seconds: float = 30.0
    interruption_grace_seconds: float = 2.0
    worker_startup_timeout_seconds: float = 10.0
    max_pending_operations: int = 32
    queue_wait_timeout_seconds: float = 30.0
    max_ipc_message_bytes: int = 4 * 1024 * 1024

    def __post_init__(self) -> None:
        integer_fields = (
            "max_text_chars",
            "max_repr_chars",
            "max_traceback_chars",
            "max_results",
            "max_display_items",
            "max_json_depth",
            "max_tool_name_chars",
            "max_tool_description_chars",
            "max_dynamic_tools",
            "max_pending_operations",
            "max_ipc_message_bytes",
        )
        float_fields = (
            "operation_timeout_seconds",
            "interruption_grace_seconds",
            "worker_startup_timeout_seconds",
            "queue_wait_timeout_seconds",
        )
        for name in integer_fields:
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{name} must be a positive integer")
        for name in float_fields:
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(f"{name} must be a positive finite number")
            if float(value) <= 0 or not math.isfinite(float(value)):
                raise ValueError(f"{name} must be a positive finite number")

    @classmethod
    def from_env(cls) -> "ServerConfig":
        paths = tuple(
            Path(part).expanduser()
            for part in os.getenv("IPYTHON_MCP_LIBRARY_PATHS", "").split(os.pathsep)
            if part.strip()
        )
        aliases_text = os.getenv("IPYTHON_MCP_MODULE_ALIASES", "{}")
        try:
            aliases_value = json.loads(aliases_text)
        except json.JSONDecodeError as exc:
            raise ValueError("IPYTHON_MCP_MODULE_ALIASES must be a JSON object") from exc
        if not isinstance(aliases_value, dict) or not all(
            isinstance(key, str) and isinstance(value, str)
            for key, value in aliases_value.items()
        ):
            raise ValueError("IPYTHON_MCP_MODULE_ALIASES must map module names to aliases")
        return cls(
            library_paths=paths,
            preload_modules=_split(os.getenv("IPYTHON_MCP_PRELOAD_MODULES", "")),
            module_aliases=aliases_value,
            max_text_chars=_positive_int("IPYTHON_MCP_MAX_TEXT_CHARS", 8_192),
            max_repr_chars=_positive_int("IPYTHON_MCP_MAX_REPR_CHARS", 1_024),
            max_traceback_chars=_positive_int("IPYTHON_MCP_MAX_TRACEBACK_CHARS", 4_096),
            max_results=_positive_int("IPYTHON_MCP_MAX_RESULTS", 100),
            max_display_items=_positive_int("IPYTHON_MCP_MAX_DISPLAY_ITEMS", 20),
            max_json_depth=_positive_int("IPYTHON_MCP_MAX_JSON_DEPTH", 6),
            max_tool_name_chars=_positive_int("IPYTHON_MCP_MAX_TOOL_NAME_CHARS", 64),
            max_tool_description_chars=_positive_int(
                "IPYTHON_MCP_MAX_TOOL_DESCRIPTION_CHARS", 1_024
            ),
            max_dynamic_tools=_positive_int("IPYTHON_MCP_MAX_DYNAMIC_TOOLS", 100),
            operation_timeout_seconds=_positive_float(
                "IPYTHON_MCP_OPERATION_TIMEOUT_SECONDS", 30.0
            ),
            interruption_grace_seconds=_positive_float(
                "IPYTHON_MCP_INTERRUPTION_GRACE_SECONDS", 2.0
            ),
            worker_startup_timeout_seconds=_positive_float(
                "IPYTHON_MCP_WORKER_STARTUP_TIMEOUT_SECONDS", 10.0
            ),
            max_pending_operations=_positive_int(
                "IPYTHON_MCP_MAX_PENDING_OPERATIONS", 32
            ),
            queue_wait_timeout_seconds=_positive_float(
                "IPYTHON_MCP_QUEUE_WAIT_TIMEOUT_SECONDS", 30.0
            ),
            max_ipc_message_bytes=_positive_int(
                "IPYTHON_MCP_MAX_IPC_MESSAGE_BYTES", 4 * 1024 * 1024
            ),
        )

    def to_worker_payload(self) -> dict[str, object]:
        """Return the JSON-compatible configuration admitted across startup IPC."""

        payload = {
            name: value
            for name, value in self.__dict__.items()
            if name
            not in {
                "operation_timeout_seconds",
                "interruption_grace_seconds",
                "worker_startup_timeout_seconds",
                "max_pending_operations",
                "queue_wait_timeout_seconds",
            }
        }
        payload["library_paths"] = [str(path) for path in self.library_paths]
        payload["preload_modules"] = list(self.preload_modules)
        return payload

    @classmethod
    def from_worker_payload(cls, payload: dict[str, object]) -> "ServerConfig":
        """Rebuild the worker-only configuration from a validated JSON payload."""

        values = dict(payload)
        values["library_paths"] = tuple(Path(str(path)) for path in values["library_paths"])
        values["preload_modules"] = tuple(str(name) for name in values["preload_modules"])
        return cls(**values)


def _positive_int(name: str, default: int) -> int:
    raw = os.getenv(name, str(default))
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be a positive integer") from exc
    if value < 1:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _positive_float(name: str, default: float) -> float:
    raw = os.getenv(name, str(default))
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be a positive finite number") from exc
    if value <= 0 or not math.isfinite(value):
        raise ValueError(f"{name} must be a positive finite number")
    return value
