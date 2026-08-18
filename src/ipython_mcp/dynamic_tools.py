"""Bounded schema derivation and the lifespan-owned dynamic tool registry."""

from __future__ import annotations

import copy
import functools
import hashlib
import inspect
import json
import re
import types
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Generic, Literal, TypeVar, Union, get_args, get_origin, get_type_hints

from pydantic import TypeAdapter, ValidationError

from .config import ServerConfig
from .models import (
    CallFunctionResponse,
    DynamicToolRegistration,
    ErrorInfo,
    RegisterToolResponse,
    UnregisterToolResponse,
)

T = TypeVar("T")
_MISSING = object()


@dataclass(frozen=True)
class OperationResult(Generic[T]):
    """A serialized operation result plus whether the advertised catalog changed."""

    value: T
    catalog_changed: bool = False


@dataclass(frozen=True)
class CallableSpec:
    signature: inspect.Signature
    input_schema: dict[str, Any]
    schema_json: str
    fingerprint: str
    token: tuple[Any, ...]
    adapters: dict[str, TypeAdapter[Any]]


@dataclass
class RegistryEntry:
    backing_name: str
    tool_name: str
    description: str | None
    description_truncated: bool
    input_schema: dict[str, Any]
    schema_json: str
    fingerprint: str
    signature: inspect.Signature
    adapters: dict[str, TypeAdapter[Any]]
    callable_object: Any
    signature_token: tuple[Any, ...]
    state: Literal["active", "stale"] = "active"
    stale_reason: str | None = None


@dataclass(frozen=True)
class DynamicToolSnapshot:
    name: str
    description: str | None
    input_schema: dict[str, Any]
    active: bool


class SignatureContractError(ValueError):
    """A callable cannot be represented by the bounded dynamic contract."""


