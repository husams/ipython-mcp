from __future__ import annotations

import asyncio
import json
import logging
import multiprocessing
import os
import subprocess
import sys
import threading
import time
import tracemalloc
import zipfile
from pathlib import Path

import pytest
from fastmcp import Client
from fastmcp.client.transports import StdioTransport

from ipython_mcp.config import ServerConfig
from ipython_mcp.runtime import BoundedTextSink, RuntimeStartupError, ShellRuntime
from ipython_mcp.server import create_server


async def call_tool(client: Client, name: str, arguments: dict | None = None):
    result = await client.call_tool(name, arguments or {})
    return result.structured_content


def _wheel(directory: Path, version: str) -> Path:
    wheel = directory / f"taskdep-{version}-py3-none-any.whl"
    dist_info = f"taskdep-{version}.dist-info"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr(
            "taskdep/__init__.py",
            f"__version__ = {version!r}\nVALUE = {version!r}\n",
        )
        archive.writestr(
            f"{dist_info}/METADATA",
            f"Metadata-Version: 2.1\nName: taskdep\nVersion: {version}\n",
        )
        archive.writestr(
            f"{dist_info}/WHEEL",
            "Wheel-Version: 1.0\nGenerator: ipython-mcp-tests\n"
            "Root-Is-Purelib: true\nTag: py3-none-any\n",
        )
        archive.writestr(f"{dist_info}/RECORD", "")
    return wheel


def test_configuration_validates_optional_environment_and_removed_worker_fields(
    monkeypatch, tmp_path: Path
):
    monkeypatch.setenv("IPYTHON_MCP_ENVIRONMENT_WORKSPACE", str(tmp_path))
    monkeypatch.setenv("IPYTHON_MCP_ACTIVE_ENVIRONMENT", "analysis")
    monkeypatch.setenv(
        "IPYTHON_MCP_ENVIRONMENT_REQUIREMENTS", '["packaging>=24", "packaging>=24"]'
    )
    monkeypatch.setenv(
        "IPYTHON_MCP_ENVIRONMENT_VARIABLES", '{"TASK_MODE":"offline"}'
    )
    monkeypatch.setenv("IPYTHON_MCP_ENVIRONMENT_SETUP_TIMEOUT_SECONDS", "1.5")
    config = ServerConfig.from_env()
    assert config.environment_workspace == tmp_path
    assert config.active_environment == "analysis"
    assert config.environment_requirements == ("packaging>=24",)
    assert config.environment_variables == {"TASK_MODE": "offline"}
    assert config.environment_setup_timeout_seconds == 1.5
    for removed in (
        "operation_timeout_seconds",
        "interruption_grace_seconds",
        "worker_startup_timeout_seconds",
        "max_pending_operations",
        "queue_wait_timeout_seconds",
        "max_ipc_message_bytes",
    ):
        assert not hasattr(config, removed)

    with pytest.raises(ValueError, match="configured together"):
        ServerConfig(environment_workspace=tmp_path)
    with pytest.raises(ValueError, match="safe name"):
        ServerConfig(environment_workspace=tmp_path, active_environment="../escape")
    with pytest.raises(ValueError, match="absolute"):
        ServerConfig(library_paths=(Path("relative-library"),))
    with pytest.raises(ValueError, match="PEP 508"):
        ServerConfig(
            environment_workspace=tmp_path,
            active_environment="badreq",
            environment_requirements=("not a valid !!! requirement",),
        )
    with pytest.raises(ValueError, match="protected"):
        ServerConfig(preload_modules=("math",), module_aliases={"math": "open"})
    with pytest.raises(ValueError, match="matching preloads"):
        ServerConfig(module_aliases={"math": "m"})
    with pytest.raises(ValueError, match="positive finite"):
        ServerConfig(environment_setup_timeout_seconds=float("inf"))


def test_unconfigured_shell_is_direct_in_process_and_starts_no_runtime_child():
    async def scenario():
        children_before = {child.pid for child in multiprocessing.active_children()}
        runtime = ShellRuntime(ServerConfig())
        await runtime.start()
        try:
            created = await runtime.execute(
                "import os\nprocess_id = os.getpid()\nmarker = object()\n"
                "marker_id = id(marker)\n(process_id, marker_id)"
            )
            assert created.result == [os.getpid(), created.result[1]]
            observed = await runtime.execute("(process_id, id(marker), marker_id)")
            assert observed.result == [os.getpid(), observed.result[1], observed.result[1]]
            assert "runtime" not in observed.model_dump()
            assert {child.pid for child in multiprocessing.active_children()} == children_before
        finally:
            await runtime.close()

    asyncio.run(scenario())


