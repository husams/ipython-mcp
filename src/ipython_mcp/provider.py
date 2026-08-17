"""FastMCP provider that projects lifespan-owned registrations on demand."""

from __future__ import annotations

import contextlib
from typing import Any, Sequence

from fastmcp.server.context import Context
from fastmcp.server.dependencies import get_context
from fastmcp.server.providers import Provider
from fastmcp.tools import Tool, ToolResult

from .models import CallFunctionResponse
from .controller import ManagedRuntime


def runtime_from_context(ctx: Context) -> ManagedRuntime:
    runtime = ctx.lifespan_context.get("runtime")
    if not isinstance(runtime, ManagedRuntime):
        raise RuntimeError("IPython runtime is unavailable outside the server lifespan")
    return runtime


async def notify_tool_list_changed(ctx: Context, changed: bool) -> None:
    """Notify an active MCP session without making notification loss fatal."""

    if not changed or ctx.request_context is None:
        return
    sender = getattr(ctx.session, "send_tool_list_changed", None)
    if sender is None:
        return
    with contextlib.suppress(Exception):
        await sender()


class RegisteredDynamicTool(Tool):
    """A schema snapshot whose invocation revalidates the current live callable."""

    async def run(self, arguments: dict[str, Any]) -> ToolResult:
        ctx = get_context()
        outcome = await runtime_from_context(ctx).call_dynamic(self.name, arguments)
        await notify_tool_list_changed(ctx, outcome.catalog_changed)
        response = outcome.value.model_dump(mode="json")
        return ToolResult(structured_content=response)


class DynamicToolProvider(Provider):
    """Resolve active and stale dynamic tools from the current lifespan runtime."""

    async def _list_tools(self) -> Sequence[Tool]:
        ctx = get_context()
        outcome = await runtime_from_context(ctx).dynamic_tools()
        await notify_tool_list_changed(ctx, outcome.catalog_changed)
        return [self._tool(snapshot) for snapshot in outcome.value]

    async def _get_tool(self, name: str, version: Any = None) -> Tool | None:
        del version
        ctx = get_context()
        outcome = await runtime_from_context(ctx).dynamic_tool(name)
        await notify_tool_list_changed(ctx, outcome.catalog_changed)
        if outcome.value is None:
            return None
        return self._tool(outcome.value)

    @staticmethod
    def _tool(snapshot: Any) -> RegisteredDynamicTool:
        return RegisteredDynamicTool(
            name=snapshot.name,
            description=snapshot.description,
            parameters=snapshot.input_schema,
            output_schema=CallFunctionResponse.model_json_schema(),
        )