class DynamicRegistry:
    """Mutable registry used only inside ShellRuntime's serialization boundary."""

    def __init__(self, config: ServerConfig, stable_tool_names: set[str]):
        self._config = config
        self._stable_tool_names = frozenset(stable_tool_names)
        self._entries: dict[str, RegistryEntry] = {}
        self._symbols: dict[str, str] = {}
        self.revision = 0

    @property
    def active_count(self) -> int:
        return sum(entry.state == "active" for entry in self._entries.values())

    def register(
        self,
        namespace: Mapping[str, Any],
        protected_name: Any,
        name: str,
        tool_name: str | None,
        description: str | None,
    ) -> OperationResult[RegisterToolResponse]:
        if (
            not isinstance(name, str)
            or not name.isidentifier()
            or "." in name
            or protected_name(name)
            or len(name) > self._config.max_text_chars
        ):
            return self._register_error(
                "invalid_symbol_name",
                "name must be an unprotected top-level Python identifier",
            )

        effective_name = name if tool_name is None else tool_name
        if not isinstance(effective_name, str) or not self._valid_tool_name(effective_name):
            return self._register_error(
                "invalid_tool_name",
                "tool_name must start with an ASCII letter, contain only ASCII letters, digits, '_' or '-', "
                f"and be at most {self._config.max_tool_name_chars} characters",
            )
        if effective_name in self._stable_tool_names:
            return self._register_error(
                "stable_tool_collision",
                f"{effective_name!r} is a stable tool and cannot be replaced",
            )
        if description is not None and not isinstance(description, str):
            return self._register_error("invalid_description", "description must be a string or null")

        try:
            target = namespace[name]
        except KeyError:
            return self._register_error("name_not_found", f"live symbol {name!r} was not found")
        if not callable(target):
            return self._register_error("not_callable", f"live symbol {name!r} is not callable")

        existing_by_name = self._entries.get(effective_name)
        existing_tool = self._symbols.get(name)
        if existing_by_name is not None and existing_by_name.backing_name != name:
            return self._register_error(
                "duplicate_tool_name",
                f"dynamic tool name {effective_name!r} is already registered",
            )
        if existing_tool is not None and existing_tool != effective_name:
            return self._register_error(
                "duplicate_symbol",
                f"live symbol {name!r} is already registered as {existing_tool!r}",
            )
        if existing_by_name is None and len(self._entries) >= self._config.max_dynamic_tools:
            return self._register_error(
                "registry_full",
                f"dynamic registry is limited to {self._config.max_dynamic_tools} tools",
            )

        try:
            spec = derive_callable_spec(target)
            raw_description = inspect.getdoc(target) if description is None else description
        except UnsupportedCallableKind as exc:
            return self._register_error("unsupported_callable_kind", str(exc))
        except (SignatureContractError, TypeError, ValueError) as exc:
            return self._register_error("unsupported_signature", str(exc) or type(exc).__name__)
        bounded_description, description_truncated = _bounded_optional_text(
            raw_description, self._config.max_tool_description_chars
        )

        replacement = RegistryEntry(
            backing_name=name,
            tool_name=effective_name,
            description=bounded_description,
            description_truncated=description_truncated,
            input_schema=copy.deepcopy(spec.input_schema),
            schema_json=spec.schema_json,
            fingerprint=spec.fingerprint,
            signature=spec.signature,
            adapters=spec.adapters,
            callable_object=target,
            signature_token=spec.token,
        )
        changed = existing_by_name is None or not _same_advertisement(existing_by_name, replacement)
        self._entries[effective_name] = replacement
        self._symbols[name] = effective_name
        if changed:
            self.revision += 1
        return OperationResult(
            RegisterToolResponse(ok=True, registration=self._registration(replacement)),
            catalog_changed=changed,
        )

    def unregister(
        self, namespace: Mapping[str, Any], names: list[str]
    ) -> OperationResult[UnregisterToolResponse]:
        reconciliation_changed = self.reconcile(namespace)
        if (
            not isinstance(names, list)
            or len(names) > self._config.max_results
            or any(
                not isinstance(name, str)
                or not name
                or len(name) > self._config.max_tool_name_chars
                for name in names
            )
        ):
            return OperationResult(
                UnregisterToolResponse(
                    ok=False,
                    revision=self.revision,
                    error=ErrorInfo(
                        code="invalid_names",
                        message="names must be a list of non-empty tool names",
                    ),
                ),
                reconciliation_changed,
            )
        unregistered: list[str] = []
        unknown: list[str] = []
        advertised_removed = False
        for tool_name in sorted(set(names)):
            entry = self._entries.pop(tool_name, None)
            if entry is None:
                unknown.append(tool_name)
                continue
            self._symbols.pop(entry.backing_name, None)
            unregistered.append(tool_name)
            advertised_removed = advertised_removed or entry.state == "active"
        if advertised_removed:
            self.revision += 1
        return OperationResult(
            UnregisterToolResponse(
                ok=True,
                unregistered=unregistered,
                unknown=unknown,
                revision=self.revision,
            ),
            reconciliation_changed or advertised_removed,
        )

    def snapshots(
        self, namespace: Mapping[str, Any], *, include_stale: bool = False
    ) -> OperationResult[list[DynamicToolSnapshot]]:
        changed = self.reconcile(namespace)
        snapshots = [
            DynamicToolSnapshot(
                name=entry.tool_name,
                description=entry.description,
                input_schema=json.loads(entry.schema_json),
                active=entry.state == "active",
            )
            for entry in sorted(self._entries.values(), key=lambda item: item.tool_name)
            if include_stale or entry.state == "active"
        ]
        return OperationResult(snapshots, changed)

    def snapshot(
        self, namespace: Mapping[str, Any], tool_name: str
    ) -> OperationResult[DynamicToolSnapshot | None]:
        changed = self.reconcile(namespace)
        entry = self._entries.get(tool_name)
        if entry is None:
            return OperationResult(None, changed)
        return OperationResult(
            DynamicToolSnapshot(
                name=entry.tool_name,
                description=entry.description,
                input_schema=json.loads(entry.schema_json),
                active=entry.state == "active",
            ),
            changed,
        )

    def prepare_call(
        self, namespace: Mapping[str, Any], tool_name: str, arguments: dict[str, Any]
    ) -> tuple[RegistryEntry | None, dict[str, Any], CallFunctionResponse | None, bool]:
        changed = self.reconcile(namespace)
        entry = self._entries.get(tool_name)
        if entry is None:
            return (
                None,
                {},
                CallFunctionResponse(
                    ok=False,
                    name=tool_name,
                    error=ErrorInfo(code="tool_not_found", message=f"dynamic tool {tool_name!r} is not registered"),
                ),
                changed,
            )
        if entry.state != "active":
            return (
                entry,
                {},
                CallFunctionResponse(
                    ok=False,
                    name=tool_name,
                    error=ErrorInfo(
                        code="stale_registration",
                        message=entry.stale_reason or "the live callable no longer matches its registered schema",
                    ),
                ),
                changed,
            )
        if not isinstance(arguments, dict):
            return (
                entry,
                {},
                CallFunctionResponse(
                    ok=False,
                    name=tool_name,
                    error=ErrorInfo(code="invalid_arguments", message="arguments must be a JSON object"),
                ),
                changed,
            )
        validation_error = validate_json_schema(entry.input_schema, arguments)
        if validation_error is not None:
            return (
                entry,
                {},
                CallFunctionResponse(
                    ok=False,
                    name=tool_name,
                    error=ErrorInfo(code="invalid_arguments", message=validation_error),
                ),
                changed,
            )
        try:
            coerced = {
                name: entry.adapters[name].validate_python(value)
                for name, value in arguments.items()
            }
            entry.signature.bind(**coerced)
        except (ValidationError, TypeError, ValueError) as exc:
            return (
                entry,
                {},
                CallFunctionResponse(
                    ok=False,
                    name=tool_name,
                    error=ErrorInfo(
                        code="argument_binding_error",
                        message=str(exc),
                        error_type=type(exc).__name__,
                    ),
                ),
                changed,
            )
        return entry, coerced, None, changed

    def reconcile(self, namespace: Mapping[str, Any]) -> bool:
        catalog_changed = False
        for entry in list(self._entries.values()):
            if entry.state != "active":
                continue
            try:
                current = namespace[entry.backing_name]
                if not callable(current):
                    raise SignatureContractError("the backing symbol is no longer callable")
                current_token = signature_affecting_token(current)
                if current is entry.callable_object and current_token == entry.signature_token:
                    continue
                spec = derive_callable_spec(current, token=current_token)
            except (KeyError, UnsupportedCallableKind, SignatureContractError, TypeError, ValueError) as exc:
                entry.state = "stale"
                entry.stale_reason = (
                    f"registration {entry.tool_name!r} is stale: {str(exc) or type(exc).__name__}"
                )
                catalog_changed = True
                continue
            if spec.schema_json != entry.schema_json or spec.fingerprint != entry.fingerprint:
                entry.state = "stale"
                entry.stale_reason = (
                    f"registration {entry.tool_name!r} is stale because the input schema changed"
                )
                catalog_changed = True
                continue
            entry.callable_object = current
            entry.signature_token = spec.token
            entry.signature = spec.signature
            entry.adapters = spec.adapters
        if catalog_changed:
            self.revision += 1
        return catalog_changed

    def reset(self) -> bool:
        advertised = any(entry.state == "active" for entry in self._entries.values())
        self._entries.clear()
        self._symbols.clear()
        if advertised:
            self.revision += 1
        return advertised

    def close(self) -> None:
        self._entries.clear()
        self._symbols.clear()

    def _valid_tool_name(self, value: str) -> bool:
        pattern = rf"[A-Za-z][A-Za-z0-9_-]{{0,{self._config.max_tool_name_chars - 1}}}"
        return re.fullmatch(pattern, value) is not None

    def _registration(self, entry: RegistryEntry) -> DynamicToolRegistration:
        return DynamicToolRegistration(
            name=entry.backing_name,
            tool_name=entry.tool_name,
            description=entry.description,
            description_truncated=entry.description_truncated,
            input_schema=json.loads(entry.schema_json),
            schema_fingerprint=entry.fingerprint,
            revision=self.revision,
        )

    def _register_error(self, code: str, message: str) -> OperationResult[RegisterToolResponse]:
        return OperationResult(
            RegisterToolResponse(ok=False, error=ErrorInfo(code=code, message=message))
        )