def test_started_operation_is_not_preempted_by_timeout_or_cancellation():
    async def scenario():
        runtime = ShellRuntime(ServerConfig())
        await runtime.start()
        try:
            started = time.monotonic()
            async with asyncio.timeout(0.01):
                result = await runtime.execute("import time\ntime.sleep(0.06)\n'finished'")
            assert result.result == "finished"
            assert time.monotonic() - started >= 0.05

            loop = asyncio.get_running_loop()
            task = asyncio.create_task(
                runtime.execute("import time\ntime.sleep(0.06)\n'not-preempted'")
            )
            timer = threading.Timer(0.01, lambda: loop.call_soon_threadsafe(task.cancel))
            timer.start()
            try:
                assert (await task).result == "not-preempted"
            finally:
                timer.cancel()
            assert (await runtime.execute("'still-same-shell'" )).result == "still-same-shell"
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
        config = ServerConfig(max_text_chars=128, max_repr_chars=64, max_display_items=2)
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
                {"code": "from IPython.display import display\nfor _ in range(5):\n    display({'text/plain': 'd' * 1000, 'application/json': {'payload': 'j' * 1000}}, raw=True, metadata={'note': 'm' * 1000})"},
            )
            assert len(displayed["display_data"]) == 2
            assert displayed["truncated"]["display_data"] is True

    asyncio.run(scenario())


def test_startup_environment_applies_variables_paths_preloads_and_reuses(
    tmp_path: Path, monkeypatch
):
    workspace = tmp_path / "environments"
    library = tmp_path / "library"
    library.mkdir()
    (library / "preload_lib.py").write_text(
        "import os\nVALUE = os.environ['TASK_SETTING']\n", encoding="utf-8"
    )
    wheel = _wheel(tmp_path, "1.0")
    config = ServerConfig(
        environment_workspace=workspace,
        active_environment="alpha",
        environment_requirements=(f"taskdep @ {wheel.as_uri()}",),
        environment_variables={"TASK_SETTING": "configured-value"},
        library_paths=(library,),
        preload_modules=("taskdep", "preload_lib"),
        module_aliases={"preload_lib": "lib"},
    )
    monkeypatch.delenv("TASK_SETTING", raising=False)

    async def run_once():
        runtime = ShellRuntime(config)
        await runtime.start()
        try:
            result = await runtime.execute(
                "(taskdep.__version__, lib.VALUE, __import__('os').environ['TASK_SETTING'])"
            )
            assert result.result == ["1.0", "configured-value", "configured-value"]
            assert runtime._startup_environment.site_packages is not None
            assert str(runtime._startup_environment.site_packages) == sys.path[0]
        finally:
            await runtime.close()

    asyncio.run(run_once())
    metadata = workspace / "alpha" / ".ipython-mcp-environment.json"
    first_metadata = metadata.read_text(encoding="utf-8")
    assert "TASK_SETTING" not in first_metadata
    assert os.getenv("TASK_SETTING") is None
    for module in ("taskdep", "preload_lib"):
        sys.modules.pop(module, None)
    asyncio.run(run_once())
    assert metadata.read_text(encoding="utf-8") == first_metadata
    assert not list(workspace.glob(".alpha.tmp-*"))


def test_separate_process_startups_select_conflicting_dependency_versions(
    tmp_path: Path
):
    wheel_one = _wheel(tmp_path, "1.0")
    wheel_two = _wheel(tmp_path, "2.0")
    workspace = tmp_path / "task-environments"
    project = Path(__file__).resolve().parents[1]
    script = """
import asyncio
import json
import sys
from pathlib import Path
from ipython_mcp.config import ServerConfig
from ipython_mcp.runtime import ShellRuntime

async def main():
    runtime = ShellRuntime(ServerConfig(
        environment_workspace=Path(sys.argv[1]),
        active_environment=sys.argv[2],
        environment_requirements=(sys.argv[3],),
        preload_modules=("taskdep",),
    ))
    await runtime.start()
    result = await runtime.execute("taskdep.__version__")
    await runtime.close()
    print(json.dumps(result.result))

asyncio.run(main())
"""
    observed = []
    for name, wheel in (("one", wheel_one), ("two", wheel_two)):
        completed = subprocess.run(
            [
                sys.executable,
                "-c",
                script,
                str(workspace),
                name,
                f"taskdep @ {wheel.as_uri()}",
            ],
            cwd=project,
            capture_output=True,
            text=True,
            timeout=60,
            check=True,
        )
        observed.append(json.loads(completed.stdout.strip()))
    assert observed == ["1.0", "2.0"]


