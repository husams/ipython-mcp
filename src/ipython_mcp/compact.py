"""Small, model-facing response and tool wrappers for the compact profile."""

from __future__ import annotations

import json
from typing import Any

from fastmcp.tools import ToolResult
from mcp.types import TextContent
from pydantic import BaseModel

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