class UnsupportedCallableKind(SignatureContractError):
    pass


def derive_callable_spec(target: Any, *, token: tuple[Any, ...] | None = None) -> CallableSpec:
    _reject_unsupported_callable_kind(target)
    if token is None:
        token = signature_affecting_token(target)
    try:
        signature = inspect.signature(target, follow_wrapped=True)
    except (TypeError, ValueError) as exc:
        raise SignatureContractError(f"inspect.signature failed: {exc}") from exc
    try:
        hints = _resolved_parameter_hints(target, signature)
    except Exception as exc:
        raise SignatureContractError(f"type hints could not be resolved: {type(exc).__name__}: {exc}") from exc

    properties: dict[str, Any] = {}
    required: list[str] = []
    adapters: dict[str, TypeAdapter[Any]] = {}
    for parameter in signature.parameters.values():
        if parameter.kind in {
            inspect.Parameter.POSITIONAL_ONLY,
            inspect.Parameter.VAR_POSITIONAL,
            inspect.Parameter.VAR_KEYWORD,
        }:
            raise SignatureContractError(
                f"parameter {parameter.name!r} has unsupported kind {parameter.kind.description}"
            )
        annotation = hints.get(parameter.name, parameter.annotation)
        if annotation is inspect.Parameter.empty or isinstance(annotation, str):
            raise SignatureContractError(f"parameter {parameter.name!r} has no resolved annotation")
        schema = annotation_schema(annotation)
        try:
            adapters[parameter.name] = TypeAdapter(annotation)
        except Exception as exc:
            raise SignatureContractError(
                f"parameter {parameter.name!r} annotation cannot be validated: {exc}"
            ) from exc
        if parameter.default is inspect.Parameter.empty:
            required.append(parameter.name)
        else:
            try:
                default = _json_value(parameter.default)
            except (TypeError, ValueError) as exc:
                raise SignatureContractError(
                    f"parameter {parameter.name!r} has a non-JSON-compatible default"
                ) from exc
            if validate_json_schema(schema, default) is not None:
                raise SignatureContractError(
                    f"parameter {parameter.name!r} default does not match its annotation"
                )
            schema = {**schema, "default": default}
        properties[parameter.name] = schema

    input_schema: dict[str, Any] = {
        "type": "object",
        "properties": properties,
        "additionalProperties": False,
    }
    if required:
        input_schema["required"] = required
    schema_json = _canonical_json(input_schema)
    return CallableSpec(
        signature=signature,
        input_schema=input_schema,
        schema_json=schema_json,
        fingerprint=hashlib.sha256(schema_json.encode("utf-8")).hexdigest(),
        token=token,
        adapters=adapters,
    )


