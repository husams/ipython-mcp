---
name: ipython-mcp
description: Use the persistent IPython MCP server for efficient multi-step Python work with reusable project helpers.
---

# Persistent IPython workflow

Use this skill when related Python steps can share imports, data, functions, or
module state. `IPYTHON_MCP_PROFILE=compact` exposes `execute` and
`call_function`; `full` exposes all 10 stable tools.

Use absolute project, data, output, and snippet paths: the server's working
directory may differ from the agent's (for example, with `uv --directory`).
Persist reviewed helpers under
`/absolute/project/root/.ipython-mcp/snippets/`; import with
`sys.path.insert(0, "/absolute/project/root/.ipython-mcp/snippets")`. Use the
configured absolute `IPYTHON_MCP_LIBRARY_PATHS` when it is already set.

## Efficient sequence

For known helper and data paths, make one `execute` call that imports the
helper, loads new input, computes the result, saves any requested artifact,
asserts a small invariant, and returns a bounded summary:

Example assuming your saved `reports.py` provides the shown functions:

```python
import json
import sys
from pathlib import Path
sys.path.insert(0, "/absolute/project/root/.ipython-mcp/snippets")
from reports import load_orders, summarize_orders

rows = load_orders("/absolute/project/root/data/orders.csv")
summary = summarize_orders(rows)
Path("/absolute/project/root/out/orders.json").write_text(
    json.dumps(summary), encoding="utf-8"
)
assert summary["count"] >= 0
{"count": summary["count"], "sample": summary["sample"][:5]}
```

Make the smallest call a meaningful checkpoint, such as a completed
transformation or verified artifact; do not spend separate calls on imports.
Use `call_function` only when its direct return is sufficient. If the result
must be retained, combined with other work, saved, or checked, call the helper
inside `execute` and retain the live result. Avoid a serialization detour that
calls a helper, then repeats it in `execute`.

For live rows, call a pure in-memory summarizer directly; use a path wrapper
for new inputs. Keep output bounded with counts, keys, samples,
limits, and explicit failures rather than returning whole datasets.

Compact clients should read the server's single JSON text response and check
`ok`, `error`, and `truncated` fields. Execution is serialized in the server's
in-process owner; cancellation is non-preemptive, so inspect the current state
before replaying side effects after an interrupted request.

State belongs to the server process, not the MCP connection. Connections to
one process share it; a new process loses it, while project files survive.
Explicitly reload edited modules (`reload` in `full`, `importlib.reload` in
`compact`). Do not create secrets, arbitrary auto-memory, or a dynamic tool for
every helper; registration is explicit in profiles that expose it.

Smaller models should use short calls with concrete checkpoints; larger models
can batch independent transformations. Both use the same correctness contract:
bounded output, reproducible inputs, explicit failures, and final verification.
Do not hardcode model IDs. The bundled `assets/helpers.py` provides a bounded
CSV summary example to adapt and persist when useful.