def test_startup_failures_are_phase_specific_redacted_and_clean(
    tmp_path: Path, monkeypatch
):
    workspace = tmp_path / "envs"
    secret = "never-expose-this"
    config = ServerConfig(
        environment_workspace=workspace,
        active_environment="broken",
        environment_variables={"TASK_SECRET": secret},
        environment_requirements=("private @ https://user:password@example.invalid/pkg.whl",),
        max_text_chars=160,
    )

    def failed_run(*args, **kwargs):
        del args, kwargs
        return subprocess.CompletedProcess(
            [], 1, stdout="", stderr=f"https://user:password@example.invalid {secret}"
        )

    monkeypatch.setattr(subprocess, "run", failed_run)

    async def scenario():
        runtime = ShellRuntime(config)
        with pytest.raises(RuntimeStartupError) as caught:
            await runtime.start()
        message = str(caught.value)
        assert "during provisioning" in message
        assert "password" not in message
        assert secret not in message
        assert "***" in message
        assert runtime.closed is True

    asyncio.run(scenario())
    assert not (workspace / "broken").exists()
    assert not list(workspace.glob(".broken.tmp-*"))
    assert os.getenv("TASK_SECRET") is None

    symlink = tmp_path / "workspace-link"
    symlink.symlink_to(workspace, target_is_directory=True)
    runtime = ShellRuntime(
        ServerConfig(environment_workspace=symlink, active_environment="safe")
    )
    with pytest.raises(RuntimeStartupError, match="path_validation"):
        asyncio.run(runtime.start())


def test_runtime_logs_are_metadata_only(caplog):
    async def scenario():
        async with Client(create_server()) as client:
            await call_tool(client, "execute", {"code": "log_secret = 'never-log-me'"})
            await call_tool(client, "execute", {"code": "raise RuntimeError('trace-secret')"})

    with caplog.at_level(logging.INFO, logger="ipython_mcp.runtime"):
        asyncio.run(scenario())
    assert "tool=execute" in caplog.text
    assert "log_secret" not in caplog.text
    assert "never-log-me" not in caplog.text
    assert "trace-secret" not in caplog.text


def test_worker_ipc_controller_and_status_contracts_are_absent():
    project = Path(__file__).resolve().parents[1]
    package = project / "src" / "ipython_mcp"
    for removed in ("controller.py", "worker.py", "ipc.py"):
        assert not (package / removed).exists()
    source = "\n".join(path.read_text(encoding="utf-8") for path in package.glob("*.py"))
    for removed_symbol in (
        "multiprocessing",
        "RuntimeMetadata",
        "RuntimeStatusResponse",
        "runtime_status",
        "max_ipc_message_bytes",
        "worker_startup_timeout_seconds",
        "queue_wait_timeout_seconds",
    ):
        assert removed_symbol not in source

    async def scenario():
        async with Client(create_server()) as client:
            names = {tool.name for tool in await client.list_tools()}
            assert "runtime_status" not in names
            response = await call_tool(client, "execute", {"code": "40 + 2"})
            assert response["result"] == 42
            assert "runtime" not in response

    asyncio.run(scenario())


def test_stdio_persistent_dynamic_cleanup_flow():
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
            assert "runtime_status" not in names
            await call_tool(
                client,
                "execute",
                {"code": "value = 5\ndef add(x: int) -> int:\n    return value + x\nclass Marker: pass"},
            )
            assert (await call_tool(client, "inspect", {"name": "Marker"}))["kind"] == "type"
            assert (await call_tool(client, "register_tool", {"name": "add"}))["ok"]
            assert (await call_tool(client, "add", {"x": 4}))["result"] == 9
            assert (await call_tool(client, "unregister_tool", {"names": ["add"]}))["unregistered"] == ["add"]
            assert (await call_tool(client, "remove", {"names": ["Marker"]}))["removed"] == ["Marker"]
            assert (await call_tool(client, "reset"))["ok"]

    asyncio.run(scenario())