def annotation_schema(annotation: Any) -> dict[str, Any]:
    origin = get_origin(annotation)
    args = get_args(annotation)
    if annotation is str:
        return {"type": "string"}
    if annotation is int:
        return {"type": "integer"}
    if annotation is float:
        return {"type": "number"}
    if annotation is bool:
        return {"type": "boolean"}
    if annotation in (None, type(None)):
        return {"type": "null"}
    if origin in (types.UnionType, Union):
        choices = [annotation_schema(arg) for arg in args]
        choices.sort(key=_canonical_json)
        return {"anyOf": choices}
    if origin is Literal:
        if not args:
            raise SignatureContractError("Literal must contain at least one value")
        values = []
        for value in args:
            if not isinstance(value, (str, int, float, bool, type(None))):
                raise SignatureContractError("Literal values must be JSON scalar values")
            values.append(_json_value(value))
        return {"enum": values}
    if origin is list:
        if len(args) != 1:
            raise SignatureContractError("list annotations require one item type")
        return {"type": "array", "items": annotation_schema(args[0])}
    if origin in (set, frozenset):
        if len(args) != 1:
            raise SignatureContractError("set annotations require one item type")
        return {"type": "array", "items": annotation_schema(args[0]), "uniqueItems": True}
    if origin is tuple:
        if len(args) == 2 and args[1] is Ellipsis:
            return {"type": "array", "items": annotation_schema(args[0])}
        if not args:
            raise SignatureContractError("tuple annotations require item types")
        return {
            "type": "array",
            "prefixItems": [annotation_schema(arg) for arg in args],
            "items": False,
            "minItems": len(args),
            "maxItems": len(args),
        }
    if origin is dict:
        if len(args) != 2 or args[0] is not str:
            raise SignatureContractError("dict annotations require string keys and one value type")
        return {"type": "object", "additionalProperties": annotation_schema(args[1])}
    raise SignatureContractError(f"unsupported annotation: {annotation!r}")


