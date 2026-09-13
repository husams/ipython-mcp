from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

import pytest
from fastmcp import Client
from fastmcp.client.transports import StdioTransport
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
}


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

                # The marker-held loop cannot finish until release; leave
                # scheduling margin for slower locked interpreter cells.
                assert await asyncio.wait_for(client.ping(), timeout=3.0) is True
                assert execute_task.done() is False

                discovered = await asyncio.wait_for(client.list_tools(), timeout=3.0)
                assert {tool.name for tool in discovered} == expected_catalog
                rediscovered = await asyncio.wait_for(client.list_tools(), timeout=3.0)
                assert {tool.name for tool in rediscovered} == expected_catalog
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
            assert mutated["ok"] is (mutation_exit == "return")
            if mutation_exit != "return":
                assert mutated["error"]["error_type"] == (
                    "SystemExit"
                    if mutation_exit == "system_exit"
                    else "KeyboardInterrupt"
                )
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


def test_stdio_user_stdin_is_eof_and_does_not_consume_followup_requests():
    async def scenario() -> None:
        client = Client(
            StdioTransport(
                command=sys.executable,
                args=["-m", "ipython_mcp.server"],
                cwd=str(Path(__file__).resolve().parents[1]),
                env={"PATH": os.environ.get("PATH", "")},
            )
        )
        async with client:
            input_result = await asyncio.wait_for(
                _call_tool(client, "execute", {"code": "input('prompt: ')"}),
                timeout=2.0,
            )
            assert input_result["ok"] is False
            assert input_result["error"]["code"] == "execution_error"
            assert input_result["error"]["error_type"] == "EOFError"

            readline_result = await asyncio.wait_for(
                _call_tool(client, "execute", {"code": "import sys\nsys.stdin.readline()"}),
                timeout=2.0,
            )
            assert readline_result["ok"] is True
            assert readline_result["result"] == ""

            defined = await asyncio.wait_for(
                _call_tool(
                    client,
                    "execute",
                    {
                        "code": (
                            "def noisy(value: int) -> int:\n"
                            "    print('plain call output', flush=True)\n"
                            "    return value + 1\n\n"
                            "def dynamic_noisy(value: int) -> int:\n"
                            "    print('dynamic call output', flush=True)\n"
                            "    return value + 2\n"
                        )
                    },
                ),
                timeout=1.0,
            )
            assert defined["ok"] is True
            plain = await asyncio.wait_for(
                _call_tool(
                    client, "call_function", {"name": "noisy", "arguments": {"value": 1}}
                ),
                timeout=1.0,
            )
            assert plain["ok"] is True
            assert plain["result"] == 2
            registered = await asyncio.wait_for(
                _call_tool(client, "register_tool", {"name": "dynamic_noisy"}),
                timeout=1.0,
            )
            assert registered["ok"] is True
            dynamic = await asyncio.wait_for(
                _call_tool(client, "dynamic_noisy", {"value": 2}), timeout=1.0
            )
            assert dynamic["ok"] is True
            assert dynamic["result"] == 4

            tools = await asyncio.wait_for(client.list_tools(), timeout=1.0)
            assert {tool.name for tool in tools} == STABLE_TOOLS | {"dynamic_noisy"}
            followup = await asyncio.wait_for(
                _call_tool(
                    client,
                    "execute",
                    {"code": "stdin_followup = 'request survived'\nstdin_followup"},
                ),
                timeout=1.0,
            )
            assert followup["ok"] is True
            assert followup["result"] == "request survived"

    asyncio.run(scenario())
