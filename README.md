# ipython-mcp

`ipython-mcp` exposes one persistent, trusted local IPython namespace through
FastMCP. It is intended for an agent that needs to build state over several
calls: variables, functions, classes, imports, and module state survive until
the server lifespan ends or a non-cooperative operation forces worker recovery.

FastMCP and the bounded admission controller stay in the parent process. The
IPython shell, complete namespace, dynamic registry, history-disabled owner
thread, and every live Python object stay in one lifespan-owned worker process.
Only versioned, size-limited JSON protocol models cross that process boundary.
The worker is a reliability boundary that makes hard recovery possible; it is
not a sandbox or a permission boundary.

## Install and run

### From a source checkout

```bash
uv sync
uv run ipython-mcp
```

For development dependencies, use `uv sync --extra dev`.

### From the v0.1.0 wheel

```bash
uv build
uv tool install dist/ipython_mcp-0.1.0-py3-none-any.whl
ipython-mcp
```

The console entry point uses stdio and writes no non-protocol data to stdout.
Operational logs, when enabled, go to stderr and contain metadata only.

The entry point uses stdio, so configure it as a local MCP server in the client
of your choice:

```json
{
  "mcpServers": {
    "ipython": {
      "command": "uv",
      "args": ["run", "--directory", "/path/to/ipython-mcp", "ipython-mcp"]
    }
  }
}
```

## Codex setup

Register a source checkout (replace the path with an absolute path):

```bash
codex mcp add ipython -- \
  uv run --directory /absolute/path/to/ipython-mcp ipython-mcp
```

For a wheel installed with `uv tool install`, the shorter command is:

```bash
codex mcp add ipython -- ipython-mcp
```

Pass server settings without editing a file by repeating `--env` before `--`:

```bash
codex mcp add ipython \
  --env IPYTHON_MCP_OPERATION_TIMEOUT_SECONDS=20 \
  --env IPYTHON_MCP_PRELOAD_MODULES=math,json \
  -- ipython-mcp
```

Equivalent project-scoped `.codex/config.toml` fields are:

```toml
[mcp_servers.ipython]
command = "uv"
args = ["run", "--directory", "/absolute/path/to/ipython-mcp", "ipython-mcp"]
startup_timeout_sec = 15
tool_timeout_sec = 45

[mcp_servers.ipython.env]
IPYTHON_MCP_OPERATION_TIMEOUT_SECONDS = "20"
IPYTHON_MCP_PRELOAD_MODULES = "math,json"
```

Interactive Codex sessions retain their normal MCP approval policy. For a
deliberately unattended acceptance run, pre-approve this server only for that
invocation with
`-c 'mcp_servers.ipython.default_tools_approval_mode="approve"'`; without that
explicit override, `codex exec` running with approvals disabled cancels MCP
tool calls instead of silently authorizing execution.

Verify the stored entry with `codex mcp get ipython --json`, then open Codex
and ask it to call `runtime_status`. Remove the entry with
`codex mcp remove ipython`.

## Stable tool surface

The server always publishes eleven stable tools. Registered live callables are
additional, opt-in tools and are never published automatically:

- `list` discovers visible functions with their current signatures, modules,
  and docstrings.
- `execute` runs expressions, statements, definitions, and multiline blocks in
  the persistent IPython shell. It returns bounded stdout, stderr, display
  data, final values, and structured failures.
- `call_function` resolves a live name without `eval`, binds a JSON object as
  keyword arguments, invokes the callable, and returns a JSON-compatible value.
- `search` finds exact or partial names across functions, variables, modules,
  types, and other visible objects with bounded metadata.
- `reload` explicitly reloads named imported modules and refreshes their shell
  binding. It never guesses at dependencies.
- `inspect` resolves one live name and returns its kind, qualified type,
  callable status, signature when available, module, documentation, and safe
  representation. Text fields are bounded and the `truncated` object reports
  `qualified_type`, `signature`, `module`, `documentation`, and
  `representation` independently. Resolution, attribute access, metadata, and
  representation failures are returned through the response's structured
  `error` field; they do not stop the shared runtime.