def validate_json_schema(schema: dict[str, Any], value: Any, path: str = "arguments") -> str | None:
    if "anyOf" in schema:
        if any(validate_json_schema(choice, value, path) is None for choice in schema["anyOf"]):
            return None
        return f"{path} does not match any supported union branch"
    if "enum" in schema:
        encoded = _canonical_json(value)
        if any(encoded == _canonical_json(option) for option in schema["enum"]):
            return None
        return f"{path} is not one of the allowed literal values"
    expected = schema.get("type")
    if expected == "null":
        return None if value is None else f"{path} must be null"
    if expected == "boolean":
        return None if isinstance(value, bool) else f"{path} must be a boolean"
    if expected == "integer":
        return None if isinstance(value, int) and not isinstance(value, bool) else f"{path} must be an integer"
    if expected == "number":
        return None if isinstance(value, (int, float)) and not isinstance(value, bool) else f"{path} must be a number"
    if expected == "string":
        return None if isinstance(value, str) else f"{path} must be a string"
    if expected == "array":
        if not isinstance(value, list):
            return f"{path} must be an array"
        if "minItems" in schema and len(value) < schema["minItems"]:
            return f"{path} has too few items"
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            return f"{path} has too many items"
        if schema.get("uniqueItems"):
            encoded = [_canonical_json(item) for item in value]
            if len(encoded) != len(set(encoded)):
                return f"{path} must contain unique items"
        if "prefixItems" in schema:
            for index, item_schema in enumerate(schema["prefixItems"]):
                error = validate_json_schema(item_schema, value[index], f"{path}[{index}]")
                if error:
                    return error
        elif isinstance(schema.get("items"), dict):
            for index, item in enumerate(value):
                error = validate_json_schema(schema["items"], item, f"{path}[{index}]")
                if error:
                    return error
        return None
    if expected == "object":
        if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
            return f"{path} must be an object with string keys"
        properties = schema.get("properties")
        if isinstance(properties, dict):
            missing = [name for name in schema.get("required", []) if name not in value]
            if missing:
                return f"{path} is missing required properties: {', '.join(missing)}"
            if schema.get("additionalProperties") is False:
                extra = sorted(set(value) - set(properties))
                if extra:
                    return f"{path} has unexpected properties: {', '.join(extra)}"
            for name, item in value.items():
                if name in properties:
                    error = validate_json_schema(properties[name], item, f"{path}.{name}")
                    if error:
                        return error
            return None
        additional = schema.get("additionalProperties")
        if isinstance(additional, dict):
            for name, item in value.items():
                error = validate_json_schema(additional, item, f"{path}.{name}")
                if error:
                    return error
        return None
    return f"{path} uses an unsupported schema"


