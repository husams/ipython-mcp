"""A thread-affine, serialized IPython runtime."""

from __future__ import annotations

import asyncio
import atexit
import contextlib
import ctypes
import importlib
import inspect
import io
import itertools
import json
import logging
import heapq
import sys
import threading
import traceback
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, fields, is_dataclass
from pathlib import Path
from types import ModuleType
from typing import Any

from IPython.core.interactiveshell import InteractiveShell
from IPython.core.displayhook import DisplayHook

from .config import ServerConfig
from .dynamic_tools import DynamicRegistry, DynamicToolSnapshot, OperationResult
from .models import (
    CallFunctionResponse,
    ErrorInfo,
    ErrorLocation,
    ExecuteResponse,
    FunctionInfo,
    FunctionTruncationInfo,
    InspectResponse,
    InspectTruncationInfo,
    ListResponse,
    RemoveResponse,
    ReloadResponse,
    ReloadResult,
    ResetResponse,
    RegisterToolResponse,
    SearchResponse,
    SearchResult,
    SearchTruncationInfo,
    TruncationInfo,
    UnregisterToolResponse,
)

logger = logging.getLogger(__name__)
_IPYTHON_INTERNAL_NAMES = {
    "In",
    "Out",
    "get_ipython",
    "exit",
    "quit",
    "open",
}
STABLE_TOOL_NAMES = {
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


class JsonTranslationError(ValueError):
    """A live Python result cannot be safely represented as JSON."""


class RuntimeStartupError(RuntimeError):
    """The shell could not be initialized atomically."""


class _SilentDisplayHook(DisplayHook):
    """Preserve IPython's displayhook interface without echoing values."""

    def write_format_data(self, format_dict: dict[str, str], md_dict: dict[str, Any] | None = None) -> None:
        return None


class BoundedTextSink(io.TextIOBase):
    """A streaming prefix sink whose retained memory is independent of output size."""

    def __init__(self, limit: int):
        self.limit = limit
        self._parts: list[str] = []
        self.retained_characters = 0
        self.total_characters = 0

    @property
    def omitted_characters(self) -> int:
        return self.total_characters - self.retained_characters

    @property
    def truncated(self) -> bool:
        return self.omitted_characters > 0

    def writable(self) -> bool:
        return True

    def write(self, value: str) -> int:
        if not isinstance(value, str):
            value = str(value)
        length = len(value)
        self.total_characters += length
        remaining = self.limit - self.retained_characters
        if remaining > 0:
            retained = value[:remaining]
            self._parts.append(retained)
            self.retained_characters += len(retained)
        return length

    def getvalue(self) -> str:
        return "".join(self._parts)


class ShellRuntime:
    """Own the only IPython shell and execute all operations on one thread."""

    def __init__(self, config: ServerConfig):
        self.config = config
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ipython-shell")
        self._shell: InteractiveShell | None = None
        self._module_bindings: dict[str, str] = {}
        self._runtime_bindings: dict[str, Any] = {}
        self._added_library_paths: list[str] = []
        self._execution_count = 0
        self._closed = False
        self._owner_thread_id: int | None = None
        self._dynamic_registry = DynamicRegistry(config, STABLE_TOOL_NAMES)

    @property
    def closed(self) -> bool:
        """Whether the runtime has completed or begun deterministic teardown."""

        return self._closed

    async def start(self) -> None:
        try:
            await self._submit(self._start_in_owner)
        except Exception:
            self._closed = True
            self._executor.shutdown(wait=True, cancel_futures=True)
            raise

    async def close(self) -> None:
        if self._closed:
            return
        try:
            await self._submit(self._close_in_owner)
        finally:
            self._closed = True
            self._executor.shutdown(wait=True, cancel_futures=True)

    def request_interrupt(self) -> bool:
        """Inject ``KeyboardInterrupt`` into the thread-affine Python owner."""

        thread_id = self._owner_thread_id
        if thread_id is None or self._closed:
            return False
        changed = ctypes.pythonapi.PyThreadState_SetAsyncExc(
            ctypes.c_ulong(thread_id), ctypes.py_object(KeyboardInterrupt)
        )
        if changed == 1:
            return True
        if changed > 1:
            ctypes.pythonapi.PyThreadState_SetAsyncExc(
                ctypes.c_ulong(thread_id), ctypes.c_void_p(0)
            )
        return False

    async def health(self) -> dict[str, Any]:
        """Probe the owner and reconcile the catalog after an interruption."""

        outcome = await self.dynamic_tools()
        return {
            "dynamic_tools": [asdict(snapshot) for snapshot in outcome.value],
            "catalog_changed": outcome.catalog_changed,
            "catalog_revision": self._dynamic_registry.revision,
        }

    @property
    def catalog_revision(self) -> int:
        return self._dynamic_registry.revision

    @property
    def dynamic_tool_count(self) -> int:
        return self._dynamic_registry.active_count

    async def execute(self, code: str) -> ExecuteResponse:
        if not isinstance(code, str) or not code.strip():
            return ExecuteResponse(
                ok=False,
                status="error",
                error=ErrorInfo(code="invalid_code", message="code must be a non-empty string"),
            )
        return (await self.execute_operation(code)).value

    async def execute_operation(self, code: str) -> OperationResult[ExecuteResponse]:
        if not isinstance(code, str) or not code.strip():
            return await self._submit(
                lambda: OperationResult(
                    ExecuteResponse(
                        ok=False,
                        status="error",
                        error=ErrorInfo(
                            code="invalid_code",
                            message="code must be a non-empty string",
                        ),
                    ),
                    self._dynamic_registry.reconcile(self.shell.user_ns),
                )
            )
        return await self._submit(lambda: self._execute_operation_in_owner(code))

    async def list_functions(self) -> ListResponse:
        return await self._submit(self._list_functions_in_owner)

    async def call_function(self, name: str, arguments: dict[str, Any] | str) -> CallFunctionResponse:
        return await self._submit(lambda: self._call_function_in_owner(name, arguments))

    async def search(self, query: str, exact: bool, limit: int) -> SearchResponse:
        return await self._submit(lambda: self._search_in_owner(query, exact, limit))

    async def reload(self, modules: list[str]) -> ReloadResponse:
        return await self._submit(lambda: self._reload_in_owner(modules))

    async def inspect(self, name: str) -> InspectResponse:
        return await self._submit(lambda: self._inspect_in_owner(name))

    async def remove(self, names: list[str]) -> RemoveResponse:
        return (await self.remove_operation(names)).value

    async def remove_operation(self, names: list[str]) -> OperationResult[RemoveResponse]:
        return await self._submit(lambda: self._remove_operation_in_owner(names))

    async def reset(self) -> ResetResponse:
        return (await self.reset_operation()).value

    async def reset_operation(self) -> OperationResult[ResetResponse]:
        return await self._submit(self._reset_operation_in_owner)

    async def register_tool(
        self, name: str, tool_name: str | None, description: str | None
    ) -> OperationResult[RegisterToolResponse]:
        return await self._submit(
            lambda: self._register_tool_in_owner(name, tool_name, description)
        )

    async def unregister_tool(
        self, names: list[str]
    ) -> OperationResult[UnregisterToolResponse]:
        return await self._submit(
            lambda: self._dynamic_registry.unregister(self.shell.user_ns, names)
        )

    async def dynamic_tools(self) -> OperationResult[list[DynamicToolSnapshot]]:
        return await self._submit(
            lambda: self._dynamic_registry.snapshots(self.shell.user_ns)
        )

    async def dynamic_tool(
        self, name: str
    ) -> OperationResult[DynamicToolSnapshot | None]:
        return await self._submit(
            lambda: self._dynamic_registry.snapshot(self.shell.user_ns, name)
        )

    async def call_dynamic(
        self, name: str, arguments: dict[str, Any]
    ) -> OperationResult[CallFunctionResponse]:
        return await self._submit(
            lambda: self._call_dynamic_in_owner(name, arguments)
        )

    async def _submit(self, operation: Callable[[], Any]) -> Any:
        if self._closed:
            raise RuntimeError("IPython runtime is closed")
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._executor, operation)

    def _start_in_owner(self) -> None:
        self._owner_thread_id = threading.get_ident()
        # IPython's public display() helper only publishes rich output when an
        # active singleton exists.  The worker process owns exactly one shell,
        # so registering it as that singleton makes the public API and the
        # shell's bounded display publisher share the same lifecycle.
        self._shell = InteractiveShell.instance()
        # This server is a transient MCP process, not an IPython profile. Do
        # not write source, results, or history metadata to the user's global
        # history database.
        self._shell.history_manager.enabled = False
        self._stop_history_writer(self._shell)
        self._runtime_bindings = {
            name: self._shell.user_ns[name]
            for name in _IPYTHON_INTERNAL_NAMES
            if name in self._shell.user_ns
        }
        current_module: str | None = None
        try:
            for path in reversed(self.config.library_paths):
                path = Path(path).expanduser().resolve()
                if not path.is_dir():
                    raise RuntimeStartupError(f"configured library path does not exist: {path}")
                if str(path) not in sys.path:
                    sys.path.insert(0, str(path))
                    self._added_library_paths.append(str(path))
            for module_name in self.config.preload_modules:
                current_module = module_name
                self._import_and_bind(module_name)
        except Exception as exc:
            self._shell = None
            with contextlib.suppress(Exception):
                InteractiveShell.clear_instance()
            self._runtime_bindings.clear()
            self._remove_added_library_paths()
            if isinstance(exc, RuntimeStartupError):
                raise
            module_name = current_module or "unknown"
            raise RuntimeStartupError(f"failed to preload module {module_name}: {type(exc).__name__}: {exc}") from exc

    def _close_in_owner(self) -> None:
        if self._shell is not None:
            shell = self._shell
            with contextlib.suppress(Exception):
                atexit.unregister(shell.atexit_operations)
            with contextlib.suppress(Exception):
                shell.atexit_operations()
            self._stop_history_writer(shell)
            self._shell = None
            with contextlib.suppress(Exception):
                InteractiveShell.clear_instance()
        self._runtime_bindings.clear()
        self._module_bindings.clear()
        self._dynamic_registry.close()
        self._remove_added_library_paths()
        self._owner_thread_id = None

    @staticmethod
    def _stop_history_writer(shell: InteractiveShell) -> None:
        writer = getattr(shell.history_manager, "save_thread", None)
        if writer is None:
            return
        with contextlib.suppress(Exception):
            writer.stop()
        with contextlib.suppress(Exception):
            writer.join(timeout=1.0)

    def _remove_added_library_paths(self) -> None:
        for path in self._added_library_paths:
            with contextlib.suppress(ValueError):
                sys.path.remove(path)
        self._added_library_paths.clear()

    @property
    def shell(self) -> InteractiveShell:
        if self._shell is None:
            raise RuntimeError("IPython runtime is not started")
        return self._shell

    def _import_and_bind(self, module_name: str) -> str:
        module = importlib.import_module(module_name)
        binding = self.config.module_aliases.get(module_name, module_name.rsplit(".", 1)[-1])
        self.shell.user_ns[binding] = module
        self._module_bindings[module_name] = binding
        return binding

    def _execute_in_owner(self, code: str) -> ExecuteResponse:
        self._execution_count += 1
        runtime_execution_count = self._execution_count
        stdout_sink = BoundedTextSink(self.config.max_text_chars)
        stderr_sink = BoundedTextSink(self.config.max_text_chars)
        stdout = ""
        stderr = ""
        display_data: list[dict[str, Any]] = []
        truncation = TruncationInfo()
        run_result = None
        try:
            with contextlib.redirect_stdout(stdout_sink), contextlib.redirect_stderr(stderr_sink):
                # IPython normally prints its rich traceback to stdout. The
                # structured error below is the protocol-facing traceback;
                # suppressing the duplicate display keeps user stdout intact.
                showtraceback = self.shell.showtraceback
                showsyntaxerror = self.shell.showsyntaxerror
                showindentationerror = self.shell.showindentationerror
                displayhook = self.shell.displayhook
                display_trap_hook = self.shell.display_trap.hook
                display_publish = self.shell.display_pub.publish
                self.shell.showtraceback = lambda *args, **kwargs: None
                self.shell.showsyntaxerror = lambda *args, **kwargs: None
                self.shell.showindentationerror = lambda *args, **kwargs: None
                self.shell.displayhook = _SilentDisplayHook(shell=self.shell, cache_size=0)
                self.shell.display_trap.hook = self.shell.displayhook

                def publish(data: dict[str, Any], metadata: dict[str, Any] | None = None, **_: Any) -> None:
                    nonlocal display_data
                    if len(display_data) >= self.config.max_display_items:
                        truncation.display_data = True
                        return
                    try:
                        safe_data, data_truncated = self._json_safe(data)
                        safe_metadata, metadata_truncated = self._json_safe(metadata or {})
                    except JsonTranslationError:
                        safe_data = {"text/plain": self._safe_repr(data)}
                        safe_metadata = {}
                        data_truncated = True
                        metadata_truncated = False
                    truncation.display_data = (
                        truncation.display_data or data_truncated or metadata_truncated
                    )
                    display_data.append({"data": safe_data, "metadata": safe_metadata})

                self.shell.display_pub.publish = publish
                try:
                    run_result = self.shell.run_cell(code, store_history=False, silent=False)
                finally:
                    self.shell.showtraceback = showtraceback
                    self.shell.showsyntaxerror = showsyntaxerror
                    self.shell.showindentationerror = showindentationerror
                    self.shell.displayhook = displayhook
                    self.shell.display_trap.hook = display_trap_hook
                    self.shell.display_pub.publish = display_publish
            stdout = stdout_sink.getvalue()
            stderr = stderr_sink.getvalue()
            truncation.stdout = stdout_sink.truncated
            truncation.stderr = stderr_sink.truncated
            truncation.stdout_omitted_chars = stdout_sink.omitted_characters
            truncation.stderr_omitted_chars = stderr_sink.omitted_characters
        except Exception as exc:
            stdout = stdout_sink.getvalue()
            stderr = stderr_sink.getvalue()
            truncation.stdout = stdout_sink.truncated
            truncation.stderr = stderr_sink.truncated
            truncation.stdout_omitted_chars = stdout_sink.omitted_characters
            truncation.stderr_omitted_chars = stderr_sink.omitted_characters
            error = self._error_info("execution_error", exc)
            truncation.traceback = error.traceback_truncated
            logger.info("tool=execute ok=false")
            return ExecuteResponse(
                ok=False,
                status="error",
                stdout=stdout,
                stderr=stderr,
                display_data=display_data,
                error=error,
                truncated=truncation,
            )

        exception = getattr(run_result, "error_before_exec", None) or getattr(run_result, "error_in_exec", None)
        execution_count = runtime_execution_count
        if exception is not None:
            error = self._error_info("syntax_error" if getattr(run_result, "error_before_exec", None) else "execution_error", exception)
            truncation.traceback = error.traceback_truncated
            logger.info("tool=execute ok=false")
            return ExecuteResponse(
                ok=False,
                status="error",
                execution_count=execution_count,
                stdout=stdout,
                stderr=stderr,
                display_data=display_data,
                error=error,
                truncated=truncation,
            )

        result = getattr(run_result, "result", None)
        if result is not None:
            try:
                result, result_truncated = self._json_safe(result)
                truncation.result = result_truncated
            except JsonTranslationError as exc:
                live_result = getattr(run_result, "result", None)
                result = {
                    "json_compatible": False,
                    "type": f"{type(live_result).__module__}.{type(live_result).__qualname__}",
                    "representation": self._safe_repr(live_result),
                }
                truncation.result = True
                logger.info("tool=execute ok=true")
                return ExecuteResponse(
                    ok=True,
                    status="ok",
                    execution_count=execution_count,
                    result=result,
                    stdout=stdout,
                    stderr=stderr,
                    display_data=display_data,
                    truncated=truncation,
                )
        logger.info("tool=execute ok=true")
        return ExecuteResponse(
            ok=True,
            status="ok",
            execution_count=execution_count,
            result=result,
            stdout=stdout,
            stderr=stderr,
            display_data=display_data,
            truncated=truncation,
        )

    def _execute_operation_in_owner(self, code: str) -> OperationResult[ExecuteResponse]:
        response = self._execute_in_owner(code)
        changed = self._dynamic_registry.reconcile(self.shell.user_ns)
        return OperationResult(response, changed)

    def _list_functions_in_owner(self) -> ListResponse:
        functions: list[FunctionInfo] = []
        for name, value in self._visible_namespace_items():
            if not callable(value):
                continue
            try:
                signature = str(inspect.signature(value))
            except (TypeError, ValueError):
                signature = "(...)"
            signature, signature_truncated = _bounded_text(
                signature, self.config.max_text_chars
            )
            raw_module = getattr(value, "__module__", None)
            module = raw_module if isinstance(raw_module, str) else None
            module, module_truncated = _bounded_optional_text(
                module, self.config.max_text_chars
            )
            raw_doc = inspect.getdoc(value)
            description, description_truncated = _bounded_optional_text(
                raw_doc, self.config.max_tool_description_chars
            )
            functions.append(
                FunctionInfo(
                    name=name,
                    signature=signature,
                    module=module,
                    description=description,
                    truncated=FunctionTruncationInfo(
                        signature=signature_truncated,
                        module=module_truncated,
                        description=description_truncated,
                    ),
                )
            )
        functions.sort(key=lambda item: item.name)
        truncated = len(functions) > self.config.max_results
        return ListResponse(functions=functions[: self.config.max_results], truncated=truncated)

    def _call_function_in_owner(self, name: str, arguments: dict[str, Any] | str) -> CallFunctionResponse:
        if not isinstance(name, str) or not name.strip():
            return CallFunctionResponse(ok=False, name=str(name), error=ErrorInfo(code="invalid_name", message="name must be a non-empty string"))
        if len(name) > self.config.max_text_chars:
            bounded_name, _ = _bounded_text(name, self.config.max_text_chars)
            return CallFunctionResponse(
                ok=False,
                name=bounded_name,
                name_truncated=True,
                error=ErrorInfo(
                    code="invalid_name",
                    message=f"name must be at most {self.config.max_text_chars} characters",
                ),
            )
        parsed, parse_error = _parse_arguments(arguments)
        if parse_error is not None:
            return CallFunctionResponse(ok=False, name=name, error=parse_error)
        try:
            target = self._resolve_name(name)
        except (KeyError, AttributeError) as exc:
            return CallFunctionResponse(ok=False, name=name, error=ErrorInfo(code="name_not_found", message=f"callable {name!r} was not found", error_type=type(exc).__name__))
        if not callable(target):
            return CallFunctionResponse(ok=False, name=name, error=ErrorInfo(code="not_callable", message=f"{name!r} resolves to a non-callable object", error_type=type(target).__name__))
        try:
            signature = inspect.signature(target)
            signature.bind(**parsed)
        except (TypeError, ValueError) as exc:
            return CallFunctionResponse(ok=False, name=name, error=ErrorInfo(code="argument_binding_error", message=str(exc), error_type=type(exc).__name__))
        try:
            value = target(**parsed)
        except Exception as exc:
            logger.info("tool=call_function ok=false")
            return CallFunctionResponse(ok=False, name=name, error=self._error_info("callable_error", exc))
        try:
            safe_value, truncated = self._json_safe(value)
        except JsonTranslationError as exc:
            logger.info("tool=call_function ok=false")
            return CallFunctionResponse(ok=False, name=name, error=ErrorInfo(code="result_not_json", message=str(exc), error_type=type(value).__name__))
        logger.info("tool=call_function ok=true")
        return CallFunctionResponse(ok=True, name=name, result=safe_value, truncated=truncated)

    def _search_in_owner(self, query: str, exact: bool, limit: int) -> SearchResponse:
        if not isinstance(query, str):
            return SearchResponse(
                results=[], error=ErrorInfo(code="invalid_query", message="query must be a string")
            )
        if len(query) > self.config.max_text_chars:
            return SearchResponse(
                results=[],
                error=ErrorInfo(
                    code="invalid_query",
                    message=f"query must be at most {self.config.max_text_chars} characters",
                ),
            )
        limit = max(1, min(limit, self.config.max_results))
        matches: list[SearchResult] = []
        items = sorted(self._visible_namespace_items(), key=lambda item: item[0])
        truncated = False
        for name, value in items:
            matched = name == query if exact else query.casefold() in name.casefold()
            if not matched:
                continue
            if len(matches) >= limit:
                truncated = True
                break
            qualified_type, qualified_type_truncated = _bounded_text(
                f"{type(value).__module__}.{type(value).__qualname__}",
                self.config.max_text_chars,
            )
            try:
                raw_representation = repr(value)
            except Exception as exc:
                raw_representation = (
                    f"<{type(value).__name__} repr failed: {type(exc).__name__}>"
                )
            representation, representation_truncated = _bounded_text(
                raw_representation, self.config.max_repr_chars
            )
            raw_doc = inspect.getdoc(value)
            description, description_truncated = _bounded_optional_text(
                raw_doc, self.config.max_tool_description_chars
            )
            matches.append(
                SearchResult(
                    name=name,
                    kind=_object_kind(value),
                    qualified_type=qualified_type,
                    representation=representation,
                    description=description,
                    truncated=SearchTruncationInfo(
                        qualified_type=qualified_type_truncated,
                        representation=representation_truncated,
                        description=description_truncated,
                    ),
                )
            )
        return SearchResponse(results=matches, truncated=truncated)

    def _reload_in_owner(self, modules: list[str]) -> ReloadResponse:
        if not isinstance(modules, list) or not modules:
            return ReloadResponse(results=[ReloadResult(module="", ok=False, error=ErrorInfo(code="invalid_modules", message="modules must be a non-empty list"))])
        if len(modules) > self.config.max_results:
            return ReloadResponse(
                results=[],
                error=ErrorInfo(
                    code="invalid_modules",
                    message=f"modules is limited to {self.config.max_results} items",
                ),
            )
        if any(
            not isinstance(module, str) or len(module) > self.config.max_text_chars
            for module in modules
        ):
            return ReloadResponse(
                results=[],
                error=ErrorInfo(
                    code="invalid_modules",
                    message=(
                        "module names must be strings of at most "
                        f"{self.config.max_text_chars} characters"
                    ),
                ),
            )
        results: list[ReloadResult] = []
        for module_name in modules:
            if not isinstance(module_name, str) or not module_name.strip():
                results.append(ReloadResult(module=str(module_name), ok=False, error=ErrorInfo(code="invalid_module", message="module names must be non-empty strings")))
                continue
            try:
                importlib.invalidate_caches()
                module = sys.modules.get(module_name)
                if module is None:
                    module = importlib.import_module(module_name)
                else:
                    self._remove_source_bytecode(module)
                    module = importlib.reload(module)
                binding = self._module_bindings.get(module_name) or self.config.module_aliases.get(module_name, module_name.rsplit(".", 1)[-1])
                self.shell.user_ns[binding] = module
                self._module_bindings[module_name] = binding
                results.append(ReloadResult(module=module_name, ok=True, binding=binding))
            except Exception as exc:
                results.append(ReloadResult(module=module_name, ok=False, error=self._error_info("reload_error", exc)))
        logger.info("tool=reload ok=%s", all(item.ok for item in results))
        return ReloadResponse(results=results)

    def _inspect_in_owner(self, name: str) -> InspectResponse:
        if not isinstance(name, str) or not name or name.strip() != name:
            return InspectResponse(
                ok=False,
                name=str(name),
                error=ErrorInfo(code="invalid_name", message="name must be a non-empty string without surrounding whitespace"),
            )
        if len(name) > self.config.max_text_chars:
            bounded_name, _ = _bounded_text(name, self.config.max_text_chars)
            return InspectResponse(
                ok=False,
                name=bounded_name,
                error=ErrorInfo(
                    code="invalid_name",
                    message=f"name must be at most {self.config.max_text_chars} characters",
                ),
            )
        try:
            value = self._resolve_name(name)
        except (KeyError, AttributeError) as exc:
            return InspectResponse(
                ok=False,
                name=name,
                error=ErrorInfo(
                    code="name_not_found",
                    message=f"live name {name!r} was not found",
                    error_type=type(exc).__name__,
                ),
            )
        except Exception as exc:
            return InspectResponse(ok=False, name=name, error=self._error_info("attribute_access_error", exc))

        try:
            kind = _object_kind(value)
            qualified_type = f"{type(value).__module__}.{type(value).__qualname__}"
            is_callable = callable(value)
            signature: str | None = None
            if is_callable:
                try:
                    signature = str(inspect.signature(value))
                except (TypeError, ValueError):
                    signature = None
            if isinstance(value, ModuleType):
                module = value.__name__
            else:
                module = getattr(value, "__module__", None) or type(value).__module__
            documentation = inspect.getdoc(value)
        except Exception as exc:
            return InspectResponse(ok=False, name=name, error=self._error_info("inspection_error", exc))

        try:
            representation = repr(value)
        except Exception as exc:
            return InspectResponse(ok=False, name=name, error=self._error_info("representation_error", exc))

        qualified_type, qualified_type_truncated = _bounded_text(qualified_type, self.config.max_text_chars)
        signature, signature_truncated = _bounded_optional_text(signature, self.config.max_text_chars)
        module, module_truncated = _bounded_optional_text(module, self.config.max_text_chars)
        documentation, documentation_truncated = _bounded_optional_text(documentation, self.config.max_text_chars)
        representation, representation_truncated = _bounded_text(representation, self.config.max_repr_chars)
        return InspectResponse(
            ok=True,
            name=name,
            kind=kind,
            qualified_type=qualified_type,
            callable=is_callable,
            signature=signature,
            module=module,
            documentation=documentation,
            representation=representation,
            truncated=InspectTruncationInfo(
                qualified_type=qualified_type_truncated,
                signature=signature_truncated,
                module=module_truncated,
                documentation=documentation_truncated,
                representation=representation_truncated,
            ),
        )

    def _remove_in_owner(self, names: list[str]) -> RemoveResponse:
        if not isinstance(names, list) or any(not isinstance(name, str) or not name for name in names):
            return RemoveResponse(
                ok=False,
                error=ErrorInfo(code="invalid_names", message="names must be a list of non-empty strings"),
            )
        if len(names) > self.config.max_results or any(
            len(name) > self.config.max_text_chars for name in names
        ):
            return RemoveResponse(
                ok=False,
                error=ErrorInfo(
                    code="invalid_names",
                    message=(
                        f"names is limited to {self.config.max_results} items of at most "
                        f"{self.config.max_text_chars} characters"
                    ),
                ),
            )

        removed: list[str] = []
        refused: list[str] = []
        unknown: list[str] = []
        seen: set[str] = set()
        for name in names:
            if name in seen:
                continue
            seen.add(name)
            if self._is_protected_name(name):
                refused.append(name)
            elif "." in name or name not in self.shell.user_ns:
                unknown.append(name)
            else:
                del self.shell.user_ns[name]
                removed.append(name)
        return RemoveResponse(ok=True, removed=removed, refused=refused, unknown=unknown)

    def _remove_operation_in_owner(self, names: list[str]) -> OperationResult[RemoveResponse]:
        response = self._remove_in_owner(names)
        changed = self._dynamic_registry.reconcile(self.shell.user_ns)
        return OperationResult(response, changed)

    def _reset_in_owner(self) -> ResetResponse:
        configured_modules: dict[str, ModuleType] = {}
        try:
            for module_name in self._configured_module_bindings():
                configured_modules[module_name] = sys.modules.get(module_name) or importlib.import_module(module_name)
        except Exception as exc:
            return ResetResponse(
                ok=False,
                execution_count=self._execution_count,
                error=self._error_info("reset_rebind_error", exc),
            )

        removed = sorted(
            name
            for name in self.shell.user_ns
            if isinstance(name, str) and not self._is_protected_name(name)
        )
        for name in removed:
            del self.shell.user_ns[name]
        self.shell.user_ns.update(self._runtime_bindings)
        for module_name, binding in self._configured_module_bindings().items():
            self.shell.user_ns[binding] = configured_modules[module_name]
        return ResetResponse(ok=True, removed=removed, execution_count=self._execution_count)

    def _reset_operation_in_owner(self) -> OperationResult[ResetResponse]:
        reconciliation_changed = self._dynamic_registry.reconcile(self.shell.user_ns)
        response = self._reset_in_owner()
        changed = self._dynamic_registry.reset() if response.ok else False
        return OperationResult(response, reconciliation_changed or changed)

    def _register_tool_in_owner(
        self, name: str, tool_name: str | None, description: str | None
    ) -> OperationResult[RegisterToolResponse]:
        reconciliation_changed = self._dynamic_registry.reconcile(self.shell.user_ns)
        registered = self._dynamic_registry.register(
            self.shell.user_ns,
            self._is_protected_name,
            name,
            tool_name,
            description,
        )
        return OperationResult(
            registered.value,
            reconciliation_changed or registered.catalog_changed,
        )

    def _call_dynamic_in_owner(
        self, name: str, arguments: dict[str, Any]
    ) -> OperationResult[CallFunctionResponse]:
        entry, coerced, error, changed = self._dynamic_registry.prepare_call(
            self.shell.user_ns, name, arguments
        )
        if error is not None:
            return OperationResult(error, changed)
        assert entry is not None
        target = self.shell.user_ns.get(entry.backing_name)
        if target is not entry.callable_object:
            # A defensive owner-thread recheck makes the preparation/invocation
            # boundary explicit even though no other shell operation can run here.
            changed = self._dynamic_registry.reconcile(self.shell.user_ns) or changed
            entry, coerced, error, _ = self._dynamic_registry.prepare_call(
                self.shell.user_ns, name, arguments
            )
            if error is not None:
                return OperationResult(error, changed)
            assert entry is not None
            target = entry.callable_object
        try:
            value = target(**coerced)
        except Exception as exc:
            response = CallFunctionResponse(
                ok=False,
                name=name,
                error=self._error_info("callable_error", exc),
            )
        else:
            try:
                safe_value, truncated = self._json_safe(value)
            except JsonTranslationError as exc:
                response = CallFunctionResponse(
                    ok=False,
                    name=name,
                    error=ErrorInfo(
                        code="result_not_json",
                        message=str(exc),
                        error_type=type(value).__name__,
                    ),
                )
            else:
                response = CallFunctionResponse(
                    ok=True,
                    name=name,
                    result=safe_value,
                    truncated=truncated,
                )
        changed = self._dynamic_registry.reconcile(self.shell.user_ns) or changed
        logger.info("tool=dynamic_call ok=%s", response.ok)
        return OperationResult(response, changed)

    def _configured_module_bindings(self) -> dict[str, str]:
        configured_modules = set(self.config.preload_modules) | set(self.config.module_aliases)
        return {
            module_name: binding
            for module_name, binding in self._module_bindings.items()
            if module_name in configured_modules
        }

    def _is_protected_name(self, name: str) -> bool:
        return (
            name.startswith("_")
            or name in _IPYTHON_INTERNAL_NAMES
            or name in self._configured_module_bindings().values()
        )

    @staticmethod
    def _remove_source_bytecode(module: ModuleType) -> None:
        source = getattr(module, "__file__", None)
        if not source or not source.endswith((".py", ".pyw")):
            return
        try:
            cache = Path(importlib.util.cache_from_source(source))
            cache.unlink(missing_ok=True)
        except (TypeError, OSError):
            return

    def _resolve_name(self, name: str) -> Any:
        parts = name.split(".")
        if any(not part or part.startswith("__") for part in parts):
            raise KeyError(name)
        value = self.shell.user_ns[parts[0]]
        for part in parts[1:]:
            value = getattr(value, part)
        return value

    def _visible_namespace_items(self) -> list[tuple[str, Any]]:
        return [
            (name, value)
            for name, value in self.shell.user_ns.items()
            if isinstance(name, str) and not name.startswith("_") and name not in _IPYTHON_INTERNAL_NAMES
        ]

    def _json_safe(self, value: Any) -> tuple[Any, bool]:
        return _json_safe(value, depth=0, max_depth=self.config.max_json_depth, max_items=self.config.max_results, max_chars=self.config.max_text_chars)

    def _safe_repr(self, value: Any) -> str:
        try:
            text = repr(value)
        except Exception as exc:
            text = f"<{type(value).__name__} repr failed: {type(exc).__name__}>"
        return _bounded_text(text, self.config.max_repr_chars)[0]

    def _error_info(self, code: str, exc: BaseException) -> ErrorInfo:
        raw_filename = getattr(exc, "filename", None)
        filename, location_truncated = _bounded_optional_text(
            raw_filename if isinstance(raw_filename, str) else None,
            self.config.max_text_chars,
        )
        location = ErrorLocation(
            line=getattr(exc, "lineno", None),
            column=getattr(exc, "offset", None),
            filename=filename,
        )
        if not any(value is not None for value in location.model_dump().values()):
            location = None
        try:
            message = str(exc) or type(exc).__name__
        except Exception:
            message = type(exc).__name__
        message, message_truncated = _bounded_text(message, self.config.max_text_chars)
        traceback_sink = BoundedTextSink(self.config.max_traceback_chars)
        for part in traceback.TracebackException.from_exception(exc).format():
            traceback_sink.write(part)
        tb = traceback_sink.getvalue()
        return ErrorInfo(
            code=code,
            message=message,
            error_type=type(exc).__name__,
            location=location,
            traceback=tb or None,
            message_truncated=message_truncated,
            location_truncated=location_truncated,
            traceback_truncated=traceback_sink.truncated,
        )