- `remove` accepts a list of top-level namespace names and deterministically
  partitions unique requested names into `removed`, `refused`, and `unknown`.
  Names beginning with `_`, IPython runtime bindings (`In`, `Out`,
  `get_ipython`, `exit`, `quit`, and `open`), and active bindings for configured
  preloaded or aliased modules are protected and therefore reported in
  `refused`. Repeating a removal produces no additional removals.
- `reset` removes every unprotected user-created top-level name, reports the
  removed names in sorted order, restores configured module bindings, and
  returns the unchanged monotonic `execution_count`. A repeated reset reports
  no additional removals. All inspection and cleanup operations run through
  the same serialized shell owner as execution.
- `register_tool` publishes one top-level live callable. Its request is
  `{"name": string, "tool_name": string | null, "description": string | null}`.
  The default MCP name is exactly the backing symbol name. Success returns the
  bounded description snapshot, deterministic input schema, SHA-256 schema
  fingerprint, and registry revision; failure returns a structured error and
  leaves that registration unchanged.
- `unregister_tool` accepts `{"names": [tool_name, ...]}` and deterministically
  reports unique names in the `unregistered` and `unknown` partitions. It is
  idempotent and cannot remove a stable tool.
- `runtime_status` is out of band from the namespace queue. It reports only
  control-plane state (`ready`, `busy`, `recovering`, `unavailable`, or
  `closed`), the runtime epoch, queue depth, active-operation flag, latest
  interruption kind and namespace outcome, and measured replacement startup
  time. It never returns code, arguments, results, or namespace values.

## Deadlines, admission, and recovery

Every namespace read, mutation, dynamic discovery/reconciliation, registration
change, and callable invocation receives a monotonic admission sequence and is
dispatched FIFO. The active operation is not counted in the pending bound.
The defaults allow 32 pending requests to absorb short bursts; they deliberately
shed sustained slow load and do not promise that 32 near-30-second operations
will eventually run.

- A new request is rejected immediately with `runtime_busy` when the pending
  queue is full. A request waiting more than 30 seconds returns `queue_timeout`.
  These outcomes are retryable, do not execute code, and do not increment the
  execution count. Time in the queue does not consume the operation deadline.
- The 30-second operation deadline starts only when a request is dispatched.
  MCP cancellation before dispatch removes exactly that queued admission.
  Cancellation after dispatch follows the same interruption path as a timeout.
- The controller first injects a cooperative interruption into the worker's
  owner thread. State is reported as `preserved` only after the operation stops,
  its late result is discarded, a health probe succeeds, and registry
  reconciliation completes in the same process and epoch. Mutations performed
  before interruption remain visible, including partially updated containers.
- If the operation does not stop within the default two-second grace period,
  the parent terminates the worker and starts an atomic replacement within the
  default ten-second startup bound. The epoch advances, user names and dynamic
  registrations are cleared, configured paths/preloads are restored, stale
  results are discarded, and tool-list change is signaled when supported.
- If initial or replacement startup fails, `runtime_status` reports
  `unavailable` with a bounded error. Namespace operations fail deterministically
  until the server lifespan is restarted; no partially initialized shell is used.

Timeout responses use `operation_timeout`; active cancellations are recorded as
`operation_cancelled`. Their `runtime` metadata names the request, admission
sequence, epoch, queue wait, interruption kind, and `preserved`, `reset`, or
`unknown` namespace outcome. Request code, arguments, captured values, and
unbounded exception text are excluded from that metadata and from default logs.

## Output and shutdown bounds

stdout and stderr are retained by streaming prefix sinks while output is
produced. `truncated.stdout` / `truncated.stderr` identify truncation and
`stdout_omitted_chars` / `stderr_omitted_chars` report exact omitted character
counts. Display items are capped as they arrive and each retained data/metadata
payload is normalized through the same JSON depth, item, and character limits.
Results, representations, docs, signatures, error messages, filenames, and
tracebacks expose deterministic field-level truncation flags where applicable.

When the MCP transport closes, admission stops atomically, queued requests
complete as `runtime_closed`, active work is interrupted, and a non-cooperative
worker is terminated after the grace period. Repeated close is safe. Teardown
does not leave an IPython worker, owner thread, or history writer alive.

## Dynamic tool contract

