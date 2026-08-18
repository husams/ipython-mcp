# ipython-mcp

`ipython-mcp` exposes one persistent, trusted local IPython namespace through
FastMCP. Variables, functions, classes, imports, module state, and explicitly
registered dynamic tools survive across calls until the server lifespan ends.

The FastMCP server directly owns exactly one in-process IPython
`InteractiveShell`. Every fixed tool, dynamic-tool lookup, and dynamic call is
serialized through one in-process boundary and works with the same live Python
objects. There is no runtime child process or object-encoding protocol.

This is intentionally not a sandbox. User Python has the server process's
permissions. Once a call starts executing Python it runs until it returns or
raises. A tool timeout or MCP cancellation cannot safely terminate that code or
promise that the shell remains reusable; non-cooperative code can block the
trusted local server and requires restarting it.

## Install and run

From a source checkout:

```bash
uv sync --extra dev
uv run ipython-mcp
```

From a built wheel:

```bash
uv build
uv tool install dist/ipython_mcp-0.1.0-py3-none-any.whl
ipython-mcp
```

The console entry point uses stdio. It writes no non-protocol data to stdout;
optional operational logs go to stderr and contain metadata only.

Register a source checkout with Codex (replace the path):

```bash
codex mcp add ipython -- \
  uv run --directory /absolute/path/to/ipython-mcp ipython-mcp
```

For a wheel installed with `uv tool install`:

```bash
codex mcp add ipython -- ipython-mcp
```

## Stable tool surface

The server publishes ten stable tools. Registered callables are additional,
opt-in tools and are never published automatically.

- `list` returns visible callables with bounded signatures, modules, and docs.
- `execute` runs expressions, statements, definitions, and multiline blocks;
  it returns bounded stdout, stderr, display data, final values, and structured
  failures.
- `call_function` resolves a live callable without `eval`, binds a JSON object
  as keyword arguments, and returns a JSON-compatible value.
- `search` finds visible objects by exact or partial name with bounded metadata.
- `reload` explicitly reloads named modules and refreshes shell bindings.
- `inspect` returns one object's kind, type, signature, module, docs, and safe
  representation with field-level truncation flags.
- `remove` partitions unique top-level names into `removed`, `refused`, and
  `unknown` while protecting IPython and configured module bindings.
- `reset` removes unprotected user names, restores configured bindings, clears
  dynamic registrations, and preserves the monotonic execution count.
- `register_tool` explicitly publishes one supported top-level synchronous
  callable with a deterministic JSON schema and fingerprint.
- `unregister_tool` idempotently removes requested dynamic registrations.

All ten paths use the same shell and registry. Live Python objects never cross
a process boundary and response models no longer contain request, queue,
worker, recovery, or epoch metadata.

## Optional startup task environment

A server can prepare one named uv environment before constructing IPython. The
environment lives under an explicit workspace outside the project. It is
created without system site packages using the same Python major/minor as the
server, or safely reused only when its recorded Python and canonical
requirements fingerprint match.

Both `IPYTHON_MCP_ENVIRONMENT_WORKSPACE` and
`IPYTHON_MCP_ACTIVE_ENVIRONMENT` are required to enable provisioning. When
both are omitted, uv is never called and the ordinary in-process startup path
is unchanged. Environment variables, trusted library paths, and preloads may
still be used without a uv environment.

Example:

```bash
codex mcp add ipython \
  --env IPYTHON_MCP_ENVIRONMENT_WORKSPACE=/absolute/task-environments \
  --env IPYTHON_MCP_ACTIVE_ENVIRONMENT=analytics-v1 \
  --env 'IPYTHON_MCP_ENVIRONMENT_REQUIREMENTS=["polars==1.32.3"]' \
  --env 'IPYTHON_MCP_ENVIRONMENT_VARIABLES={"TASK_MODE":"offline"}' \
  --env IPYTHON_MCP_LIBRARY_PATHS=/absolute/agent-libraries \
  --env IPYTHON_MCP_PRELOAD_MODULES=polars,my_agent_lib \
  --env 'IPYTHON_MCP_MODULE_ALIASES={"polars":"pl","my_agent_lib":"lib"}' \
  -- ipython-mcp
```

