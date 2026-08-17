"""Parent-side admission, IPC, interruption, and worker-replacement controller."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import multiprocessing
import sys
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from multiprocessing.connection import Connection
from typing import Any, Generic, TypeVar

from pydantic import BaseModel

from .config import ServerConfig
from .dynamic_tools import DynamicToolSnapshot, OperationResult
from .ipc import IpcProtocolError, encode_message, receive_message, send_message
from .models import (
    CallFunctionResponse,
    ErrorInfo,
    ExecuteResponse,
    InspectResponse,
    ListResponse,
    RegisterToolResponse,
    ReloadResponse,
    RemoveResponse,
    ResetResponse,
    RuntimeMetadata,
    RuntimeStatusResponse,
    SearchResponse,
    UnregisterToolResponse,
)
from .worker import worker_main

logger = logging.getLogger(__name__)
T = TypeVar("T")


@dataclass
class _Pending(Generic[T]):
    sequence: int
    request_id: str
    operation: str
    payload: dict[str, Any]
    enqueued_at: float
    future: asyncio.Future[OperationResult[T]]
    dispatched: bool = False
    cancelled: bool = False
    interruption_kind: str | None = None
    interrupt_event: asyncio.Event = field(default_factory=asyncio.Event)


class ManagedRuntime:
    """Own one killable worker and serialize all namespace operations FIFO."""

    def __init__(self, config: ServerConfig):
        self.config = config
        self._context = multiprocessing.get_context("spawn")
        self._process: multiprocessing.Process | None = None
        self._connection: Connection | None = None
        self._queue: deque[_Pending[Any]] = deque()
        self._sequence = 0
        self._epoch = 0
        self._active: _Pending[Any] | None = None
        self._wake = asyncio.Event()
        self._lock = asyncio.Lock()
        self._dispatcher: asyncio.Task[None] | None = None
        self._state = "recovering"
        self._closed = False
        self._closing = False
        self._latest_interruption_kind: str | None = None
        self._latest_namespace_state = "unchanged"
        self._replacement_startup_seconds: float | None = None
        self._startup_error: ErrorInfo | None = None
        self._dynamic_count = 0
        self._catalog_revision = 0
        self._catalog_notification_pending = False

    @property
    def closed(self) -> bool:
        return self._closed

    async def start(self) -> None:
        if self._dispatcher is not None:
            return
        await self._start_worker(replacement=False)
        self._dispatcher = asyncio.create_task(
            self._dispatch_loop(), name="ipython-mcp-admission"
        )

    async def close(self) -> None:
        if self._closed:
            return
        self._closing = True
        self._state = "closed"
        async with self._lock:
            queued = list(self._queue)
            self._queue.clear()
            active = self._active
            if active is not None:
                active.interruption_kind = "operation_cancelled"
                active.interrupt_event.set()
        for pending in queued:
            if not pending.future.done():
                pending.future.set_result(
                    self._failure(
                        pending,
                        "runtime_closed",
                        "the runtime is closed",
                        namespace_state="unchanged",
                    )
                )
        if active is not None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(
                    asyncio.shield(active.future),
                    timeout=self.config.interruption_grace_seconds + 0.5,
                )
        await self._terminate_worker()
        if self._dispatcher is not None:
            self._dispatcher.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._dispatcher
        self._closed = True
        self._state = "closed"

    async def runtime_status(self) -> RuntimeStatusResponse:
        return RuntimeStatusResponse(
            state=self._state,
            epoch=self._epoch,
            queue_depth=len(self._queue),
            operation_active=self._active is not None,
            latest_interruption_kind=self._latest_interruption_kind,
            latest_namespace_state=self._latest_namespace_state,
            replacement_startup_seconds=self._replacement_startup_seconds,
            error=self._startup_error,
        )

    async def execute_operation(self, code: str) -> OperationResult[ExecuteResponse]:
        return await self._submit("execute", {"code": code})

    async def execute(self, code: str) -> ExecuteResponse:
        return (await self.execute_operation(code)).value

    async def list_functions(self) -> ListResponse:
        return (await self.list_functions_operation()).value

    async def list_functions_operation(self) -> OperationResult[ListResponse]:
        return await self._submit("list", {})

    async def call_function(
        self, name: str, arguments: dict[str, Any] | str
    ) -> CallFunctionResponse:
        return (await self.call_function_operation(name, arguments)).value

    async def call_function_operation(
        self, name: str, arguments: dict[str, Any] | str
    ) -> OperationResult[CallFunctionResponse]:
        return await self._submit(
            "call_function", {"name": name, "arguments": arguments}
        )

    async def search(self, query: str, exact: bool, limit: int) -> SearchResponse:
        return (await self.search_operation(query, exact, limit)).value

    async def search_operation(
        self, query: str, exact: bool, limit: int
    ) -> OperationResult[SearchResponse]:
        return await self._submit(
            "search", {"query": query, "exact": exact, "limit": limit}
        )

    async def reload(self, modules: list[str]) -> ReloadResponse:
        return (await self.reload_operation(modules)).value

    async def reload_operation(self, modules: list[str]) -> OperationResult[ReloadResponse]:
        return await self._submit("reload", {"modules": modules})

    async def inspect(self, name: str) -> InspectResponse:
        return (await self.inspect_operation(name)).value

    async def inspect_operation(self, name: str) -> OperationResult[InspectResponse]:
        return await self._submit("inspect", {"name": name})

    async def remove_operation(self, names: list[str]) -> OperationResult[RemoveResponse]:
        return await self._submit("remove", {"names": names})

    async def reset_operation(self) -> OperationResult[ResetResponse]:
        return await self._submit("reset", {})

    async def register_tool(
        self, name: str, tool_name: str | None, description: str | None
    ) -> OperationResult[RegisterToolResponse]:
        return await self._submit(
            "register_tool",
            {"name": name, "tool_name": tool_name, "description": description},
        )

    async def unregister_tool(
        self, names: list[str]
    ) -> OperationResult[UnregisterToolResponse]:
        return await self._submit("unregister_tool", {"names": names})

    async def dynamic_tools(self) -> OperationResult[list[DynamicToolSnapshot]]:
        return await self._submit("dynamic_tools", {})

    async def dynamic_tool(
        self, name: str
    ) -> OperationResult[DynamicToolSnapshot | None]:
        return await self._submit("dynamic_tool", {"name": name})

    async def call_dynamic(
        self, name: str, arguments: dict[str, Any]
    ) -> OperationResult[CallFunctionResponse]:
        return await self._submit(
            "call_dynamic", {"name": name, "arguments": arguments}
        )

    async def _submit(self, operation: str, payload: dict[str, Any]) -> OperationResult[Any]:
        loop = asyncio.get_running_loop()
        async with self._lock:
            self._sequence += 1
            pending = _Pending(
                sequence=self._sequence,
                request_id=uuid.uuid4().hex,
                operation=operation,
                payload=payload,
                enqueued_at=time.monotonic(),
                future=loop.create_future(),
            )
            if self._closed or self._closing:
                return self._failure(
                    pending, "runtime_closed", "the runtime is closed"
                )
            if self._state == "unavailable":
                return self._failure(
                    pending,
                    "runtime_unavailable",
                    "the runtime worker is unavailable",
                    retryable=True,
                )
            try:
                encode_message(
                    {
                        "type": "request",
                        "request_id": pending.request_id,
                        "sequence": pending.sequence,
                        "epoch": self._epoch,
                        "operation": operation,
                        "payload": payload,
                    },
                    self.config.max_ipc_message_bytes,
                )
            except IpcProtocolError:
                return self._failure(
                    pending,
                    "ipc_message_too_large",
                    "the operation exceeds the bounded IPC request size",
                )
            if len(self._queue) >= self.config.max_pending_operations:
                return self._failure(
                    pending,
                    "runtime_busy",
                    "the pending operation queue is full",
                    retryable=True,
                    retry_after=self.config.queue_wait_timeout_seconds,
                )
            self._queue.append(pending)
            self._wake.set()
        try:
            try:
                outcome = await asyncio.wait_for(
                    asyncio.shield(pending.future),
                    timeout=self.config.queue_wait_timeout_seconds,
                )
            except asyncio.TimeoutError:
                async with self._lock:
                    if pending.dispatched:
                        outcome = None
                    else:
                        pending.cancelled = True
                        with contextlib.suppress(ValueError):
                            self._queue.remove(pending)
                        outcome = self._failure(
                            pending,
                            "queue_timeout",
                            "the operation expired before dispatch",
                            queue_wait=time.monotonic() - pending.enqueued_at,
                            retryable=True,
                            retry_after=self.config.queue_wait_timeout_seconds,
                        )
                        if not pending.future.done():
                            pending.future.set_result(outcome)
                if outcome is None:
                    # Dispatch won the timeout race. From this point the
                    # operation deadline, rather than the queue deadline,
                    # owns completion.
                    outcome = await asyncio.shield(pending.future)
            async with self._lock:
                changed = outcome.catalog_changed or self._catalog_notification_pending
                if changed:
                    self._catalog_notification_pending = False
            return OperationResult(outcome.value, changed)
        except asyncio.CancelledError:
            async with self._lock:
                if not pending.dispatched:
                    pending.cancelled = True
                    with contextlib.suppress(ValueError):
                        self._queue.remove(pending)
                    if not pending.future.done():
                        pending.future.cancel()
                    self._latest_interruption_kind = "operation_cancelled"
                    self._latest_namespace_state = "unchanged"
                else:
                    pending.interruption_kind = "operation_cancelled"
                    pending.interrupt_event.set()
            raise

    async def _dispatch_loop(self) -> None:
        while True:
            await self._wake.wait()
            self._wake.clear()
            while True:
                async with self._lock:
                    while self._queue and self._queue[0].cancelled:
                        self._queue.popleft()
                    if not self._queue or self._closing:
                        break
                    pending = self._queue.popleft()
                    pending.dispatched = True
                    self._active = pending
                    self._state = "busy"
                queue_wait = time.monotonic() - pending.enqueued_at
                if queue_wait >= self.config.queue_wait_timeout_seconds:
                    outcome = self._failure(
                        pending,
                        "queue_timeout",
                        "the operation expired before dispatch",
                        queue_wait=queue_wait,
                        retryable=True,
                        retry_after=self.config.queue_wait_timeout_seconds,
                    )
                else:
                    outcome = await self._execute_pending(pending, queue_wait)
                if not pending.future.done():
                    pending.future.set_result(outcome)
                async with self._lock:
                    if self._active is pending:
                        self._active = None
                    if not self._closing and self._state not in {"unavailable", "recovering"}:
                        self._state = "ready"
                    if self._queue:
                        self._wake.set()

    async def _execute_pending(
        self, pending: _Pending[Any], queue_wait: float
    ) -> OperationResult[Any]:
        if self._connection is None or self._process is None or not self._process.is_alive():
            reset = await self._hard_replace()
            if not reset:
                return self._failure(
                    pending, "runtime_unavailable", "the runtime worker is unavailable"
                )
        request = {
            "type": "request",
            "request_id": pending.request_id,
            "sequence": pending.sequence,
            "epoch": self._epoch,
            "operation": pending.operation,
            "payload": pending.payload,
        }
        try:
            await self._send(request)
        except (BrokenPipeError, EOFError, IpcProtocolError, OSError):
            await self._hard_replace()
            return self._failure(
                pending,
                "ipc_failure",
                "the runtime request could not be transferred",
                namespace_state="reset",
                retryable=True,
            )

        receive = asyncio.create_task(self._receive())
        timeout = asyncio.create_task(asyncio.sleep(self.config.operation_timeout_seconds))
        interrupt = asyncio.create_task(pending.interrupt_event.wait())
        done, _ = await asyncio.wait(
            {receive, timeout, interrupt}, return_when=asyncio.FIRST_COMPLETED
        )
        if receive in done:
            timeout.cancel()
            interrupt.cancel()
            try:
                message = receive.result()
            except (BrokenPipeError, EOFError, IpcProtocolError, OSError):
                await self._hard_replace()
                return self._failure(
                    pending,
                    "ipc_failure",
                    "the runtime response could not be transferred",
                    namespace_state="reset",
                    retryable=True,
                )
            return self._decode_response(pending, message, queue_wait)

        timeout.cancel()
        interrupt.cancel()
        kind = pending.interruption_kind or "operation_timeout"
        namespace_state, catalog_changed = await self._interrupt_and_recover(
            pending, receive, kind
        )
        return self._failure(
            pending,
            kind,
            "the dispatched operation was cancelled"
            if kind == "operation_cancelled"
            else "the dispatched operation exceeded its deadline",
            queue_wait=queue_wait,
            namespace_state=namespace_state,
            catalog_changed=catalog_changed,
            retryable=True,
        )

    async def _interrupt_and_recover(
        self,
        pending: _Pending[Any],
        receive: asyncio.Task[dict[str, Any]],
        kind: str,
    ) -> tuple[str, bool]:
        self._state = "recovering"
        self._latest_interruption_kind = kind
        try:
            await self._send(
                {
                    "type": "interrupt",
                    "request_id": pending.request_id,
                    "sequence": pending.sequence,
                    "epoch": self._epoch,
                }
            )
            await asyncio.wait_for(
                asyncio.shield(receive), timeout=self.config.interruption_grace_seconds
            )
        except (asyncio.TimeoutError, BrokenPipeError, EOFError, IpcProtocolError, OSError):
            receive.cancel()
            changed = self._dynamic_count > 0
            if self._closing:
                await self._terminate_worker()
                self._latest_namespace_state = "reset"
                return "reset", changed
            replaced = await self._hard_replace()
            self._latest_namespace_state = "reset" if replaced else "unknown"
            return self._latest_namespace_state, changed

        try:
            await self._send({"type": "health", "epoch": self._epoch})
            health = await asyncio.wait_for(
                self._receive(), timeout=self.config.interruption_grace_seconds
            )
            if health.get("type") != "health" or health.get("status") != "ready":
                raise IpcProtocolError("worker health probe failed")
        except (asyncio.TimeoutError, BrokenPipeError, EOFError, IpcProtocolError, OSError):
            changed = self._dynamic_count > 0
            replaced = False if self._closing else await self._hard_replace()
            self._latest_namespace_state = "reset" if replaced else "unknown"
            return self._latest_namespace_state, changed
        self._catalog_revision = int(health.get("catalog_revision", self._catalog_revision))
        self._dynamic_count = len(health.get("dynamic_tools", []))
        changed = bool(health.get("catalog_changed", False))
        self._state = "ready"
        self._latest_namespace_state = "preserved"
        return "preserved", changed

    def _decode_response(
        self, pending: _Pending[Any], message: dict[str, Any], queue_wait: float
    ) -> OperationResult[Any]:
        if (
            message.get("type") != "response"
            or message.get("request_id") != pending.request_id
            or message.get("sequence") != pending.sequence
            or message.get("epoch") != self._epoch
        ):
            return self._failure(
                pending,
                "stale_runtime_response",
                "an obsolete runtime response was discarded",
                namespace_state="unknown",
                retryable=True,
            )
        if message.get("status") != "ok":
            return self._failure(
                pending,
                "worker_operation_error",
                "the worker operation failed before producing a protocol result",
                retryable=True,
            )
        self._catalog_revision = int(message.get("catalog_revision", self._catalog_revision))
        self._dynamic_count = int(message.get("dynamic_count", self._dynamic_count))
        value = self._model_value(pending, message.get("value"), queue_wait)
        logger.info(
            "tool=%s request_id=%s epoch=%s elapsed_outcome=ok truncated=%s",
            pending.operation,
            pending.request_id,
            self._epoch,
            self._response_truncated(value),
        )
        return OperationResult(value, bool(message.get("catalog_changed", False)))

    def _model_value(self, pending: _Pending[Any], value: Any, queue_wait: float) -> Any:
        if pending.operation == "dynamic_tools":
            return [DynamicToolSnapshot(**item) for item in (value or [])]
        if pending.operation == "dynamic_tool":
            return None if value is None else DynamicToolSnapshot(**value)
        model = {
            "execute": ExecuteResponse,
            "list": ListResponse,
            "call_function": CallFunctionResponse,
            "search": SearchResponse,
            "reload": ReloadResponse,
            "inspect": InspectResponse,
            "remove": RemoveResponse,
            "reset": ResetResponse,
            "register_tool": RegisterToolResponse,
            "unregister_tool": UnregisterToolResponse,
            "call_dynamic": CallFunctionResponse,
        }[pending.operation]
        data = dict(value)
        data["runtime"] = self._metadata(pending, queue_wait=queue_wait).model_dump()
        return model.model_validate(data)

    def _failure(
        self,
        pending: _Pending[Any],
        code: str,
        message: str,
        *,
        queue_wait: float = 0.0,
        namespace_state: str = "unchanged",
        catalog_changed: bool = False,
        retryable: bool = False,
        retry_after: float | None = None,
    ) -> OperationResult[Any]:
        error = ErrorInfo(
            code=code,
            message=message,
            retryable=retryable,
            retry_after_seconds=retry_after,
        )
        metadata = self._metadata(
            pending,
            queue_wait=queue_wait,
            interruption_kind=code
            if code in {"operation_timeout", "operation_cancelled"}
            else None,
            namespace_state=namespace_state,
        )
        operation = pending.operation
        if operation in {"dynamic_tools", "dynamic_tool"}:
            return OperationResult([] if operation == "dynamic_tools" else None, catalog_changed)
        if operation == "execute":
            value: BaseModel = ExecuteResponse(
                ok=False, status="error", error=error, runtime=metadata
            )
        elif operation in {"call_function", "call_dynamic"}:
            value = CallFunctionResponse(
                ok=False,
                name=str(pending.payload.get("name", "")),
                error=error,
                runtime=metadata,
            )
        elif operation == "list":
            value = ListResponse(functions=[], error=error, runtime=metadata)
        elif operation == "search":
            value = SearchResponse(results=[], error=error, runtime=metadata)
        elif operation == "reload":
            value = ReloadResponse(results=[], error=error, runtime=metadata)
        elif operation == "inspect":
            value = InspectResponse(
                ok=False,
                name=str(pending.payload.get("name", "")),
                error=error,
                runtime=metadata,
            )
        elif operation == "remove":
            value = RemoveResponse(ok=False, error=error, runtime=metadata)
        elif operation == "reset":
            value = ResetResponse(
                ok=False, execution_count=0, error=error, runtime=metadata
            )
        elif operation == "register_tool":
            value = RegisterToolResponse(ok=False, error=error, runtime=metadata)
        elif operation == "unregister_tool":
            value = UnregisterToolResponse(
                ok=False,
                revision=self._catalog_revision,
                error=error,
                runtime=metadata,
            )
        else:
            raise AssertionError(f"unsupported operation {operation}")
        logger.info(
            "tool=%s request_id=%s epoch=%s outcome=%s runtime_state=%s truncated=false",
            operation,
            pending.request_id,
            self._epoch,
            code,
            self._state,
        )
        return OperationResult(value, catalog_changed)

    def _metadata(
        self,
        pending: _Pending[Any],
        *,
        queue_wait: float,
        interruption_kind: str | None = None,
        namespace_state: str = "unchanged",
    ) -> RuntimeMetadata:
        return RuntimeMetadata(
            request_id=pending.request_id,
            admission_sequence=pending.sequence,
            epoch=self._epoch,
            queue_wait_seconds=round(max(0.0, queue_wait), 6),
            interruption_kind=interruption_kind,
            namespace_state=namespace_state,
        )

    async def _start_worker(self, *, replacement: bool) -> bool:
        self._state = "recovering"
        started = time.monotonic()
        parent, child = self._context.Pipe(duplex=True)
        process = self._context.Process(
            target=worker_main,
            args=(child, self.config.to_worker_payload()),
            name="ipython-mcp-worker",
            daemon=True,
        )
        main_module = sys.modules.get("__main__")
        if main_module is not None and not hasattr(main_module, "__spec__"):
            main_module.__spec__ = None
        try:
            process.start()
        except BaseException as exc:
            parent.close()
            child.close()
            text = str(exc) or type(exc).__name__
            self._startup_error = ErrorInfo(
                code="worker_startup_failed",
                message=text[: self.config.max_text_chars],
                error_type=type(exc).__name__,
                retryable=True,
            )
            self._state = "unavailable"
            return False
        child.close()
        self._connection = parent
        self._process = process
        try:
            message = await asyncio.wait_for(
                self._receive(), timeout=self.config.worker_startup_timeout_seconds
            )
            if message.get("type") != "startup" or message.get("status") != "ready":
                raise RuntimeError(
                    str(message.get("message") or "worker initialization failed")
                )
        except (asyncio.TimeoutError, RuntimeError, EOFError, IpcProtocolError, OSError) as exc:
            await self._terminate_worker()
            text = str(exc) or type(exc).__name__
            self._startup_error = ErrorInfo(
                code="worker_startup_failed",
                message=text[: self.config.max_text_chars],
                error_type=type(exc).__name__,
                retryable=True,
            )
            self._state = "unavailable"
            return False
        duration = time.monotonic() - started
        if replacement:
            self._replacement_startup_seconds = round(duration, 6)
        elif self._epoch == 0:
            self._epoch = 1
        self._startup_error = None
        self._state = "ready"
        return True

    async def _hard_replace(self) -> bool:
        previous_count = self._dynamic_count
        await self._terminate_worker()
        self._epoch += 1
        self._dynamic_count = 0
        self._catalog_revision += 1 if previous_count else 0
        self._catalog_notification_pending = (
            self._catalog_notification_pending or previous_count > 0
        )
        return await self._start_worker(replacement=True)

    async def _terminate_worker(self) -> None:
        connection, process = self._connection, self._process
        self._connection = None
        self._process = None
        if connection is not None:
            with contextlib.suppress(Exception):
                connection.close()
        if process is None:
            return
        if process.is_alive():
            process.terminate()
            await asyncio.to_thread(process.join, 0.5)
        if process.is_alive():
            process.kill()
            await asyncio.to_thread(process.join, 0.5)
        with contextlib.suppress(Exception):
            process.close()

    async def _send(self, message: dict[str, Any]) -> None:
        if self._connection is None:
            raise EOFError("runtime worker is unavailable")
        await asyncio.to_thread(
            send_message,
            self._connection,
            message,
            self.config.max_ipc_message_bytes,
        )

    async def _receive(self) -> dict[str, Any]:
        if self._connection is None:
            raise EOFError("runtime worker is unavailable")
        return await asyncio.to_thread(
            receive_message, self._connection, self.config.max_ipc_message_bytes
        )

    @staticmethod
    def _response_truncated(value: Any) -> bool:
        truncated = getattr(value, "truncated", False)
        if isinstance(truncated, BaseModel):
            return any(bool(item) for item in truncated.model_dump().values())
        return bool(truncated)