def signature_affecting_token(target: Any) -> tuple[Any, ...]:
    try:
        return _signature_token(target, set())
    except SignatureContractError:
        raise
    except Exception as exc:
        raise SignatureContractError(
            f"signature-affecting state could not be inspected: {type(exc).__name__}: {exc}"
        ) from exc


def _signature_token(target: Any, stack: set[int]) -> tuple[Any, ...]:
    identity = id(target)
    if identity in stack:
        raise SignatureContractError("cyclic wrapped or partial callable state")
    if not callable(target):
        raise SignatureContractError("wrapped or partial target is not callable")
    stack.add(identity)
    try:
        base = (
            type(target).__module__,
            type(target).__qualname__,
            id(getattr(target, "__code__", None)),
            inspect.iscoroutinefunction(target),
            inspect.isgeneratorfunction(target),
            inspect.isasyncgenfunction(target),
            _value_token(getattr(target, "__annotations__", None), set()),
            _value_token(getattr(target, "__defaults__", None), set()),
            _value_token(getattr(target, "__kwdefaults__", None), set()),
            _value_token(getattr(target, "__signature__", None), set()),
            _value_token(getattr(target, "__text_signature__", None), set()),
        )
        partial_token: Any = None
        if isinstance(target, functools.partial):
            partial_token = (
                _signature_token(target.func, stack),
                _value_token(target.args, set()),
                _value_token(target.keywords or {}, set()),
            )
        wrapped = getattr(target, "__wrapped__", _MISSING)
        wrapped_token: Any = None
        if wrapped is not _MISSING:
            if not callable(wrapped):
                raise SignatureContractError("__wrapped__ must reference a callable")
            wrapped_token = _signature_token(wrapped, stack)
        call_token: Any = None
        if not isinstance(target, functools.partial) and not inspect.isroutine(target) and not inspect.isclass(target):
            call = getattr(type(target), "__call__", None)
            if call is None or not callable(call):
                raise SignatureContractError("callable object has no inspectable __call__")
            call_token = _signature_token(call, stack)
        class_token: Any = None
        if inspect.isclass(target):
            class_token = tuple(
                _signature_token(callable_source, stack)
                for callable_source in _class_signature_callables(target)
            )
        return (base, partial_token, wrapped_token, call_token, class_token)
    finally:
        stack.remove(identity)


def _reject_unsupported_callable_kind(target: Any, seen: set[int] | None = None) -> None:
    seen = set() if seen is None else seen
    identity = id(target)
    if identity in seen:
        raise SignatureContractError("cyclic wrapped or partial callable state")
    seen.add(identity)
    try:
        if inspect.iscoroutinefunction(target) or inspect.isgeneratorfunction(target) or inspect.isasyncgenfunction(target):
            raise UnsupportedCallableKind("dynamic tools must be synchronous non-generator callables")
        if isinstance(target, functools.partial):
            _reject_unsupported_callable_kind(target.func, seen)
        wrapped = getattr(target, "__wrapped__", _MISSING)
        if wrapped is not _MISSING:
            if not callable(wrapped):
                raise SignatureContractError("__wrapped__ must reference a callable")
            _reject_unsupported_callable_kind(wrapped, seen)
        if inspect.isclass(target):
            for callable_source in _class_signature_callables(target):
                _reject_unsupported_callable_kind(callable_source, seen)
        elif not isinstance(target, functools.partial) and not inspect.isroutine(target):
            call = getattr(type(target), "__call__", None)
            if call is None:
                raise SignatureContractError("callable object has no inspectable __call__")
            _reject_unsupported_callable_kind(call, seen)
    finally:
        seen.remove(identity)


