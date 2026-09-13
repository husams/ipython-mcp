from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

import pytest
from fastmcp import Client
from fastmcp.client.transports import StdioTransport
from fastmcp.client.messages import MessageHandler

from ipython_mcp.config import ServerConfig
from ipython_mcp.server import create_server


STABLE_TOOLS = {
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


class ToolChangeRecorder(MessageHandler):
    def __init__(self) -> None:
        self.count = 0

    async def on_tool_list_changed(self, message) -> None:
        del message
        self.count += 1


async def _wait_for_marker(path: Path, timeout: float = 5.0) -> None:
    async def wait() -> None:
        while not path.exists():
            await asyncio.sleep(0.01)

    await asyncio.wait_for(wait(), timeout=timeout)


async def _call_tool(client: Client, name: str, arguments: dict | None = None):
    result = await client.call_tool(name, arguments or {})
    return result.structured_content


@pytest.mark.parametrize("transport_kind", ("in_memory", "stdio"))
@pytest.mark.parametrize("hold_mode", ("sleep", "cpu"))
def test_tools_list_stays_responsive_during_active_execute(
    tmp_path: Path, transport_kind: str, hold_mode: str
):
    async def scenario() -> None:
        started = tmp_path / "execute.started"
        release = tmp_path / "execute.release"
        started_literal = json.dumps(str(started))
        release_literal = json.dumps(str(release))
        hold_body = (
            "    time.sleep(0.01)\n"
            if hold_mode == "sleep"
            else "    sum(range(10000))\n"
        )
        hold_code = (
            "import time\n"
            "from pathlib import Path\n"
            f"_responsiveness_started = Path({started_literal})\n"
            f"_responsiveness_release = Path({release_literal})\n"
            "_responsiveness_started.write_text('started', encoding='utf-8')\n"
            "while not _responsiveness_release.exists():\n" + hold_body + "'released'"
        )

        if transport_kind == "in_memory":
            client = Client(create_server())
        else:
            client = Client(
                StdioTransport(
                    command=sys.executable,
                    args=["-m", "ipython_mcp.server"],
                    cwd=str(Path(__file__).resolve().parents[1]),
                    env={"PATH": os.environ.get("PATH", "")},
                )
            )

        async with client:
            await _call_tool(
                client,
                "execute",
                {
                    "code": (
                        "def registered_tool(value: int) -> int:\n"
                        "    return value + 1\n"
                    )
                },
            )
            registered = await _call_tool(
                client, "register_tool", {"name": "registered_tool"}
            )
            assert registered["ok"] is True
            expected_catalog = STABLE_TOOLS | {"registered_tool"}
            assert {tool.name for tool in await client.list_tools()} == expected_catalog

            execute_task = asyncio.create_task(
                _call_tool(client, "execute", {"code": hold_code})
            )
            try:
                await _wait_for_marker(started)

                assert await asyncio.wait_for(client.ping(), timeout=1.0) is True
                status = await asyncio.wait_for(
                    _call_tool(client, "runtime_status"), timeout=1.0
                )
                assert status["operation_active"] is True
                assert status["queue_depth"] == 0
                assert execute_task.done() is False

                discovered = await asyncio.wait_for(client.list_tools(), timeout=1.0)
                assert {tool.name for tool in discovered} == expected_catalog
                rediscovered = await asyncio.wait_for(client.list_tools(), timeout=1.0)
                assert {tool.name for tool in rediscovered} == expected_catalog
                status_after_discovery = await asyncio.wait_for(
                    _call_tool(client, "runtime_status"), timeout=1.0
                )
                assert status_after_discovery["operation_active"] is True
                assert status_after_discovery["queue_depth"] == 0
                assert execute_task.done() is False
            finally:
                release.touch()
                execute_result = await asyncio.shield(execute_task)

            assert execute_result["ok"] is True
            assert execute_result["result"] == "released"

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "mutation_exit", ("return", "system_exit", "keyboard_interrupt")
)
def test_dynamic_catalog_reconciles_schema_drift_without_execute(mutation_exit: str):
    async def scenario() -> None:
        exit_statement = {
            "return": "    return None\n",
            "system_exit": "    raise SystemExit('mutation complete')\n",
            "keyboard_interrupt": "    raise KeyboardInterrupt()\n",
        }[mutation_exit]
        async with Client(create_server()) as client:
            await _call_tool(
                client,
                "execute",
                {
                    "code": (
                        "def target(value: int) -> int:\n"
                        "    return value\n"
                        "\n"
                        "def mutate_target() -> None:\n"
                        "    target.__annotations__['value'] = str\n"
                        "    target.__annotations__['return'] = str\n" + exit_statement
                    )
                },
            )
            registered = await _call_tool(client, "register_tool", {"name": "target"})
            assert registered["ok"] is True

            initial = next(
                tool for tool in await client.list_tools() if tool.name == "target"
            )
            assert initial.inputSchema["properties"]["value"] == {"type": "integer"}

            # The response object must not provide a mutable reference into the
            # parent-side catalog cache.
            initial.inputSchema["properties"]["value"]["type"] = "string"
            unchanged = next(
                tool for tool in await client.list_tools() if tool.name == "target"
            )
            assert unchanged.inputSchema["properties"]["value"] == {"type": "integer"}

            mutated = await _call_tool(
                client, "call_function", {"name": "mutate_target", "arguments": {}}
            )
            assert isinstance(mutated, dict)
            assert "target" not in {tool.name for tool in await client.list_tools()}

            stale = await _call_tool(client, "target", {"value": 1})
            assert stale["error"]["code"] == "stale_registration"

            unregistered = await _call_tool(
                client, "unregister_tool", {"names": ["target"]}
            )
            assert unregistered["unregistered"] == ["target"]
            assert "target" not in {tool.name for tool in await client.list_tools()}
            unknown = await client.call_tool(
                "target", {"value": 1}, raise_on_error=False
            )
            assert unknown.is_error is True

    asyncio.run(scenario())


