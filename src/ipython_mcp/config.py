"""Typed configuration for the trusted, local in-process IPython runtime."""

from __future__ import annotations

import json
import math
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from packaging.requirements import InvalidRequirement, Requirement


_ENVIRONMENT_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_ENVIRONMENT_VARIABLE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_PROTECTED_ALIASES = {"In", "Out", "get_ipython", "exit", "quit", "open"}
_MAX_REQUIREMENTS = 64
_MAX_REQUIREMENT_CHARS = 512
_MAX_REQUIREMENTS_CHARS = 8_192
_MAX_ENVIRONMENT_VARIABLES = 128
_MAX_ENVIRONMENT_VALUE_CHARS = 8_192


def _split(value: str) -> tuple[str, ...]:
    return tuple(part.strip() for part in value.split(",") if part.strip())


@dataclass(frozen=True)
class ServerConfig:
    """Startup inputs and response limits for one server-owned shell."""

    environment_workspace: Path | None = None
    active_environment: str | None = None
    environment_requirements: tuple[str, ...] = ()
    environment_variables: dict[str, str] = field(default_factory=dict)
    library_paths: tuple[Path, ...] = ()
    preload_modules: tuple[str, ...] = ()
    module_aliases: dict[str, str] = field(default_factory=dict)
    environment_setup_timeout_seconds: float = 300.0
    max_text_chars: int = 8_192
    max_repr_chars: int = 1_024
    max_traceback_chars: int = 4_096
    max_results: int = 100
    max_display_items: int = 20
    max_json_depth: int = 6
    max_tool_name_chars: int = 64
    max_tool_description_chars: int = 1_024
    max_dynamic_tools: int = 100

    def __post_init__(self) -> None:
        for name in (
            "max_text_chars",
            "max_repr_chars",
            "max_traceback_chars",
            "max_results",
            "max_display_items",
            "max_json_depth",
            "max_tool_name_chars",
            "max_tool_description_chars",
            "max_dynamic_tools",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{name} must be a positive integer")
        timeout = self.environment_setup_timeout_seconds
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or float(timeout) <= 0
            or not math.isfinite(float(timeout))
        ):
            raise ValueError(
                "environment_setup_timeout_seconds must be a positive finite number"
            )

        workspace_set = self.environment_workspace is not None
        environment_set = self.active_environment is not None
        if workspace_set != environment_set:
            raise ValueError(
                "environment_workspace and active_environment must be configured together"
            )
        if self.active_environment is not None and not _ENVIRONMENT_NAME.fullmatch(
            self.active_environment
        ):
            raise ValueError(
                "active_environment must be a safe name of at most 64 characters"
            )
        if self.environment_workspace is not None:
            workspace = Path(self.environment_workspace).expanduser()
            if not workspace.is_absolute():
                raise ValueError("environment_workspace must be an absolute path")
            if ".." in workspace.parts:
                raise ValueError("environment_workspace may not contain traversal")
            object.__setattr__(self, "environment_workspace", workspace)
        if self.environment_requirements and not environment_set:
            raise ValueError(
                "environment_requirements require an active task environment"
            )

        object.__setattr__(
            self,
            "environment_requirements",
            _validate_requirements(self.environment_requirements),
        )
        object.__setattr__(
            self,
            "environment_variables",
            _validate_environment_variables(self.environment_variables),
        )
        library_paths = tuple(Path(path).expanduser() for path in self.library_paths)
        if any(not path.is_absolute() for path in library_paths):
            raise ValueError("library_paths must contain absolute paths")
        if any(".." in path.parts for path in library_paths):
            raise ValueError("library_paths may not contain traversal")
        object.__setattr__(self, "library_paths", library_paths)
        modules, aliases = _validate_modules(self.preload_modules, self.module_aliases)
        object.__setattr__(self, "preload_modules", modules)
        object.__setattr__(self, "module_aliases", aliases)

    @classmethod
    def from_env(cls) -> "ServerConfig":
        paths = tuple(
            Path(part).expanduser()
            for part in os.getenv("IPYTHON_MCP_LIBRARY_PATHS", "").split(os.pathsep)
            if part.strip()
        )
        workspace_text = os.getenv("IPYTHON_MCP_ENVIRONMENT_WORKSPACE", "").strip()
        active_environment = os.getenv("IPYTHON_MCP_ACTIVE_ENVIRONMENT", "").strip()
        return cls(
            environment_workspace=Path(workspace_text).expanduser()
            if workspace_text
            else None,
            active_environment=active_environment or None,
            environment_requirements=tuple(
                _json_string_list("IPYTHON_MCP_ENVIRONMENT_REQUIREMENTS", [])
            ),
            environment_variables=_json_string_dict(
                "IPYTHON_MCP_ENVIRONMENT_VARIABLES", {}
            ),
            library_paths=paths,
            preload_modules=_split(os.getenv("IPYTHON_MCP_PRELOAD_MODULES", "")),
            module_aliases=_json_string_dict("IPYTHON_MCP_MODULE_ALIASES", {}),
            environment_setup_timeout_seconds=_positive_float(
                "IPYTHON_MCP_ENVIRONMENT_SETUP_TIMEOUT_SECONDS", 300.0
            ),
            max_text_chars=_positive_int("IPYTHON_MCP_MAX_TEXT_CHARS", 8_192),
            max_repr_chars=_positive_int("IPYTHON_MCP_MAX_REPR_CHARS", 1_024),
            max_traceback_chars=_positive_int(
                "IPYTHON_MCP_MAX_TRACEBACK_CHARS", 4_096
            ),
            max_results=_positive_int("IPYTHON_MCP_MAX_RESULTS", 100),
            max_display_items=_positive_int(
                "IPYTHON_MCP_MAX_DISPLAY_ITEMS", 20
            ),
            max_json_depth=_positive_int("IPYTHON_MCP_MAX_JSON_DEPTH", 6),
            max_tool_name_chars=_positive_int(
                "IPYTHON_MCP_MAX_TOOL_NAME_CHARS", 64
            ),
            max_tool_description_chars=_positive_int(
                "IPYTHON_MCP_MAX_TOOL_DESCRIPTION_CHARS", 1_024
            ),
            max_dynamic_tools=_positive_int(
                "IPYTHON_MCP_MAX_DYNAMIC_TOOLS", 100
            ),
        )