def _hint_source(target: Any) -> Any:
    current = target
    seen: set[int] = set()
    while True:
        if id(current) in seen:
            raise SignatureContractError("cyclic wrapped or partial callable state")
        seen.add(id(current))
        if isinstance(current, functools.partial):
            current = current.func
            continue
        wrapped = getattr(current, "__wrapped__", _MISSING)
        if wrapped is not _MISSING:
            if not callable(wrapped):
                raise SignatureContractError("__wrapped__ must reference a callable")
            current = wrapped
            continue
        if inspect.isclass(current):
            return _class_effective_signature_source(current)
        if not inspect.isroutine(current):
            return type(current).__call__
        return current


def _resolved_parameter_hints(
    target: Any, signature: inspect.Signature
) -> dict[str, Any]:
    """Resolve only input annotations; return annotations are non-contractual."""

    filtered = {
        name: parameter.annotation
        for name, parameter in signature.parameters.items()
        if isinstance(parameter.annotation, str)
    }
    if not filtered:
        return {}
    source = _hint_source(target)
    function = source.__func__ if inspect.ismethod(source) else source
    globalns = getattr(function, "__globals__", None)
    if globalns is None:
        module = inspect.getmodule(function)
        globalns = vars(module) if module is not None else {}

    def hint_carrier() -> None:
        return None

    hint_carrier.__annotations__ = filtered
    hint_carrier.__module__ = getattr(function, "__module__", hint_carrier.__module__)
    return get_type_hints(
        hint_carrier,
        globalns=globalns,
        localns=globalns,
        include_extras=True,
    )


def _class_signature_callables(target: type[Any]) -> tuple[Any, ...]:
    """Callable state that can influence inspect.signature(class)."""

    return (type(target).__call__, target.__new__, target.__init__)


def _class_effective_signature_source(target: type[Any]) -> Any:
    """Mirror inspect.signature's class precedence for annotation globals."""

    metaclass_call = type(target).__call__
    if metaclass_call is not type.__call__:
        return metaclass_call
    new = target.__new__
    init = target.__init__
    for base in target.__mro__:
        if new is not object.__new__ and "__new__" in base.__dict__:
            return new
        if init is not object.__init__ and "__init__" in base.__dict__:
            return init
    return target


def _value_token(value: Any, stack: set[int]) -> Any:
    if value is None or isinstance(value, (bool, int, float, str, bytes)):
        return (type(value).__name__, value)
    if isinstance(value, type):
        return ("type", id(value), value.__module__, value.__qualname__)
    identity = id(value)
    if identity in stack:
        raise SignatureContractError("cyclic signature-affecting value")
    if isinstance(value, Mapping):
        stack.add(identity)
        try:
            items = [(_value_token(key, stack), _value_token(item, stack)) for key, item in value.items()]
            return ("mapping", tuple(sorted(items, key=lambda item: str(item[0]))))
        finally:
            stack.remove(identity)
    if isinstance(value, (tuple, list)):
        stack.add(identity)
        try:
            return (type(value).__name__, tuple(_value_token(item, stack) for item in value))
        finally:
            stack.remove(identity)
    if isinstance(value, (set, frozenset)):
        stack.add(identity)
        try:
            values = [_value_token(item, stack) for item in value]
            return (type(value).__name__, tuple(sorted(values, key=str)))
        finally:
            stack.remove(identity)
    return (type(value).__module__, type(value).__qualname__, identity)


def _json_value(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=False, allow_nan=False))


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def _bounded_optional_text(value: str | None, limit: int) -> tuple[str | None, bool]:
    if value is None:
        return None, False
    if len(value) <= limit:
        return value, False
    return value[: max(0, limit - 1)] + "…", True


def _same_advertisement(left: RegistryEntry, right: RegistryEntry) -> bool:
    return (
        left.state == "active"
        and left.schema_json == right.schema_json
        and left.description == right.description
        and left.description_truncated == right.description_truncated
    )