Equivalent project-scoped Codex configuration:

```toml
[mcp_servers.ipython]
command = "ipython-mcp"
startup_timeout_sec = 310

[mcp_servers.ipython.env]
IPYTHON_MCP_ENVIRONMENT_WORKSPACE = "/absolute/task-environments"
IPYTHON_MCP_ACTIVE_ENVIRONMENT = "analytics-v1"
IPYTHON_MCP_ENVIRONMENT_REQUIREMENTS = '["polars==1.32.3"]'
IPYTHON_MCP_ENVIRONMENT_VARIABLES = '{"TASK_MODE":"offline"}'
IPYTHON_MCP_LIBRARY_PATHS = "/absolute/agent-libraries"
IPYTHON_MCP_PRELOAD_MODULES = "polars,my_agent_lib"
IPYTHON_MCP_MODULE_ALIASES = '{"polars":"pl","my_agent_lib":"lib"}'
```

Startup order is fixed:

1. Validate the workspace, safe environment name, bounded PEP 508
   requirements, variables, paths, module names, and aliases.
2. Create a temporary uv environment and atomically rename it into place, or
   verify an existing environment's metadata.
3. Prepend the selected site-packages, then apply configured variables and
   trusted library paths.
4. Import every preload and validate its unique, non-protected alias.
5. Construct the only `InteractiveShell` and bind the preloaded modules.

The selected environment and all startup configuration are immutable for that
server process. Change configuration by restarting the server. A different
dependency set should use a new environment name; no environment create,
switch, list, delete, or activate MCP tools exist.

Configuration, path, uv, dependency, preload, or binding failures abort the
lifespan before a shell is exposed. Temporary directories are removed, process
environment and `sys.path` changes are rolled back, and the project is not
modified. Diagnostics identify the failed phase, are bounded by
`IPYTHON_MCP_MAX_TEXT_CHARS`, redact configured variable values and URL
credentials, and are never persisted in the environment metadata.

## Configuration reference

| Environment variable | Meaning |
| --- | --- |
| `IPYTHON_MCP_ENVIRONMENT_WORKSPACE` | Absolute workspace outside the project; configure with `ACTIVE_ENVIRONMENT`. |
| `IPYTHON_MCP_ACTIVE_ENVIRONMENT` | Safe environment name (`A-Z`, `a-z`, digits, `.`, `_`, `-`; at most 64 chars). |
| `IPYTHON_MCP_ENVIRONMENT_REQUIREMENTS` | JSON array of bounded PEP 508 requirements; requires an active environment. |
| `IPYTHON_MCP_ENVIRONMENT_VARIABLES` | JSON object of environment-variable string values applied before preloads. |
| `IPYTHON_MCP_ENVIRONMENT_SETUP_TIMEOUT_SECONDS` | Positive finite timeout for each uv phase; default `300`. |
| `IPYTHON_MCP_LIBRARY_PATHS` | Trusted library directories separated by the platform path separator. |
| `IPYTHON_MCP_PRELOAD_MODULES` | Comma-separated modules imported before shell construction. |
| `IPYTHON_MCP_MODULE_ALIASES` | JSON object mapping configured preload modules to unique safe bindings. |
| `IPYTHON_MCP_MAX_TEXT_CHARS` | Returned text and startup-diagnostic bound; default `8192`. |
| `IPYTHON_MCP_MAX_REPR_CHARS` | Representation bound; default `1024`. |
| `IPYTHON_MCP_MAX_TRACEBACK_CHARS` | Traceback bound; default `4096`. |
| `IPYTHON_MCP_MAX_RESULTS` | Retained collection/discovery items; default `100`. |
| `IPYTHON_MCP_MAX_DISPLAY_ITEMS` | Retained display payloads per execution; default `20`. |
| `IPYTHON_MCP_MAX_JSON_DEPTH` | Nested JSON translation depth; default `6`. |
| `IPYTHON_MCP_MAX_TOOL_NAME_CHARS` | Dynamic MCP tool-name bound; default `64`. |
| `IPYTHON_MCP_MAX_TOOL_DESCRIPTION_CHARS` | Dynamic description bound; default `1024`. |
| `IPYTHON_MCP_MAX_DYNAMIC_TOOLS` | Dynamic registration bound; default `100`. |