def _validate_requirements(values: tuple[str, ...]) -> tuple[str, ...]:
    if len(values) > _MAX_REQUIREMENTS:
        raise ValueError(f"environment_requirements is limited to {_MAX_REQUIREMENTS} items")
    canonical: list[str] = []
    seen: set[str] = set()
    total = 0
    for value in values:
        if not isinstance(value, str) or not value.strip() or value != value.strip():
            raise ValueError("environment_requirements must contain non-empty strings")
        if len(value) > _MAX_REQUIREMENT_CHARS:
            raise ValueError(
                f"each environment requirement is limited to {_MAX_REQUIREMENT_CHARS} characters"
            )
        try:
            normalized = str(Requirement(value))
        except InvalidRequirement as exc:
            raise ValueError(f"invalid PEP 508 environment requirement: {value!r}") from exc
        total += len(normalized)
        if total > _MAX_REQUIREMENTS_CHARS:
            raise ValueError(
                f"environment requirements are limited to {_MAX_REQUIREMENTS_CHARS} characters"
            )
        if normalized not in seen:
            canonical.append(normalized)
            seen.add(normalized)
    return tuple(canonical)


def _validate_environment_variables(values: dict[str, str]) -> dict[str, str]:
    if not isinstance(values, dict) or not all(
        isinstance(key, str) and isinstance(value, str)
        for key, value in values.items()
    ):
        raise ValueError("environment_variables must map names to string values")
    if len(values) > _MAX_ENVIRONMENT_VARIABLES:
        raise ValueError(
            f"environment_variables is limited to {_MAX_ENVIRONMENT_VARIABLES} items"
        )
    for name, value in values.items():
        if not _ENVIRONMENT_VARIABLE.fullmatch(name):
            raise ValueError(f"invalid environment variable name: {name!r}")
        if "\x00" in value or len(value) > _MAX_ENVIRONMENT_VALUE_CHARS:
            raise ValueError(
                f"environment variable values are limited to {_MAX_ENVIRONMENT_VALUE_CHARS} characters"
            )
    return dict(values)


def _validate_modules(
    modules: tuple[str, ...], aliases: dict[str, str]
) -> tuple[tuple[str, ...], dict[str, str]]:
    if not isinstance(aliases, dict) or not all(
        isinstance(key, str) and isinstance(value, str)
        for key, value in aliases.items()
    ):
        raise ValueError("module_aliases must map module names to aliases")
    unique_modules = tuple(dict.fromkeys(modules))
    for module in unique_modules:
        if not _valid_module_name(module):
            raise ValueError(f"invalid preload module name: {module!r}")
    unknown = sorted(set(aliases) - set(unique_modules))
    if unknown:
        raise ValueError("module aliases require matching preloads: " + ", ".join(unknown))
    bindings: list[str] = []
    for module in unique_modules:
        binding = aliases.get(module, module.rsplit(".", 1)[-1])
        if not binding.isidentifier() or binding.startswith("_"):
            raise ValueError(f"invalid preload alias: {binding!r}")
        if binding in _PROTECTED_ALIASES:
            raise ValueError(f"preload alias is protected: {binding!r}")
        bindings.append(binding)
    duplicates = sorted({binding for binding in bindings if bindings.count(binding) > 1})
    if duplicates:
        raise ValueError("duplicate preload aliases: " + ", ".join(duplicates))
    return unique_modules, dict(aliases)


def _valid_module_name(value: str) -> bool:
    return bool(value) and all(part.isidentifier() for part in value.split("."))


def _json_string_list(name: str, default: list[str]) -> list[str]:
    value = _json_value(name, default)
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"{name} must be a JSON array of strings")
    return value


def _json_string_dict(name: str, default: dict[str, str]) -> dict[str, str]:
    value = _json_value(name, default)
    if not isinstance(value, dict) or not all(
        isinstance(key, str) and isinstance(item, str) for key, item in value.items()
    ):
        raise ValueError(f"{name} must be a JSON object of string values")
    return value


def _json_value(name: str, default: object) -> object:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{name} must contain valid JSON") from exc


def _positive_int(name: str, default: int) -> int:
    raw = os.getenv(name, str(default))
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be a positive integer") from exc
    if value < 1:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _positive_float(name: str, default: float) -> float:
    raw = os.getenv(name, str(default))
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be a positive finite number") from exc
    if value <= 0 or not math.isfinite(value):
        raise ValueError(f"{name} must be a positive finite number")
    return value
