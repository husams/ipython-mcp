"""FastMCP server exposing the stable IPython namespace interface."""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any

from fastmcp import FastMCP
from fastmcp.server.context import Context
from fastmcp.tools import ToolResult

from .compact import as_tool_result
from .config import ServerConfig
from .controller import ManagedRuntime
from .models import (
    CallFunctionResponse,
    ExecuteResponse,
    InspectResponse,
    ListResponse,
    ReloadResponse,
    RemoveResponse,
    ResetResponse,
    RuntimeStatusResponse,
    RegisterToolResponse,
    SearchResponse,
    UnregisterToolResponse,
)
from .provider import DynamicToolProvider, notify_tool_list_changed, runtime_from_context


def _runtime(ctx: Context) -> ManagedRuntime:
    state: Any = ctx.request_context.lifespan_context
    return state["runtime"]


def create_server(config: ServerConfig | None = None) -> FastMCP:
    """Build a server with one lifespan-owned runtime."""

    server_config = config or ServerConfig.from_env()

    @asynccontextmanager
    async def lifespan(_: FastMCP):
        runtime = ManagedRuntime(server_config)
        await runtime.start()
        try:
            yield {"runtime": runtime}
        finally:
            await runtime.close()

    instructions = (
        "One persistent Python namespace. Batch imports, helper calls, artifact "
        "writes and assertions in execute; retain results for later steps. "
        "Use runtime_status only for busy or interrupted work. Replies are one "
        "JSON text object; check ok, error, truncated, and runtime.epoch. "
        "A changed epoch means the namespace was replaced."
        if server_config.profile == "compact"
        else "Execute trusted local Python in one persistent IPython namespace."
    )
    server = FastMCP(
        "ipython-mcp",
        instructions=instructions,
        lifespan=lifespan,
        mask_error_details=True,
    )
    if server_config.profile == "compact":
        server.tool(
            name="execute",
            description="Run Python code; check ok, error, truncated, and epoch.",
            output_schema=None,
        )(_compact_execute)
        server.tool(
            name="call_function",
            description="Call a live function when its direct return is sufficient; otherwise use execute to retain and process the result.",
            output_schema=None,
        )(_compact_call_function)
        server.tool(
            name="runtime_status",
            description="Report runtime state and epoch; changed epoch means reset.",
            output_schema=None,
        )(_compact_runtime_status)
    else:
        server.tool(name="list")(list_functions)
        server.tool(name="execute")(execute)
        server.tool(name="call_function")(call_function)
        server.tool(name="search")(search)
        server.tool(name="reload")(reload)
        server.tool(name="inspect")(inspect_name)
        server.tool(name="remove")(remove)
        server.tool(name="reset")(reset)
        server.tool(name="register_tool")(register_tool)
        server.tool(name="unregister_tool")(unregister_tool)
        server.tool(name="runtime_status")(runtime_status)
        server.add_provider(DynamicToolProvider())
    return server


async def list_functions(ctx: Context) -> ListResponse:
    """List callable functions currently available in the live namespace."""

    outcome = await _runtime(ctx).list_functions_operation()
    await notify_tool_list_changed(ctx, outcome.catalog_changed)
    return outcome.value


async def execute(code: str, ctx: Context) -> ExecuteResponse:
    """Execute Python source in the persistent IPython namespace."""

    outcome = await _runtime(ctx).execute_operation(code)
    await notify_tool_list_changed(ctx, outcome.catalog_changed)
    return outcome.value


async def call_function(name: str, arguments: dict[str, Any] | str, ctx: Context) -> CallFunctionResponse:
    """Call a live callable by name with a JSON object of keyword arguments."""

    outcome = await _runtime(ctx).call_function_operation(name, arguments)
    await notify_tool_list_changed(ctx, outcome.catalog_changed)
    return outcome.value


async def search(query: str, exact: bool = False, limit: int = 50, ctx: Context | None = None) -> SearchResponse:
    """Search visible live namespace objects by exact or partial name."""

    if ctx is None:
        raise RuntimeError("request context is required")
    outcome = await _runtime(ctx).search_operation(query, exact, limit)
    await notify_tool_list_changed(ctx, outcome.catalog_changed)
    return outcome.value


async def reload(modules: list[str], ctx: Context) -> ReloadResponse:
    """Reload explicitly named imported modules and refresh their bindings."""

    outcome = await _runtime(ctx).reload_operation(modules)
    await notify_tool_list_changed(ctx, outcome.catalog_changed)
    return outcome.value


async def inspect_name(name: str, ctx: Context) -> InspectResponse:
    """Inspect one live name with bounded metadata and explicit truncation flags."""

    outcome = await _runtime(ctx).inspect_operation(name)
    await notify_tool_list_changed(ctx, outcome.catalog_changed)
    return outcome.value


async def remove(names: list[str], ctx: Context) -> RemoveResponse:
    """Remove unprotected top-level names and report every requested partition."""

    outcome = await _runtime(ctx).remove_operation(names)
    await notify_tool_list_changed(ctx, outcome.catalog_changed)
    return outcome.value


async def reset(ctx: Context) -> ResetResponse:
    """Remove user-created names while preserving runtime and configured bindings."""

    outcome = await _runtime(ctx).reset_operation()
    await notify_tool_list_changed(ctx, outcome.catalog_changed)
    return outcome.value


async def register_tool(
    name: str,
    tool_name: str | None = None,
    description: str | None = None,
    ctx: Context | None = None,
) -> RegisterToolResponse:
    """Explicitly publish one supported top-level synchronous live callable."""

    if ctx is None:
        raise RuntimeError("request context is required")
    outcome = await runtime_from_context(ctx).register_tool(name, tool_name, description)
    await notify_tool_list_changed(ctx, outcome.catalog_changed)
    return outcome.value


async def unregister_tool(
    names: list[str], ctx: Context
) -> UnregisterToolResponse:
    """Remove requested dynamic tool registrations without affecting stable tools."""

    outcome = await _runtime(ctx).unregister_tool(names)
    await notify_tool_list_changed(ctx, outcome.catalog_changed)
    return outcome.value


async def runtime_status(ctx: Context) -> RuntimeStatusResponse:
    """Report bounded out-of-band worker, queue, epoch, and recovery metadata."""

    return await _runtime(ctx).runtime_status()


async def _compact_execute(code: str, ctx: Context) -> ToolResult:
    """Run Python code and return only meaningful response fields."""

    return as_tool_result((await _runtime(ctx).execute_operation(code)).value)


async def _compact_call_function(
    name: str, arguments: dict[str, Any] | str, ctx: Context
) -> ToolResult:
    """Call a live function and return a bounded structured result."""

    return as_tool_result(
        (await _runtime(ctx).call_function_operation(name, arguments)).value
    )


async def _compact_runtime_status(ctx: Context) -> ToolResult:
    """Report readiness and the current namespace epoch."""

    return as_tool_result(await _runtime(ctx).runtime_status())


mcp = create_server()


def main() -> None:
    """Run the default server over stdio."""

    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