def _parse_arguments(arguments: dict[str, Any] | str) -> tuple[dict[str, Any], ErrorInfo | None]:
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except json.JSONDecodeError as exc:
            return {}, ErrorInfo(code="invalid_arguments", message=f"arguments are not valid JSON: {exc.msg}", error_type=type(exc).__name__)
    if not isinstance(arguments, dict):
        return {}, ErrorInfo(code="invalid_arguments", message="arguments must be a JSON object")
    try:
        json.dumps(arguments, allow_nan=False)
    except (TypeError, ValueError) as exc:
        return {}, ErrorInfo(code="invalid_arguments", message=f"arguments must be JSON-compatible: {exc}", error_type=type(exc).__name__)
    return arguments, None


def _json_safe(value: Any, *, depth: int, max_depth: int, max_items: int, max_chars: int) -> tuple[Any, bool]:
    if value is None or isinstance(value, (bool, int, str)):
        if isinstance(value, str) and len(value) > max_chars:
            return _bounded_text(value, max_chars)
        return value, False
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            raise JsonTranslationError(f"{type(value).__name__} is not valid JSON")
        return value, False
    if depth >= max_depth:
        return "<maximum JSON depth reached>", True
    if isinstance(value, Mapping):
        converted: dict[str, Any] = {}
        truncated = len(value) > max_items
        for key, item in itertools.islice(value.items(), max_items):
            if not isinstance(key, str):
                raise JsonTranslationError("JSON object keys must be strings")
            converted[key], item_truncated = _json_safe(item, depth=depth + 1, max_depth=max_depth, max_items=max_items, max_chars=max_chars)
            truncated = truncated or item_truncated
        return converted, truncated
    if isinstance(value, (list, tuple)):
        converted = []
        truncated = len(value) > max_items
        for item in value[:max_items]:
            safe_item, item_truncated = _json_safe(item, depth=depth + 1, max_depth=max_depth, max_items=max_items, max_chars=max_chars)
            converted.append(safe_item)
            truncated = truncated or item_truncated
        return converted, truncated
    if isinstance(value, (set, frozenset)):
        retained = heapq.nsmallest(max_items + 1, value, key=_stable_sort_key)
        safe, nested_truncated = _json_safe(
            retained[:max_items],
            depth=depth,
            max_depth=max_depth,
            max_items=max_items,
            max_chars=max_chars,
        )
        return safe, nested_truncated or len(retained) > max_items
    if is_dataclass(value) and not isinstance(value, type):
        shallow = {field.name: getattr(value, field.name) for field in fields(value)}
        return _json_safe(shallow, depth=depth + 1, max_depth=max_depth, max_items=max_items, max_chars=max_chars)
    if hasattr(value, "model_dump") and callable(value.model_dump):
        model_fields = getattr(type(value), "model_fields", {})
        shallow = {name: getattr(value, name) for name in model_fields}
        return _json_safe(shallow, depth=depth + 1, max_depth=max_depth, max_items=max_items, max_chars=max_chars)
    try:
        json.dumps(value, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise JsonTranslationError(f"{type(value).__module__}.{type(value).__qualname__} is not JSON-compatible") from exc
    return value, False


def _bounded_text(value: str, limit: int) -> tuple[str, bool]:
    if len(value) <= limit:
        return value, False
    return value[: max(0, limit - 1)] + "…", True


def _bounded_optional_text(value: str | None, limit: int) -> tuple[str | None, bool]:
    if value is None:
        return None, False
    return _bounded_text(value, limit)


def _stable_sort_key(value: Any) -> tuple[str, str, str]:
    try:
        representation = repr(value)
    except Exception as exc:
        representation = f"<repr failed: {type(exc).__name__}>"
    return (
        type(value).__module__,
        type(value).__qualname__,
        _bounded_text(representation, 1_024)[0],
    )


def _object_kind(value: Any) -> str:
    if isinstance(value, ModuleType):
        return "module"
    if inspect.isclass(value):
        return "type"
    if inspect.isfunction(value) or inspect.ismethod(value) or inspect.isbuiltin(value):
        return "function"
    if callable(value):
        return "callable"
    return "variable"