def test_large_dynamic_catalog_uses_bounded_publication_deltas():
    async def scenario() -> None:
        config = ServerConfig(
            max_ipc_message_bytes=4096,
            operation_timeout_seconds=0.2,
            interruption_grace_seconds=1.0,
        )
        async with Client(create_server(config)) as client:
            definitions = "\n".join(
                f"def bounded_tool_{index}(value: int) -> int:\n"
                f"    return value + {index}\n"
                for index in range(15)
            )
            created = await _call_tool(client, "execute", {"code": definitions})
            assert created["ok"] is True

            initial_status = await _call_tool(client, "runtime_status")
            description = "d" * 200
            for index in range(15):
                registration = await _call_tool(
                    client,
                    "register_tool",
                    {
                        "name": f"bounded_tool_{index}",
                        "description": description,
                    },
                )
                assert registration["ok"] is True

            tools = await client.list_tools()
            dynamic = [tool for tool in tools if tool.name.startswith("bounded_tool_")]
            assert len(dynamic) == 15
            encoded_catalog = json.dumps(
                [
                    {
                        "name": tool.name,
                        "description": tool.description,
                        "inputSchema": tool.inputSchema,
                    }
                    for tool in dynamic
                ]
            ).encode()
            assert len(encoded_catalog) > config.max_ipc_message_bytes

            mutated = await _call_tool(
                client,
                "execute",
                {
                    "code": "\n".join(
                        f"bounded_tool_{index}.__annotations__['value'] = str"
                        for index in range(15)
                    )
                },
            )
            assert mutated["ok"] is True
            assert not any(
                tool.name.startswith("bounded_tool_")
                for tool in await client.list_tools()
            )

            timed_out = await _call_tool(
                client, "execute", {"code": "while True:\n    pass"}
            )
            assert timed_out["error"]["code"] == "operation_timeout"
            assert timed_out["runtime"]["namespace_state"] == "preserved"

            final_status = await _call_tool(client, "runtime_status")
            assert final_status["epoch"] == initial_status["epoch"]
            assert final_status["state"] == "ready"
            assert final_status["queue_depth"] == 0

    asyncio.run(scenario())


def test_timeout_health_publishes_stale_catalog_without_epoch_reset():
    async def scenario() -> None:
        config = ServerConfig(
            operation_timeout_seconds=0.2,
            interruption_grace_seconds=1.0,
        )
        recorder = ToolChangeRecorder()
        async with Client(create_server(config), message_handler=recorder) as client:
            await _call_tool(
                client,
                "execute",
                {"code": ("def target(value: int) -> int:\n    return value\n")},
            )
            registered = await _call_tool(client, "register_tool", {"name": "target"})
            assert registered["ok"] is True
            notifications_before_timeout = recorder.count
            initial_status = await _call_tool(client, "runtime_status")

            timed_out = await _call_tool(
                client,
                "execute",
                {
                    "code": (
                        "target.__annotations__['value'] = str\nwhile True:\n    pass"
                    )
                },
            )
            assert timed_out["error"]["code"] == "operation_timeout"
            assert timed_out["runtime"]["namespace_state"] == "preserved"
            assert timed_out["runtime"]["epoch"] == initial_status["epoch"]

            assert "target" not in {tool.name for tool in await client.list_tools()}
            stale = await _call_tool(client, "target", {"value": 1})
            assert stale["error"]["code"] == "stale_registration"
            await asyncio.sleep(0)
            assert recorder.count >= notifications_before_timeout + 1

    asyncio.run(scenario())
