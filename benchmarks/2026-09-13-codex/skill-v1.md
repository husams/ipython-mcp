---
name: ipython-mcp
description: Use the persistent IPython MCP server for multi-step Python analysis, stateful transformations, and reusable project-owned helpers.
---

# Persistent IPython work

Use this skill when a task has several related Python steps or needs data,
imports, functions, or module state to persist. Profiles:
`IPYTHON_MCP_PROFILE=full` exposes all 11 stable tools; `compact` exposes
`execute`, `call_function`, and `runtime_status`.

## Working loop

1. Inspect once with a bounded expression. Load or import data once, then keep
   the objects in the live namespace.
2. Batch related definitions or independent transformations in one `execute`
   call. Use it for known expressions or statements. In `full`, use `search`,
   `inspect`, or `list` only when discovery is needed. In `compact`, use
   `execute` with a small expression such as
   `sorted(name for name in globals() if not name.startswith("_"))[:20]` for
   discovery and `importlib.reload(module)` for explicit module reload.
3. Return bounded summaries (counts, keys, samples, min/max, and explicit
   errors), keeping limits in the expression.
4. Reuse known live functions with `call_function`. Names, object identities,
   and imports persist for the server lifespan.

Treat every response as a contract, parsing compact mode's single JSON text
payload and checking `ok`, `error`, `truncated`, and `runtime.epoch`; after an
epoch change, reload persisted helpers/data before trusting names and never
blindly replay side effects after interruption.

Staged example:

```python
# execute: load once
import csv
with open("/absolute/project/root/data/input.csv", newline="") as source:
    rows = list(csv.DictReader(source))

# execute: define once, return a bounded result
def summarize(limit=20):
    return {"count": len(rows), "sample": rows[:limit]}
summarize(5)
```

Use absolute paths for project files, data, and snippets because the server's
working directory can differ (for example, with `uv --directory`). Persist
reviewed, parameterized helpers under
`/absolute/project/root/.ipython-mcp/snippets/`. Import with
`sys.path.insert(0, "/absolute/project/root/.ipython-mcp/snippets")`, or use
`IPYTHON_MCP_LIBRARY_PATHS` with an absolute directory when already configured.
In `full`, reload with the explicit `reload` tool; in `compact`, use
`importlib.reload`. The bundled `assets/helpers.py` is a small stdlib example
for bounded CSV summaries. Do not create secrets, arbitrary auto-memory, or a
dynamic tool for every helper; in `full`, registration is opt-in.

Live state belongs to the server process, not the MCP connection. Connections
to one process share it; a new process loses it. Project snippets survive.

Adapt to model size: smaller models use short calls with checkpoints; larger
models batch independent transforms. Both use the same contract: bounded
output, explicit failures, reproducible inputs, and final verification.
Do not hardcode model IDs.