Registration is deliberately explicit. A backing name must be a top-level
Python identifier, may not begin with `_`, and may not be an IPython runtime
or configured module binding protected by `remove` and `reset`. Dotted names
are not supported. An MCP tool name must start with an ASCII letter and then
contain only ASCII letters, digits, `_`, or `-`. Names and descriptions are
bounded by `IPYTHON_MCP_MAX_TOOL_NAME_CHARS` (default `64`) and
`IPYTHON_MCP_MAX_TOOL_DESCRIPTION_CHARS` (default `1024`). The live catalog is
bounded by `IPYTHON_MCP_MAX_DYNAMIC_TOOLS` (default `100`). Stable-name,
dynamic-name, and backing-symbol collisions are rejected without mutation.

Dynamic tools initially support synchronous callables with positional-or-
keyword and keyword-only parameters. Every parameter must have a resolvable
annotation from this bounded set:

- `str`, `int`, `float`, `bool`, and `None`;
- `list[T]`, `set[T]`, `frozenset[T]`, `tuple[T, ...]`, fixed tuples, and
  `dict[str, T]`;
- unions and optionals composed from supported types; and
- `Literal` values containing JSON-compatible strings, numbers, booleans, or
  `None`.

Synchronous classes are supported as callables. Their advertised parameters
follow Python's `inspect.signature` constructor precedence: a custom metaclass
`__call__`, then the effective `__new__` or `__init__` found through the class
MRO. Class attribute annotations are not constructor parameters. Replacing or
mutating any constructor callable consulted by that resolution triggers the
same compatibility check as a function redefinition; an incompatible change
makes the registration stale. Invoking a registered class still uses the
normal JSON result boundary, so the constructed instance must be JSON-
compatible or the call returns `result_not_json`.

Required parameters have no default. Optional parameters include their exact
JSON-compatible default in the advertised schema. Unannotated or unresolved
parameters, unsupported annotations, non-JSON defaults, positional-only
parameters, `*args`, `**kwargs`, coroutine functions, generators, async
generators, and async/generator callable objects are rejected. Return
annotations do not participate in registration compatibility.

The schema fingerprint is SHA-256 over only the input schema serialized as
sorted-key, compact JSON. Compatibility is intentionally strict: the new
schema serialization and fingerprint must be byte-identical. Adding a
parameter even with a default, removing or renaming one, changing a default,
or changing an annotation makes the registration stale. A body-only change
with the same schema remains callable and uses the current live binding.
Description or docstring changes do not alter compatibility and do not update
the registration-time description snapshot until explicit re-registration.

Every registry read and mutation, discovery reconciliation, and dynamic call
runs on the same single-owner queue as IPython execution. Discovery therefore
waits behind earlier execution and returns an immutable, reconciled snapshot.
The common unchanged path compares callable identity plus a recursive
signature-affecting token; wrapped callables and `functools.partial` functions,
arguments, and keyword state are included. Dynamic calls revalidate the live
binding and advertised schema, bind arguments, and invoke exactly once.

An incompatible replacement disappears from fresh discovery and cached calls
receive `stale_registration` until explicit re-registration. Deleting or
removing a backing symbol invalidates it, `reset` invalidates the complete
dynamic catalog, and `unregister_tool` removes only requested registrations.
Catalog changes advance a monotonic revision and emit MCP
`notifications/tools/list_changed` when the active session supports it. The
registry belongs to the server lifespan: a new lifespan starts empty and
teardown drops every registration.

## Configuration

