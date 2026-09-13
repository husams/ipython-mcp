# In-process runtime Codex CLI verification

Both models completed the compact-profile workflow and reused an unchanged
persisted helper in a fresh MCP session. These four CLI sessions verify the
integrated in-process runtime's two-tool compact surface. No full-profile
baseline was rerun, so these results establish no new token reduction.

Codex CLI 0.153.4, high reasoning effort, one repeat per model, run on
2026-09-13 while integrating upstream `44f691d` into the feature branch.
The later teardown cancellation, stdio isolation and startup path-priority
fixes were validated by the automated suite; these CLI runs preceded those fixes. The model-facing
schemas, compact output and skill remained the same.

| Model | Input | Cached input | Output | Total tokens | Initial / reuse MCP calls |
| --- | ---: | ---: | ---: | ---: | --- |
| gpt-5.6-luna | 153,206 | 123,648 | 4,007 | 157,213 | 2 / 1 |
| gpt-6-astra | 124,726 | 68,608 | 2,115 | 126,841 | 2 / 1 |

Total = input + output from the CLI's completed-turn usage record; cached
input is already included in input. Four sessions consumed 284,054 total
tokens including cached input. Reasoning output, already included in output,
was 2,133 tokens for Luna and 176 for Astra. Cache-write input was zero.

All six MCP calls succeeded. The independent oracle verified each fixture's
JSON result, the initial quality summary, both helper entry points, and the
helper's unchanged SHA-256 across sessions. Both initial tasks reused live
rows across two calls; both fresh-session tasks completed in one call.

- [Combined correctness and token records](results.json)
- [Raw CLI traces](runs/)
- [Synthetic fixtures, output files and persisted helpers](workspaces/)
- [Earlier worker-process comparison](../2026-09-13-codex/report.md)

The original absolute temporary paths in prompts and events are retained for
provenance. Reproduce with an authenticated Codex CLI and a fresh output path:

```bash
uv run --no-sync python scripts/codex_benchmark.py \
  --output-dir /tmp/ipython-mcp-verification-new \
  --models gpt-5.6-luna gpt-6-astra --conditions compact --repeats 1
```

These are single-run developmental measurements on synthetic fixtures, with
changing cache state and normal model variability. They do not establish a
latency improvement or generalize token usage to other tasks.

## Final implementation checks

All 63 tests passed for Python 3.11, 3.12 and 3.13 with both lowest and locked
dependencies. The [release logs](validation/) retain the per-cell output.
Wheel/source builds, changed-file lint, skill validation and whitespace checks
also passed. The [final source hashes](final-source-sha256.txt) identify the
release-checked implementation, including fixes made after the CLI runs.

The first Python 3.11 locked run intermittently exceeded the CPU-load test's
one-second ping deadline. A bounded repeat reproduced one failure in five
runs; a three-second guard passed five of five. One instrumented run measured
ping at 0.479 s and consecutive discovery requests at 0.144 s and 0.031 s.
The test now allows three seconds for scheduling variance while still
requiring all responses before the marker-held Python operation is released.
Other matrix cells passed with the stricter one-second guard; the focused
Python 3.11 responsiveness suite passed with the revised guard. These are
regression deadlines, not a server latency SLA.
