"""Measure the final IPython MCP FastMCP catalog and reply wire shapes.

This is synthetic: it exercises the real FastMCP server/tool serialization but
replaces the runtime worker with deterministic in-memory responses.  It makes
no model or network calls.
"""

from __future__ import annotations

import asyncio
import hashlib
import importlib.metadata
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

REPOSITORY = Path(__file__).resolve().parents[3]
OUTPUT = Path(__file__).with_name("measurement.json")
sys.path.insert(0, str(REPOSITORY / "src"))

from fastmcp import Client  # noqa: E402
from ipython_mcp.config import ServerConfig  # noqa: E402
from ipython_mcp.controller import ManagedRuntime  # noqa: E402
from ipython_mcp.models import (  # noqa: E402
    ErrorInfo,
    ExecuteResponse,
    RuntimeMetadata,
    RuntimeStatusResponse,
    TruncationInfo,
)
import ipython_mcp.server as server_module  # noqa: E402


def normalized_json(value: Any) -> str:
    """Use the exact compact JSON normalization measured in this artifact."""

    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def catalog_wire(tools: list[Any]) -> list[dict[str, Any]]:
    """Normalize MCP tools/list models without null-valued protocol fields."""

    return [
        tool.model_dump(mode="json", by_alias=True, exclude_none=True)
        for tool in tools
    ]


def reply_wire(reply: Any) -> dict[str, Any]:
    """Normalize fields present in an MCP tools/call result."""

    return {
        "content": [
            item.model_dump(mode="json", by_alias=True, exclude_none=True)
            for item in reply.content
        ],
        "isError": reply.is_error,
        "structuredContent": reply.structured_content,
    }


def count(value: Any) -> dict[str, Any]:
    normalized = normalized_json(value)
    return {
        "utf8_bytes": len(normalized.encode("utf-8")),
        "characters": len(normalized),
        "sha256": hashlib.sha256(normalized.encode("utf-8")).hexdigest(),
    }


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def runtime_metadata() -> RuntimeMetadata:
    return RuntimeMetadata(
        request_id="r" * 32,
        admission_sequence=1,
        epoch=1,
    )


class SyntheticRuntime(ManagedRuntime):
    """Deterministic runtime used only to measure the protocol wrappers."""

    def __init__(self, config: ServerConfig):
        self.config = config

    async def start(self) -> None:
        return None

    async def close(self) -> None:
        return None

    async def execute_operation(self, code: str) -> Any:
        if code == "scalar":
            response = ExecuteResponse(
                ok=True,
                status="ok",
                execution_count=1,
                result=2,
                runtime=runtime_metadata(),
            )
        elif code == "falsy":
            response = ExecuteResponse(
                ok=True,
                status="ok",
                execution_count=2,
                result={
                    "zero": 0,
                    "false": False,
                    "none": None,
                    "empty_string": "",
                    "empty_list": [],
                    "empty_object": {},
                },
                runtime=runtime_metadata(),
            )
        elif code == "truncated":
            response = ExecuteResponse(
                ok=True,
                status="ok",
                execution_count=3,
                result="retained-result",
                stdout="retained-stdout",
                truncated=TruncationInfo(
                    result=True,
                    stdout=True,
                    stdout_omitted_chars=17,
                ),
                runtime=runtime_metadata(),
            )
        elif code == "error":
            response = ExecuteResponse(
                ok=False,
                status="error",
                stdout="partial-stdout",
                error=ErrorInfo(
                    code="execution_error",
                    message="synthetic failure",
                    error_type="ValueError",
                    retryable=True,
                    retry_after_seconds=0.5,
                    traceback_truncated=True,
                ),
                runtime=RuntimeMetadata(
                    request_id="e" * 32,
                    admission_sequence=2,
                    epoch=2,
                    interruption_kind="operation_timeout",
                    namespace_state="reset",
                ),
            )
        else:
            raise AssertionError(f"unexpected synthetic code: {code}")
        return SimpleNamespace(value=response, catalog_changed=False)

    async def runtime_status(self) -> RuntimeStatusResponse:
        return RuntimeStatusResponse(
            state="ready",
            epoch=1,
            queue_depth=0,
            operation_active=False,
        )

    async def dynamic_tools(self) -> Any:
        return SimpleNamespace(value=[], catalog_changed=False)

    async def dynamic_tool(self, name: str) -> Any:
        del name
        return SimpleNamespace(value=None, catalog_changed=False)


async def measure() -> dict[str, Any]:
    original_runtime = server_module.ManagedRuntime
    server_module.ManagedRuntime = SyntheticRuntime
    try:
        async with Client(
            server_module.create_server(ServerConfig(profile="full"))
        ) as full_client, Client(
            server_module.create_server(ServerConfig(profile="compact"))
        ) as compact_client:
            full_tools = await full_client.list_tools()
            compact_tools = await compact_client.list_tools()
            replies: dict[str, Any] = {}
            for case in ("scalar", "falsy", "truncated", "error"):
                full_reply = await full_client.call_tool("execute", {"code": case})
                compact_reply = await compact_client.call_tool(
                    "execute", {"code": case}
                )
                full_value = reply_wire(full_reply)
                compact_value = reply_wire(compact_reply)
                replies[case] = {
                    "full": count(full_value),
                    "compact": count(compact_value),
                    "compact_payload": compact_value,
                }
    finally:
        server_module.ManagedRuntime = original_runtime

    full_catalog = catalog_wire(full_tools)
    compact_catalog = catalog_wire(compact_tools)
    return {
        "kind": "synthetic-live-fastmcp-wire-measurement",
        "network_or_model_calls": False,
        "repository": str(REPOSITORY),
        "environment": {
            "python": sys.version.split()[0],
            "fastmcp": importlib.metadata.version("fastmcp"),
            "mcp": importlib.metadata.version("mcp"),
            "pydantic": importlib.metadata.version("pydantic"),
        },
        "source_sha256": {
            str(path.relative_to(REPOSITORY)): file_sha256(path)
            for path in (
                REPOSITORY / "src/ipython_mcp/compact.py",
                REPOSITORY / "src/ipython_mcp/config.py",
                REPOSITORY / "src/ipython_mcp/models.py",
                REPOSITORY / "src/ipython_mcp/server.py",
            )
        },
        "measurement_script_sha256": file_sha256(Path(__file__)),
        "normalization": {
            "json": {
                "ensure_ascii": False,
                "separators": [",", ":"],
                "sort_keys": True,
            },
            "catalog_models": {
                "mode": "json",
                "by_alias": True,
                "exclude_none": True,
            },
            "reply_fields": ["content", "isError", "structuredContent"],
            "content_models": {
                "mode": "json",
                "by_alias": True,
                "exclude_none": True,
            },
        },
        "catalog": {
            "full": {
                **count(full_catalog),
                "tool_names": [tool.name for tool in full_tools],
            },
            "compact": {
                **count(compact_catalog),
                "tool_names": [tool.name for tool in compact_tools],
                "output_schemas": [tool.outputSchema for tool in compact_tools],
            },
        },
        "replies": replies,
    }


if __name__ == "__main__":
    measurement = asyncio.run(measure())
    OUTPUT.write_text(
        json.dumps(measurement, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(OUTPUT)