| Environment variable | Meaning |
| --- | --- |
| `IPYTHON_MCP_LIBRARY_PATHS` | Trusted library directories separated by the platform path separator. |
| `IPYTHON_MCP_PRELOAD_MODULES` | Comma-separated modules imported at startup. |
| `IPYTHON_MCP_MODULE_ALIASES` | JSON object mapping module names to namespace aliases. |
| `IPYTHON_MCP_MAX_TEXT_CHARS` | Maximum returned text size; default `8192`. |
| `IPYTHON_MCP_MAX_REPR_CHARS` | Maximum search representation size; default `1024`. |
| `IPYTHON_MCP_MAX_TRACEBACK_CHARS` | Maximum returned traceback size; default `4096`. |
| `IPYTHON_MCP_MAX_RESULTS` | Maximum retained items in lists, mappings, and result discovery; default `100`. |
| `IPYTHON_MCP_MAX_DISPLAY_ITEMS` | Maximum display payloads retained per execution; default `20`. |
| `IPYTHON_MCP_MAX_JSON_DEPTH` | Maximum nested JSON translation depth; default `6`. |
| `IPYTHON_MCP_MAX_TOOL_NAME_CHARS` | Maximum dynamic MCP tool-name size; default `64`. |
| `IPYTHON_MCP_MAX_TOOL_DESCRIPTION_CHARS` | Maximum registration description snapshot; default `1024`. |
| `IPYTHON_MCP_MAX_DYNAMIC_TOOLS` | Maximum retained dynamic registrations; default `100`. |
| `IPYTHON_MCP_OPERATION_TIMEOUT_SECONDS` | Positive finite dispatch-to-result deadline; default `30`. |
| `IPYTHON_MCP_INTERRUPTION_GRACE_SECONDS` | Positive finite cooperative interruption grace; default `2`. |
| `IPYTHON_MCP_WORKER_STARTUP_TIMEOUT_SECONDS` | Positive finite initial/replacement startup bound; default `10`. |
| `IPYTHON_MCP_MAX_PENDING_OPERATIONS` | Positive pending FIFO capacity, excluding the active operation; default `32`. |
| `IPYTHON_MCP_QUEUE_WAIT_TIMEOUT_SECONDS` | Positive finite admission wait bound; default `30`. |
| `IPYTHON_MCP_MAX_IPC_MESSAGE_BYTES` | Positive bounded JSON IPC message size; default `4194304`. |

Every numeric setting must be positive; timeout values must also be finite.

## Build-import-edit-reload workflow

1. Put a reusable module in a configured trusted library directory.
2. Add its module name to `IPYTHON_MCP_PRELOAD_MODULES`, or import it with
   `execute`.
3. Discover functions with `list` or `search` and call them with
   `call_function`.
4. Edit the module using the agent's normal file tools.
5. Call `reload` with the explicit module name and continue using the refreshed
   binding.

The worker executes trusted code with the server user's permissions. Dynamic
registration does not add isolation: schema inspection, wrapped/partial state,
default values, annotations, callable bodies, and result conversion are all
trusted local Python. MCP and IPython do not provide a sandbox or isolation
boundary for untrusted code.

## Upgrade, remove, and troubleshoot

For a source checkout, update the checkout through its normal distribution
channel and run `uv sync --upgrade`. For a new wheel, reinstall explicitly:

```bash
uv tool install --reinstall dist/ipython_mcp-0.1.0-py3-none-any.whl
```

To remove both Codex wiring and a uv-tool installation:

```bash
codex mcp remove ipython
uv tool uninstall ipython-mcp
```

Common checks:

- `codex mcp get ipython --json` verifies the launcher, arguments, and env.
- If startup is `unavailable`, inspect the bounded `runtime_status.error` and
  verify every configured library directory and preload import in a clean shell.
- `runtime_busy` means the pending bound is full; retry after the reported
  interval. `queue_timeout` means the call never dispatched and is safe to retry.
- `operation_timeout` with `preserved` retains the same objects (including
  partial mutations). With `reset`, recreate user state and re-register tools.
- A client-side MCP timeout should exceed the configured operation deadline and
  interruption grace so the structured recovery response can be delivered.

## Compatibility and local release verification

v0.1.0 is locally tested with uv-managed CPython 3.11, 3.12, and 3.13 against
the lowest declared FastMCP/IPython/Pydantic set and the locked compatible set.
The supported macOS Codex smoke was recorded with `codex-cli 0.146.0`; newer
Codex releases should use the same stable MCP configuration surface. CPython
3.14, remote transports, multi-user hosting, package-index publication, signing,
and hosted CI are not claimed by this release.

Repository-native checks are:

```bash
uv sync --extra dev
uv run pytest
uv run python scripts/release_matrix.py
uv build
```

Run one matrix cell with, for example,
`uv run python scripts/release_matrix.py --python 3.11 --set lowest`. This
checkout intentionally has no Git repository, remote, or CI service; the six
matrix cells and Codex smoke are local release evidence, not CI claims.

## Development

```bash
uv sync --extra dev
uv run pytest
```
