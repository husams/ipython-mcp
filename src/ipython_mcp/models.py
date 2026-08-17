"""Typed public request and response models for the stable MCP tools."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class ErrorLocation(BaseModel):
    line: int | None = None
    column: int | None = None
    filename: str | None = None


class ErrorInfo(BaseModel):
    code: str
    message: str
    error_type: str | None = None
    location: ErrorLocation | None = None
    traceback: str | None = None
    retryable: bool = False
    retry_after_seconds: float | None = None
    message_truncated: bool = False
    location_truncated: bool = False
    traceback_truncated: bool = False


class RuntimeMetadata(BaseModel):
    request_id: str
    admission_sequence: int
    epoch: int
    queue_wait_seconds: float = 0.0
    interruption_kind: Literal["operation_timeout", "operation_cancelled"] | None = None
    namespace_state: Literal["unchanged", "preserved", "reset", "unknown"] = "unchanged"


class RuntimeStatusResponse(BaseModel):
    state: Literal["ready", "busy", "recovering", "unavailable", "closed"]
    epoch: int
    queue_depth: int
    operation_active: bool
    latest_interruption_kind: Literal["operation_timeout", "operation_cancelled"] | None = None
    latest_namespace_state: Literal["unchanged", "preserved", "reset", "unknown"] = "unchanged"
    replacement_startup_seconds: float | None = None
    error: ErrorInfo | None = None


class TruncationInfo(BaseModel):
    stdout: bool = False
    stderr: bool = False
    result: bool = False
    display_data: bool = False
    traceback: bool = False
    items: bool = False
    stdout_omitted_chars: int = 0
    stderr_omitted_chars: int = 0


class ExecuteResponse(BaseModel):
    ok: bool
    status: Literal["ok", "error"]
    execution_count: int | None = None
    result: Any = None
    stdout: str = ""
    stderr: str = ""
    display_data: list[dict[str, Any]] = Field(default_factory=list)
    error: ErrorInfo | None = None
    truncated: TruncationInfo = Field(default_factory=TruncationInfo)
    runtime: RuntimeMetadata | None = None


class FunctionTruncationInfo(BaseModel):
    signature: bool = False
    module: bool = False
    description: bool = False


class FunctionInfo(BaseModel):
    name: str
    signature: str
    module: str | None = None
    description: str | None = None
    truncated: FunctionTruncationInfo = Field(default_factory=FunctionTruncationInfo)


class ListResponse(BaseModel):
    functions: list[FunctionInfo]
    truncated: bool = False
    error: ErrorInfo | None = None
    runtime: RuntimeMetadata | None = None


class CallFunctionResponse(BaseModel):
    ok: bool
    name: str
    result: Any = None
    error: ErrorInfo | None = None
    truncated: bool = False
    runtime: RuntimeMetadata | None = None
    name_truncated: bool = False


class SearchTruncationInfo(BaseModel):
    qualified_type: bool = False
    representation: bool = False
    description: bool = False


class SearchResult(BaseModel):
    name: str
    kind: str
    qualified_type: str
    representation: str
    description: str | None = None
    truncated: SearchTruncationInfo = Field(default_factory=SearchTruncationInfo)


class SearchResponse(BaseModel):
    results: list[SearchResult]
    truncated: bool = False
    error: ErrorInfo | None = None
    runtime: RuntimeMetadata | None = None


class ReloadResult(BaseModel):
    module: str
    ok: bool
    binding: str | None = None
    error: ErrorInfo | None = None
    truncated: bool = False


class ReloadResponse(BaseModel):
    results: list[ReloadResult]
    error: ErrorInfo | None = None
    runtime: RuntimeMetadata | None = None


class InspectTruncationInfo(BaseModel):
    qualified_type: bool = False
    signature: bool = False
    module: bool = False
    documentation: bool = False
    representation: bool = False


class InspectResponse(BaseModel):
    ok: bool
    name: str
    kind: str | None = None
    qualified_type: str | None = None
    callable: bool | None = None
    signature: str | None = None
    module: str | None = None
    documentation: str | None = None
    representation: str | None = None
    truncated: InspectTruncationInfo = Field(default_factory=InspectTruncationInfo)
    error: ErrorInfo | None = None
    runtime: RuntimeMetadata | None = None


class RemoveResponse(BaseModel):
    ok: bool
    removed: list[str] = Field(default_factory=list)
    refused: list[str] = Field(default_factory=list)
    unknown: list[str] = Field(default_factory=list)
    error: ErrorInfo | None = None
    runtime: RuntimeMetadata | None = None


class ResetResponse(BaseModel):
    ok: bool
    removed: list[str] = Field(default_factory=list)
    execution_count: int
    error: ErrorInfo | None = None
    runtime: RuntimeMetadata | None = None


class DynamicToolRegistration(BaseModel):
    """Immutable protocol-facing snapshot of one advertised registration."""

    name: str
    tool_name: str
    description: str | None = None
    description_truncated: bool = False
    input_schema: dict[str, Any]
    schema_fingerprint: str
    revision: int


class RegisterToolResponse(BaseModel):
    ok: bool
    registration: DynamicToolRegistration | None = None
    error: ErrorInfo | None = None
    runtime: RuntimeMetadata | None = None


class UnregisterToolResponse(BaseModel):
    ok: bool
    unregistered: list[str] = Field(default_factory=list)
    unknown: list[str] = Field(default_factory=list)
    revision: int
    error: ErrorInfo | None = None
    runtime: RuntimeMetadata | None = None
