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


def _catalog_map(value: Any) -> dict[str, DynamicToolSnapshot]:
    if not isinstance(value, list):
        return {}
    result: dict[str, DynamicToolSnapshot] = {}
    for item in value:
        if not isinstance(item, dict) or not isinstance(item.get("name"), str):
            continue
        try:
            snapshot = DynamicToolSnapshot(**item)
        except (TypeError, ValueError):
            continue
        result[snapshot.name] = snapshot
    return result


def _catalog_delta(
    previous: dict[str, DynamicToolSnapshot],
    current: dict[str, DynamicToolSnapshot],
) -> dict[str, list[Any]]:
    upserts: list[dict[str, Any]] = []
    for name, snapshot in current.items():
        old = previous.get(name)
        if (
            old is None
            or old.input_schema != snapshot.input_schema
            or old.description != snapshot.description
        ):
            upserts.append(asdict(snapshot))
        elif old.active != snapshot.active:
            # Tombstones retain the previously published schema in the parent,
            # so state-only updates keep large schema payloads off the pipe.
            upserts.append({"name": name, "active": snapshot.active})
    return {"upserts": upserts, "removed": sorted(set(previous) - set(current))}


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
    runtime: ShellRuntime,
    message: dict[str, Any],
    published_catalog: dict[str, DynamicToolSnapshot],
) -> tuple[dict[str, Any], dict[str, DynamicToolSnapshot]]:
    current_catalog = published_catalog
    try:
        outcome = await _dispatch(runtime, message["operation"], message["payload"])
        # Reconcile after every operation that may inspect or execute live
        # user objects.  This keeps the published catalog current even when a
        # callable mutates the namespace indirectly.
        catalog_outcome = await runtime.dynamic_tools(include_stale=True)
        current_catalog = {
            snapshot.name: snapshot for snapshot in catalog_outcome.value
        }
        catalog_changed = (
            outcome.catalog_changed
            or catalog_outcome.catalog_changed
            or current_catalog != published_catalog
        )
        catalog_delta: dict[str, list[Any]] | None = None
        if catalog_changed:
            # Catalog construction remains owner-thread work, but discovery
            # itself is served from the parent-side completed snapshot.  Keep
            # stale entries here so a previously advertised tool can still
            # return its explicit stale-schema error through ``get_tool``.
            catalog_delta = _catalog_delta(published_catalog, current_catalog)
        response = {
            "type": "response",
            "request_id": message["request_id"],
            "sequence": message["sequence"],
            "epoch": message["epoch"],
            "status": "ok",
            "value": _json_value(outcome.value),
            "catalog_changed": catalog_changed,
            "catalog_revision": runtime.catalog_revision,
            "dynamic_count": runtime.dynamic_tool_count,
        }
        if catalog_delta is not None:
            response["catalog_delta"] = catalog_delta
    except BaseException as exc:
        catalog_changed = False
        catalog_delta: dict[str, list[Any]] | None = None
        try:
            catalog_outcome = await runtime.dynamic_tools(include_stale=True)
            current_catalog = {
                snapshot.name: snapshot for snapshot in catalog_outcome.value
            }
            catalog_changed = current_catalog != published_catalog
            if catalog_changed:
                catalog_delta = _catalog_delta(published_catalog, current_catalog)
        except BaseException:
            current_catalog = published_catalog
        response = {
            "type": "response",
            "request_id": message.get("request_id"),
            "sequence": message.get("sequence"),
            "epoch": message.get("epoch"),
            "status": "interrupted" if isinstance(exc, KeyboardInterrupt) else "error",
            "error_type": type(exc).__name__,
            "catalog_changed": catalog_changed,
            "catalog_revision": runtime.catalog_revision,
            "dynamic_count": runtime.dynamic_tool_count,
        }
        if catalog_delta is not None:
            response["catalog_delta"] = catalog_delta
    return response, current_catalog


async def _worker_loop(
    connection: Connection, config: ServerConfig, maximum: int
) -> None:
    runtime = ShellRuntime(config)

    async def send(message: dict[str, Any]) -> None:
        await asyncio.to_thread(send_message, connection, message, maximum)

    try:
        await runtime.start()
    except BaseException as exc:
        message = str(exc) or type(exc).__name__
        await send(
            {
                "type": "startup",
                "status": "error",
                "error_type": type(exc).__name__,
                "message": message[: config.max_text_chars],
            }
        )
        return
    await send({"type": "startup", "status": "ready"})

    active: asyncio.Task[
        tuple[dict[str, Any], dict[str, DynamicToolSnapshot]]
    ] | None = None
    published_catalog: dict[str, DynamicToolSnapshot] = {}
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
                try:
                    response, published_catalog = active.result()
                except (BrokenPipeError, EOFError, IpcProtocolError, OSError):
                    break
                active = None
                try:
                    await send(response)
                except (BrokenPipeError, EOFError, IpcProtocolError, OSError):
                    break
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
                    await send(
                        {
                            "type": "response",
                            "request_id": message.get("request_id"),
                            "sequence": message.get("sequence"),
                            "epoch": message.get("epoch"),
                            "status": "error",
                            "error_type": "WorkerBusy",
                        }
                    )
                    continue
                active = asyncio.create_task(
                    _run_request(runtime, message, published_catalog)
                )
            elif kind == "interrupt":
                runtime.request_interrupt()
            elif kind == "health" and active is None:
                try:
                    health = await runtime.health()
                except BaseException as exc:
                    await send(
                        {
                            "type": "health",
                            "status": "error",
                            "error_type": type(exc).__name__,
                        }
                    )
                else:
                    health_catalog = _catalog_map(health.get("dynamic_tools"))
                    previous_catalog = published_catalog
                    health_changed = bool(health.get("catalog_changed")) or (
                        health_catalog != previous_catalog
                    )
                    published_catalog = health_catalog
                    health.pop("dynamic_tools", None)
                    health = {
                        **health,
                        "dynamic_count": sum(
                            1 for snapshot in health_catalog.values() if snapshot.active
                        ),
                        "catalog_changed": health_changed,
                        "catalog_delta": _catalog_delta(
                            previous_catalog, health_catalog
                        ),
                    }
                    # Build the delta against the catalog that preceded the
                    # health probe; retain only bounded state changes.
                    await send({"type": "health", "status": "ready", **health})
            elif kind == "close" and active is None:
                await send({"type": "close", "status": "ok"})
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
