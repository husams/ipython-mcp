from __future__ import annotations

import asyncio
import json

from fastmcp import Client

from ipython_mcp.compact import as_tool_result
from ipython_mcp.config import ServerConfig
from ipython_mcp.models import ErrorInfo, ExecuteResponse, RuntimeMetadata
from ipython_mcp.server import create_server


def run(coro):
    return asyncio.run(coro)


async def call_tool(client: Client, name: str, arguments: dict | None = None):
    return await client.call_tool(name, arguments or {})


def compact_data(response):
    assert response.structured_content is None
    assert len(response.content) == 1
    assert response.content[0].type == "text"
    return json.loads(response.content[0].text)


def test_compact_profile_has_three_small_tools_and_shorter_replies():
    async def scenario():
        async with Client(create_server(ServerConfig(profile="full"))) as full:
            async with Client(
                create_server(ServerConfig(profile="compact"))
            ) as compact:
                full_tools = await full.list_tools()
                compact_tools = await compact.list_tools()
                assert {tool.name for tool in full_tools} == {
                    "list",
                    "execute",
                    "call_function",
                    "search",
                    "reload",
                    "inspect",
                    "remove",
                    "reset",
                    "register_tool",
                    "unregister_tool",
                    "runtime_status",
                }
                assert {tool.name for tool in compact_tools} == {
                    "execute",
                    "call_function",
                    "runtime_status",
                }
                assert all(tool.outputSchema is None for tool in compact_tools)
                full_reply = await call_tool(full, "execute", {"code": "value = 1"})
                compact_reply = await call_tool(
                    compact, "execute", {"code": "value = 1"}
                )
                full_size = len(json.dumps(full_reply.structured_content))
                compact_size = len(compact_reply.content[0].text)
                assert compact_size < full_size

    run(scenario())


def test_compact_preserves_falsy_results_and_nested_user_fields():
    async def scenario():
        async with Client(create_server(ServerConfig(profile="compact"))) as client:
            for expression, expected in (
                ("0", 0),
                ("False", False),
                ("None", None),
                ("''", ""),
                ("[]", []),
            ):
                response = await call_tool(client, "execute", {"code": expression})
                assert compact_data(response)["result"] == expected
            response = await call_tool(
                client,
                "execute",
                {
                    "code": (
                        "nested = {'zero': 0, 'false': False, 'none': None, "
                        "'empty': {}, 'items': []}\nnested"
                    )
                },
            )
            assert compact_data(response)["result"] == {
                "zero": 0,
                "false": False,
                "none": None,
                "empty": {},
                "items": [],
            }
            await call_tool(
                client, "execute", {"code": "def returns_none(): return None"}
            )
            called = await call_tool(
                client, "call_function", {"name": "returns_none", "arguments": {}}
            )
            assert compact_data(called)["result"] is None

    run(scenario())


def test_compact_keeps_error_truncation_and_recovery_metadata():
    response = ExecuteResponse(
        ok=False,
        status="error",
        error=ErrorInfo(
            code="operation_timeout",
            message="timed out",
            retryable=True,
            retry_after_seconds=0.5,
            traceback_truncated=True,
        ),
        runtime=RuntimeMetadata(
            request_id="secret-id",
            admission_sequence=4,
            epoch=7,
            interruption_kind="operation_timeout",
            namespace_state="reset",
        ),
    )
    wire = as_tool_result(response)
    assert compact_data(wire) == {
        "ok": False,
        "status": "error",
        "error": {
            "code": "operation_timeout",
            "message": "timed out",
            "retryable": True,
            "retry_after_seconds": 0.5,
            "traceback_truncated": True,
        },
        "runtime": {
            "epoch": 7,
            "interruption_kind": "operation_timeout",
            "namespace_state": "reset",
        },
    }
    empty_error = as_tool_result(
        ExecuteResponse(ok=False, status="error", error=ErrorInfo(code="", message=""))
    )
    assert compact_data(empty_error)["error"] == {"code": "", "message": ""}


def test_compact_mcp_bounds_stdout_and_result_with_flags():
    async def scenario():
        config = ServerConfig(profile="compact", max_text_chars=12)
        async with Client(create_server(config)) as client:
            response = await call_tool(
                client,
                "execute",
                {"code": "print('o' * 100)\n'r' * 100"},
            )
            data = compact_data(response)
            assert len(data["stdout"]) == 12
            assert len(data["result"]) == 12
            assert data["truncated"]["stdout"] is True
            assert data["truncated"]["result"] is True
            assert data["truncated"]["stdout_omitted_chars"] > 0

    run(scenario())


def test_compact_runtime_status_remains_responsive_while_execute_is_busy():
    async def scenario():
        async with Client(create_server(ServerConfig(profile="compact"))) as client:
            pending = asyncio.create_task(
                call_tool(
                    client,
                    "execute",
                    {"code": "import time; time.sleep(0.3)"},
                )
            )
            for _ in range(100):
                status = compact_data(await call_tool(client, "runtime_status"))
                if status.get("operation_active"):
                    assert status["state"] == "busy"
                    break
                await asyncio.sleep(0.005)
            else:
                raise AssertionError(
                    "compact runtime status did not observe busy state"
                )
            await asyncio.wait_for(pending, timeout=2)

    run(scenario())


def test_profile_from_environment(monkeypatch):
    monkeypatch.setenv("IPYTHON_MCP_PROFILE", "compact")
    assert ServerConfig.from_env().profile == "compact"
    monkeypatch.setenv("IPYTHON_MCP_PROFILE", "unknown")
    try:
        ServerConfig.from_env()
    except ValueError as exc:
        assert "profile" in str(exc)
    else:
        raise AssertionError("invalid profile was accepted")
