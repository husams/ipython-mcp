"""Small, model-facing response and tool wrappers for the compact profile."""

from __future__ import annotations

import json
from typing import Any

from fastmcp.tools import ToolResult
from mcp.types import TextContent
from pydantic import BaseModel

from .models import RuntimeStatusResponse


def _compact_error(error: dict[str, Any] | None) -> dict[str, Any] | None:
    if not error:
        return None
    result: dict[str, Any] = {}
    for key in ("code", "message"):
        if key in error:
            result[key] = error[key]
    for key in ("error_type", "traceback"):
        value = error.get(key)
        if value is not None and value != "":
            result[key] = value
    location = error.get("location")
    if isinstance(location, dict):
        location = {
            key: value
            for key, value in location.items()
            if value is not None and value != ""
        }
        if location:
            result["location"] = location
    if error.get("retryable"):
        result["retryable"] = True
    if error.get("retry_after_seconds") is not None:
        result["retry_after_seconds"] = error["retry_after_seconds"]
    for key in (
        "message_truncated",
        "location_truncated",
        "traceback_truncated",
    ):
        if error.get(key):
            result[key] = True
    return result or None


def _compact_runtime(runtime: dict[str, Any] | None) -> dict[str, Any] | None:
    if not runtime:
        return None
    # Epoch is deliberately retained on every operation response.  It is the
    # one inexpensive signal that tells an agent its namespace was replaced.
    result: dict[str, Any] = {"epoch": runtime["epoch"]}
    if runtime.get("queue_wait_seconds", 0):
        result["queue_wait_seconds"] = runtime["queue_wait_seconds"]
    if runtime.get("interruption_kind") is not None:
        result["interruption_kind"] = runtime["interruption_kind"]
    if runtime.get("namespace_state", "unchanged") != "unchanged":
        result["namespace_state"] = runtime["namespace_state"]
    return result


def _compact_truncation(
    truncated: dict[str, Any] | bool | None,
) -> dict[str, Any] | bool | None:
    if isinstance(truncated, dict):
        result = {key: value for key, value in truncated.items() if value}
        return result or None
    return True if truncated else None


def compact_response(value: BaseModel) -> dict[str, Any]:
    """Prune protocol defaults while preserving all user result values."""

    data = value.model_dump(mode="json")
    fields = value.model_fields_set

    if isinstance(value, RuntimeStatusResponse):
        result: dict[str, Any] = {
            "state": data["state"],
            "epoch": data["epoch"],
        }
        if data["queue_depth"]:
            result["queue_depth"] = data["queue_depth"]
        if data["operation_active"]:
            result["operation_active"] = True
        if data["latest_interruption_kind"] is not None:
            result["latest_interruption_kind"] = data["latest_interruption_kind"]
        if data["latest_namespace_state"] != "unchanged":
            result["latest_namespace_state"] = data["latest_namespace_state"]
        if data["replacement_startup_seconds"] is not None:
            result["replacement_startup_seconds"] = data[
                "replacement_startup_seconds"
            ]
        error = _compact_error(data.get("error"))
        if error is not None:
            result["error"] = error
        return result

    result = {}
    for key in ("ok", "status", "name"):
        if key in data:
            result[key] = data[key]
    if "execution_count" in data and data["execution_count"] is not None:
        result["execution_count"] = data["execution_count"]
    if "result" in data and ("result" in fields or data["result"] is not None):
        # Do not recursively filter this value: false, 0, None, empty strings,
        # empty containers, and nested user dictionary fields are meaningful.
        result["result"] = data["result"]
    for key in ("stdout", "stderr", "display_data"):
        if data.get(key):
            result[key] = data[key]
    error = _compact_error(data.get("error"))
    if error is not None:
        result["error"] = error
    truncated = _compact_truncation(data.get("truncated"))
    if truncated is not None:
        result["truncated"] = truncated
    runtime = _compact_runtime(data.get("runtime"))
    if runtime is not None:
        result["runtime"] = runtime
    for key in ("name_truncated",):
        if data.get(key):
            result[key] = True
    return result


def as_tool_result(value: BaseModel) -> ToolResult:
    """Return one CLI-visible compact JSON content block."""

    text = json.dumps(
        compact_response(value), separators=(",", ":"), ensure_ascii=False
    )
    return ToolResult(
        content=[TextContent(type="text", text=text)], structured_content=None
    )