## Output, namespace, and logging bounds

stdout and stderr use streaming prefix sinks whose retained memory is
independent of produced output size. Exact omitted-character counts accompany
their truncation flags. Display items are capped as they arrive. Results,
representations, docs, signatures, filenames, error messages, and tracebacks
use deterministic depth, item, and character bounds.

Names beginning with `_`, IPython bindings (`In`, `Out`, `get_ipython`,
`exit`, `quit`, and `open`), and configured preload aliases are protected from
`remove`, `reset`, and dynamic registration. `reset` restores configured
modules. Default logs contain tool name and outcome only—not code, arguments,
results, namespace values, environment-variable values, or traceback locals.

## Dynamic tool contract

A backing name must be a non-protected top-level Python identifier. Dynamic
tool names start with an ASCII letter and contain only letters, digits, `_`, or
`-`. Stable-name, dynamic-name, and backing-symbol collisions are rejected.

Supported parameters use resolvable annotations composed from `str`, `int`,
`float`, `bool`, `None`, bounded containers, unions/optionals, and JSON-safe
`Literal` values. Positional-only parameters, variadics, unresolved or
unsupported annotations, non-JSON defaults, coroutine functions, generators,
and async/generator callable objects are rejected.

The schema fingerprint is SHA-256 over sorted compact input-schema JSON.
Body-only replacement with an identical schema stays callable through the
current live binding. A signature-affecting change makes the registration
stale until explicit re-registration. Delete/remove invalidate the affected
registration; reset clears the catalog. Catalog changes emit
`notifications/tools/list_changed` when the client supports it.

## Migration from the F-004 runtime

F-005 is a deliberate breaking simplification. It removes the F-004
multiprocessing worker, controller, versioned IPC, pipes, reader/writer loops,
health probes, worker replacement, runtime epochs, stale-response handling,
process admission queue, queue limits, worker startup/interruption grace,
hard operation timeout recovery, worker shutdown logic, response `runtime`
metadata, and `runtime_status` tool.

Remove these obsolete settings from client configuration:

- `IPYTHON_MCP_OPERATION_TIMEOUT_SECONDS`
- `IPYTHON_MCP_INTERRUPTION_GRACE_SECONDS`
- `IPYTHON_MCP_WORKER_STARTUP_TIMEOUT_SECONDS`
- `IPYTHON_MCP_MAX_PENDING_OPERATIONS`
- `IPYTHON_MCP_QUEUE_WAIT_TIMEOUT_SECONDS`
- `IPYTHON_MCP_MAX_IPC_MESSAGE_BYTES`

If callers relied on forced termination, queue overload responses, recovery
epochs, or status polling, they must instead apply a client-side observation
timeout and restart the entire trusted local server when code does not return.
A client-side timeout does not imply that Python stopped.

## Build-import-edit-reload workflow

1. Put a reusable module in a configured trusted library directory.
2. Preload it or import it with `execute`.
3. Discover functions with `list` or `search` and call them with
   `call_function`.
4. Edit the module using normal file tools.
5. Call `reload` with the explicit module name.

## Verification

Repository-native checks are:

```bash
uv sync --extra dev
uv run pytest
uv run python scripts/release_matrix.py
uv build
```

The tests cover the unconfigured startup path, direct same-process ownership,
persistent state and dynamic tools, absence of runtime child processes and IPC
modules, output bounds, startup variables/paths/preloads, uv reuse, conflicting
pure-Python dependency versions across separate restarts, clean/redacted
failures, stdio packaging flow, and the explicitly non-preemptive contract.
