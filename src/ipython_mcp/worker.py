"""Child-process entry point that owns all live IPython and registry objects."""

from __future__ import annotations

import asyncio
from dataclasses import asdict
from multiprocessing.connection import Connection
from typing import Any

from pydantic import BaseModel

from .config import ServerConfig
from .dynamic_tools import DynamicToolSnapshot, OperationResult
from .ipc import IpcProtocolError, receive_message, send_message
from .runtime import ShellRuntime


def _json_value(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, DynamicToolSnapshot):
        return asdict(value)
    if isinstance(value, list):
        return [_json_value(item) for item in value]
    return value


async def _dispatch(
    runtime: ShellRuntime, operation: str, payload: dict[str, Any]
) -> OperationResult[Any]:
    if operation == "execute":
        return await runtime.execute_operation(payload.get("code"))
    if operation == "list":
        return OperationResult(await runtime.list_functions())
    if operation == "call_function":
        return OperationResult(
            await runtime.call_function(payload.get("name"), payload.get("arguments"))
        )
    if operation == "search":
        return OperationResult(
            await runtime.search(
                payload.get("query"), payload.get("exact", False), payload.get("limit", 50)
            )
        )
    if operation == "reload":
        return OperationResult(await runtime.reload(payload.get("modules")))
    if operation == "inspect":
        return OperationResult(await runtime.inspect(payload.get("name")))
    if operation == "remove":
        return await runtime.remove_operation(payload.get("names"))
    if operation == "reset":
        return await runtime.reset_operation()
    if operation == "register_tool":
        return await runtime.register_tool(
            payload.get("name"), payload.get("tool_name"), payload.get("description")
        )
    if operation == "unregister_tool":
        return await runtime.unregister_tool(payload.get("names"))
    if operation == "dynamic_tools":
        return await runtime.dynamic_tools()
    if operation == "dynamic_tool":
        return await runtime.dynamic_tool(payload.get("name"))
    if operation == "call_dynamic":
        return await runtime.call_dynamic(payload.get("name"), payload.get("arguments"))
    raise ValueError("unknown worker operation")


async def _run_request(
    connection: Connection,
    runtime: ShellRuntime,
    message: dict[str, Any],
    maximum: int,
) -> None:
    try:
        outcome = await _dispatch(runtime, message["operation"], message["payload"])
    except BaseException as exc:
        response = {
            "type": "response",
            "request_id": message.get("request_id"),
            "sequence": message.get("sequence"),
            "epoch": message.get("epoch"),
            "status": "interrupted" if isinstance(exc, KeyboardInterrupt) else "error",
            "error_type": type(exc).__name__,
            "catalog_revision": runtime.catalog_revision,
            "dynamic_count": runtime.dynamic_tool_count,
        }
    else:
        response = {
            "type": "response",
            "request_id": message["request_id"],
            "sequence": message["sequence"],
            "epoch": message["epoch"],
            "status": "ok",
            "value": _json_value(outcome.value),
            "catalog_changed": outcome.catalog_changed,
            "catalog_revision": runtime.catalog_revision,
            "dynamic_count": runtime.dynamic_tool_count,
        }
    try:
        send_message(connection, response, maximum)
    except (BrokenPipeError, EOFError, IpcProtocolError, OSError):
        return


async def _worker_loop(
    connection: Connection, config: ServerConfig, maximum: int
) -> None:
    runtime = ShellRuntime(config)
    try:
        await runtime.start()
    except BaseException as exc:
        message = str(exc) or type(exc).__name__
        send_message(
            connection,
            {
                "type": "startup",
                "status": "error",
                "error_type": type(exc).__name__,
                "message": message[: config.max_text_chars],
            },
            maximum,
        )
        return
    send_message(connection, {"type": "startup", "status": "ready"}, maximum)

    active: asyncio.Task[None] | None = None
    receive: asyncio.Task[dict[str, Any]] | None = None
    try:
        while True:
            if receive is None:
                receive = asyncio.create_task(
                    asyncio.to_thread(receive_message, connection, maximum)
                )
            waits: set[asyncio.Task[Any]] = {receive}
            if active is not None:
                waits.add(active)
            done, _ = await asyncio.wait(waits, return_when=asyncio.FIRST_COMPLETED)
            if active is not None and active in done:
                await active
                active = None
            if receive not in done:
                continue
            try:
                message = receive.result()
            except (EOFError, BrokenPipeError, IpcProtocolError, OSError):
                break
            receive = None
            kind = message.get("type")
            if kind == "request":
                if active is not None:
                    send_message(
                        connection,
                        {
                            "type": "response",
                            "request_id": message.get("request_id"),
                            "sequence": message.get("sequence"),
                            "epoch": message.get("epoch"),
                            "status": "error",
                            "error_type": "WorkerBusy",
                        },
                        maximum,
                    )
                    continue
                active = asyncio.create_task(
                    _run_request(connection, runtime, message, maximum)
                )
            elif kind == "interrupt":
                runtime.request_interrupt()
            # A health probe received during the tiny interval between an
            # operation response and active-task cleanup is deliberately
            # ignored. The parent then takes the fail-safe hard-replacement
            # path; it can report reset, never a false preserved outcome.
            elif kind == "health" and active is None:
                try:
                    health = await runtime.health()
                except BaseException as exc:
                    send_message(
                        connection,
                        {
                            "type": "health",
                            "status": "error",
                            "error_type": type(exc).__name__,
                        },
                        maximum,
                    )
                else:
                    send_message(
                        connection,
                        {"type": "health", "status": "ready", **health},
                        maximum,
                    )
            elif kind == "close" and active is None:
                send_message(connection, {"type": "close", "status": "ok"}, maximum)
                break
    finally:
        if receive is not None:
            receive.cancel()
        if active is not None:
            active.cancel()
        await runtime.close()


def worker_main(connection: Connection, payload: dict[str, Any]) -> None:
    """Spawn-safe worker entry point; only bounded JSON models cross the pipe."""

    maximum = int(payload["max_ipc_message_bytes"])
    try:
        config = ServerConfig.from_worker_payload(payload)
        asyncio.run(_worker_loop(connection, config, maximum))
    finally:
        connection.close()
