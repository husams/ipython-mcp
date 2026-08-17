from __future__ import annotations

import asyncio
import shutil
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

from fastmcp import Client
from fastmcp.client.transports import StdioTransport


def _copy_clean_source(root: Path, destination: Path) -> None:
    shutil.copytree(root / "src", destination / "src")
    for name in ("README.md", "pyproject.toml", "uv.lock"):
        shutil.copy2(root / name, destination / name)


def test_clean_build_artifacts_and_installed_console_stdio(tmp_path: Path):
    root = Path(__file__).resolve().parents[1]
    source = tmp_path / "source"
    source.mkdir()
    _copy_clean_source(root, source)
    distribution = tmp_path / "dist"
    subprocess.run(
        ["uv", "build", "--out-dir", str(distribution)],
        cwd=source,
        check=True,
        capture_output=True,
        text=True,
    )
    wheel = next(distribution.glob("ipython_mcp-0.1.0-*.whl"))
    source_archive = distribution / "ipython_mcp-0.1.0.tar.gz"
    assert source_archive.exists()

    forbidden = ("tests/", "__pycache__", ".pyc", ".venv", ".backlog", ".env")
    with zipfile.ZipFile(wheel) as archive:
        wheel_names = archive.namelist()
        assert "ipython_mcp/server.py" in wheel_names
        assert any(name.endswith(".dist-info/entry_points.txt") for name in wheel_names)
        metadata_name = next(name for name in wheel_names if name.endswith(".dist-info/METADATA"))
        metadata = archive.read(metadata_name).decode()
        assert "Version: 0.1.0" in metadata
        assert "Requires-Python: >=3.11" in metadata
        assert "Requires-Dist: fastmcp<4,>=3.1" in metadata
        assert not any(marker in name for name in wheel_names for marker in forbidden)
    with tarfile.open(source_archive) as archive:
        source_names = archive.getnames()
        assert any(name.endswith("/README.md") for name in source_names)
        assert any(name.endswith("/src/ipython_mcp/server.py") for name in source_names)
        assert not any(marker in name for name in source_names for marker in forbidden)

    environment = tmp_path / "clean-venv"
    subprocess.run(
        ["uv", "venv", "--python", f"{sys.version_info.major}.{sys.version_info.minor}", str(environment)],
        check=True,
        capture_output=True,
        text=True,
    )
    python = environment / "bin" / "python"
    subprocess.run(
        ["uv", "pip", "install", "--python", str(python), str(wheel)],
        check=True,
        capture_output=True,
        text=True,
    )
    executable = environment / "bin" / "ipython-mcp"

    async def scenario():
        async with Client(StdioTransport(command=str(executable), args=[])) as client:
            tools = {tool.name for tool in await client.list_tools()}
            assert "runtime_status" in tools
            executed = await client.call_tool(
                "execute",
                {
                    "code": "release_value = 5\ndef release_add(x: int) -> int:\n    return release_value + x\nclass ReleaseMarker: pass"
                },
            )
            assert executed.structured_content["ok"] is True
            inspected = await client.call_tool("inspect", {"name": "ReleaseMarker"})
            assert inspected.structured_content["kind"] == "type"
            registered = await client.call_tool("register_tool", {"name": "release_add"})
            assert registered.structured_content["ok"] is True
            assert "release_add" in {tool.name for tool in await client.list_tools()}
            invoked = await client.call_tool("release_add", {"x": 4})
            assert invoked.structured_content["result"] == 9
            retained = await client.call_tool("execute", {"code": "release_value"})
            assert retained.structured_content["result"] == 5
            unregistered = await client.call_tool(
                "unregister_tool", {"names": ["release_add"]}
            )
            assert unregistered.structured_content["unregistered"] == ["release_add"]
            removed = await client.call_tool("remove", {"names": ["ReleaseMarker"]})
            assert removed.structured_content["removed"] == ["ReleaseMarker"]
            reset = await client.call_tool("reset", {})
            assert reset.structured_content["ok"] is True
            searched = await client.call_tool(
                "search", {"query": "release_value", "exact": True}
            )
            assert searched.structured_content["results"] == []

    asyncio.run(scenario())
