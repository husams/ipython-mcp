from __future__ import annotations

import asyncio
import logging
import multiprocessing
import os
import shutil
import sys
import time
import tracemalloc
from pathlib import Path

import pytest
from fastmcp import Client
from fastmcp.client.messages import MessageHandler
from fastmcp.client.transports import StdioTransport

from ipython_mcp.config import ServerConfig
from ipython_mcp.controller import ManagedRuntime
from ipython_mcp.runtime import BoundedTextSink
from ipython_mcp.server import create_server


async def call_tool(client: Client, name: str, arguments: dict | None = None):
    result = await client.call_tool(name, arguments or {})
    return result.structured_content


async def wait_until_ready(runtime: ManagedRuntime, timeout: float = 5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = await runtime.runtime_status()
        if status.state in {"ready", "unavailable"}:
            return status
        await asyncio.sleep(0.01)
    raise AssertionError("runtime did not settle")


def test_reliability_configuration_is_positive_finite(monkeypatch):
    monkeypatch.setenv("IPYTHON_MCP_OPERATION_TIMEOUT_SECONDS", "1.5")
    monkeypatch.setenv("IPYTHON_MCP_INTERRUPTION_GRACE_SECONDS", "0.25")
    monkeypatch.setenv("IPYTHON_MCP_WORKER_STARTUP_TIMEOUT_SECONDS", "4")
    monkeypatch.setenv("IPYTHON_MCP_MAX_PENDING_OPERATIONS", "7")
    monkeypatch.setenv("IPYTHON_MCP_QUEUE_WAIT_TIMEOUT_SECONDS", "2.5")
    config = ServerConfig.from_env()
    assert config.operation_timeout_seconds == 1.5
    assert config.interruption_grace_seconds == 0.25
    assert config.worker_startup_timeout_seconds == 4
    assert config.max_pending_operations == 7
    assert config.queue_wait_timeout_seconds == 2.5
    for field, value in (
        ("operation_timeout_seconds", 0),
        ("interruption_grace_seconds", float("inf")),
        ("worker_startup_timeout_seconds", -1),
        ("queue_wait_timeout_seconds", float("nan")),
        ("max_pending_operations", 0),
    ):
        with pytest.raises(ValueError):
            ServerConfig(**{field: value})


def test_live_state_and_objects_exist_only_in_worker_process():
    async def scenario():
        runtime = ManagedRuntime(ServerConfig(worker_startup_timeout_seconds=5))
        await runtime.start()
        try:
            created = await runtime.execute(
                "import os\nworker_pid = os.getpid()\nmarker = object()\nmarker_identity = id(marker)\nworker_pid"
            )
            assert created.ok
            assert created.result != os.getpid()
            observed = await runtime.execute("(worker_pid, id(marker), marker_identity)")
            assert observed.result == [created.result, observed.result[2], observed.result[2]]
            assert observed.runtime.epoch == 1
        finally:
            await runtime.close()

    asyncio.run(scenario())


def test_cooperative_timeout_preserves_partial_mutation_identity_and_epoch():
    async def scenario():
        runtime = ManagedRuntime(
            ServerConfig(
                operation_timeout_seconds=0.08,
                interruption_grace_seconds=0.8,
                worker_startup_timeout_seconds=5,
            )
        )
        await runtime.start()
        try:
            response = await runtime.execute(
                "partial = []\nidentity = id(partial)\npartial.append('kept')\nwhile True:\n    pass"
            )
            assert response.error.code == "operation_timeout"
            assert response.runtime.namespace_state == "preserved"
            assert response.runtime.epoch == 1
            observed = await runtime.execute("(partial, id(partial), identity)")
            assert observed.result == [["kept"], observed.result[2], observed.result[2]]
            status = await runtime.runtime_status()
            assert status.state == "ready"
            assert status.latest_namespace_state == "preserved"
        finally:
            await runtime.close()

    asyncio.run(scenario())


class ToolChangeRecorder(MessageHandler):
    def __init__(self):
        self.count = 0

    async def on_tool_list_changed(self, message):
        del message
        self.count += 1


def test_non_cooperative_timeout_replaces_worker_epoch_and_catalog():
    async def scenario():
        recorder = ToolChangeRecorder()
        config = ServerConfig(
            preload_modules=("math",),
            operation_timeout_seconds=0.08,
            interruption_grace_seconds=0.05,
            worker_startup_timeout_seconds=5,
        )
        async with Client(create_server(config), message_handler=recorder) as client:
            await call_tool(
                client,
                "execute",
                {"code": "survivor = 9\ndef slow(x: int) -> int:\n    import time\n    time.sleep(2)\n    return x"},
            )
            assert (await call_tool(client, "register_tool", {"name": "slow"}))["ok"]
            timed_out = await call_tool(client, "slow", {"x": 1})
            assert timed_out["error"]["code"] == "operation_timeout"
            assert timed_out["runtime"]["namespace_state"] == "reset"
            assert timed_out["runtime"]["epoch"] == 2
            await asyncio.sleep(0)
            tools = {tool.name for tool in await client.list_tools()}
            assert "slow" not in tools
            assert recorder.count >= 2
            missing = await call_tool(client, "inspect", {"name": "survivor"})
            assert missing["error"]["code"] == "name_not_found"
            restored = await call_tool(client, "execute", {"code": "math.sqrt(9)"})
            assert restored["result"] == 3.0
            later = await call_tool(client, "execute", {"code": "after_reset = 1\nafter_reset"})
            assert later["result"] == 1
            status = await call_tool(client, "runtime_status")
            assert status["epoch"] == 2
            assert status["replacement_startup_seconds"] <= 5

    asyncio.run(scenario())


def test_active_and_queued_cancellation_preserve_fifo_and_recovery():
    async def scenario():
        runtime = ManagedRuntime(
            ServerConfig(
                operation_timeout_seconds=2,
                interruption_grace_seconds=0.8,
                queue_wait_timeout_seconds=2,
                worker_startup_timeout_seconds=5,
            )
        )
        await runtime.start()
        try:
            await runtime.execute("events = []")
            active = asyncio.create_task(
                runtime.execute("while True:\n    pass")
            )
            while not (await runtime.runtime_status()).operation_active:
                await asyncio.sleep(0.005)
            queued = asyncio.create_task(runtime.execute("events.append('cancelled')"))
            remaining = asyncio.create_task(runtime.execute("events.append('remaining')"))
            await asyncio.sleep(0.02)
            queued.cancel()
            with pytest.raises(asyncio.CancelledError):
                await queued
            active.cancel()
            with pytest.raises(asyncio.CancelledError):
                await active
            status = await wait_until_ready(runtime)
            assert status.latest_interruption_kind == "operation_cancelled"
            await remaining
            observed = await runtime.execute("events")
            assert observed.result == ["remaining"]
        finally:
            await runtime.close()

    asyncio.run(scenario())


def test_bounded_fifo_full_queue_and_queue_wait_expiry():
    async def scenario():
        runtime = ManagedRuntime(
            ServerConfig(
                operation_timeout_seconds=1,
                interruption_grace_seconds=0.5,
                max_pending_operations=1,
                queue_wait_timeout_seconds=0.05,
                worker_startup_timeout_seconds=5,
            )
        )
        await runtime.start()
        try:
            first = asyncio.create_task(runtime.execute("import time\ntime.sleep(0.4)\n'first'"))
            while not (await runtime.runtime_status()).operation_active:
                await asyncio.sleep(0.005)
            second = asyncio.create_task(runtime.execute("'second'"))
            await asyncio.sleep(0)
            busy = await runtime.execute("'rejected'")
            assert busy.error.code == "runtime_busy"
            assert busy.error.retryable is True
            assert busy.error.retry_after_seconds == 0.05
            wait_started = time.monotonic()
            expired = await second
            assert time.monotonic() - wait_started < 0.2
            assert expired.error.code == "queue_timeout"
            assert expired.error.retryable is True
            assert expired.error.retry_after_seconds == 0.05
            assert expired.execution_count is None
            assert (await first).result == "first"
            recovered = await runtime.execute("'later'")
            assert recovered.result == "later"
            assert recovered.runtime.admission_sequence > busy.runtime.admission_sequence
        finally:
            await runtime.close()

    asyncio.run(scenario())


def test_streaming_capture_memory_ceiling_and_protocol_truncation():
    chunk = "x" * 65_536

    def measure(total: int):
        tracemalloc.start()
        sink = BoundedTextSink(8_192)
        for _ in range(total // len(chunk)):
            sink.write(chunk)
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        return sink, peak

    small, peak_small = measure(8 * 1024 * 1024)
    large, peak_large = measure(64 * 1024 * 1024)
    assert small.retained_characters == large.retained_characters == 8_192
    assert small.total_characters == 8 * 1024 * 1024
    assert large.total_characters == 64 * 1024 * 1024
    assert peak_large - peak_small <= 2 * 1024 * 1024

    async def scenario():
        config = ServerConfig(
            max_text_chars=128,
            max_repr_chars=64,
            max_display_items=2,
            worker_startup_timeout_seconds=5,
        )
        async with Client(create_server(config)) as client:
            output = await call_tool(client, "execute", {"code": "print('z' * 1000000)"})
            assert len(output["stdout"]) == 128
            assert output["truncated"]["stdout"] is True
            assert output["truncated"]["stdout_omitted_chars"] == 1_000_001 - 128
            await call_tool(
                client,
                "execute",
                {"code": "class Hostile:\n    def __repr__(self):\n        return 'r' * 1000000\nhostile = Hostile()"},
            )
            inspected = await call_tool(client, "inspect", {"name": "hostile"})
            assert len(inspected["representation"]) == 64
            assert inspected["truncated"]["representation"] is True
            displayed = await call_tool(
                client,
                "execute",
                {
                    "code": "from IPython.display import display\nfor _ in range(5):\n    display({'text/plain': 'd' * 1000, 'application/json': {'payload': 'j' * 1000}}, raw=True, metadata={'note': 'm' * 1000})"
                },
            )
            assert len(displayed["display_data"]) == 2
            assert displayed["truncated"]["display_data"] is True
            for item in displayed["display_data"]:
                assert len(item["data"]["text/plain"]) == 128
                assert len(item["data"]["application/json"]["payload"]) == 128

    asyncio.run(scenario())


def test_stale_worker_response_is_discarded_and_later_request_succeeds(monkeypatch):
    async def scenario():
        runtime = ManagedRuntime(ServerConfig(worker_startup_timeout_seconds=5))
        await runtime.start()
        original_receive = runtime._receive

        async def obsolete_receive():
            message = await original_receive()
            if message.get("type") == "response":
                return {**message, "epoch": int(message["epoch"]) + 1}
            return message

        try:
            monkeypatch.setattr(runtime, "_receive", obsolete_receive)
            stale = await runtime.execute("stale_marker = 41")
            assert stale.error.code == "stale_runtime_response"
            assert stale.runtime.namespace_state == "unknown"
            monkeypatch.setattr(runtime, "_receive", original_receive)
            later = await runtime.execute("stale_marker + 1")
            assert later.result == 42
        finally:
            await runtime.close()

    asyncio.run(scenario())


def test_call_function_timeout_recovers_twice_and_succeeds_after_each():
    async def scenario():
        runtime = ManagedRuntime(
            ServerConfig(
                operation_timeout_seconds=0.06,
                interruption_grace_seconds=0.04,
                worker_startup_timeout_seconds=5,
            )
        )
        await runtime.start()
        try:
            for expected_epoch in (2, 3):
                defined = await runtime.execute(
                    "def blocking_call():\n    import time\n    time.sleep(2)\n    return 'late'"
                )
                assert defined.ok
                timed_out = await runtime.call_function("blocking_call", {})
                assert timed_out.error.code == "operation_timeout"
                assert timed_out.runtime.namespace_state == "reset"
                assert timed_out.runtime.epoch == expected_epoch
                later = await runtime.execute(f"'ready-{expected_epoch}'")
                assert later.result == f"ready-{expected_epoch}"
        finally:
            await runtime.close()

    asyncio.run(scenario())


def test_replacement_startup_failure_is_atomic_and_bounded(tmp_path: Path):
    module = tmp_path / "fragile_preload.py"
    module.write_text("VALUE = 1\n", encoding="utf-8")

    async def scenario():
        runtime = ManagedRuntime(
            ServerConfig(
                library_paths=(tmp_path,),
                preload_modules=("fragile_preload",),
                operation_timeout_seconds=0.06,
                interruption_grace_seconds=0.04,
                worker_startup_timeout_seconds=3,
            )
        )
        await runtime.start()
        try:
            assert (await runtime.execute("fragile_preload.VALUE")).result == 1
            module.unlink()
            failed = await runtime.execute("import time\ntime.sleep(2)")
            assert failed.error.code == "operation_timeout"
            assert failed.runtime.namespace_state == "unknown"
            status = await runtime.runtime_status()
            assert status.state == "unavailable"
            assert status.error.code == "worker_startup_failed"
            unavailable = await runtime.execute("1 + 1")
            assert unavailable.error.code == "runtime_unavailable"
        finally:
            await runtime.close()

    asyncio.run(scenario())


def test_bounded_shutdown_during_non_cooperative_work_leaves_no_worker():
    async def scenario():
        runtime = ManagedRuntime(
            ServerConfig(
                operation_timeout_seconds=10,
                interruption_grace_seconds=0.05,
                worker_startup_timeout_seconds=5,
            )
        )
        await runtime.start()
        active = asyncio.create_task(runtime.execute("import time\ntime.sleep(10)"))
        while not (await runtime.runtime_status()).operation_active:
            await asyncio.sleep(0.005)
        started = time.monotonic()
        await runtime.close()
        assert time.monotonic() - started < 1.5
        result = await active
        assert result.error.code == "operation_cancelled"
        await runtime.close()
        assert not any(child.name == "ipython-mcp-worker" for child in multiprocessing.active_children())

    asyncio.run(scenario())


def test_parent_logs_only_bounded_operational_metadata(caplog):
    async def scenario():
        async with Client(create_server(ServerConfig(worker_startup_timeout_seconds=5))) as client:
            await call_tool(client, "execute", {"code": "log_secret = 'never-log-me'"})
            await call_tool(client, "execute", {"code": "raise RuntimeError('trace-secret')"})

    with caplog.at_level(logging.INFO, logger="ipython_mcp.controller"):
        asyncio.run(scenario())
    assert "tool=execute" in caplog.text
    assert "request_id=" in caplog.text
    assert "epoch=" in caplog.text
    assert "log_secret" not in caplog.text
    assert "never-log-me" not in caplog.text
    assert "trace-secret" not in caplog.text


def test_subprocess_stdio_persistent_dynamic_cleanup_flow():
    project = Path(__file__).resolve().parents[1]
    transport = StdioTransport(
        command=sys.executable,
        args=["-m", "ipython_mcp.server"],
        cwd=str(project),
        env={"PATH": os.environ.get("PATH", "")},
    )

    async def scenario():
        async with Client(transport) as client:
            names = {tool.name for tool in await client.list_tools()}
            assert "runtime_status" in names
            await call_tool(
                client,
                "execute",
                {"code": "value = 5\ndef add(x: int) -> int:\n    return value + x\nclass Marker: pass"},
            )
            assert (await call_tool(client, "inspect", {"name": "Marker"}))["kind"] == "type"
            assert (await call_tool(client, "register_tool", {"name": "add"}))["ok"]
            assert "add" in {tool.name for tool in await client.list_tools()}
            assert (await call_tool(client, "add", {"x": 4}))["result"] == 9
            assert (await call_tool(client, "unregister_tool", {"names": ["add"]}))["unregistered"] == ["add"]
            assert (await call_tool(client, "remove", {"names": ["Marker"]}))["removed"] == ["Marker"]
            reset = await call_tool(client, "reset")
            assert reset["ok"]
            assert (await call_tool(client, "search", {"query": "value", "exact": True}))["results"] == []

    asyncio.run(scenario())
