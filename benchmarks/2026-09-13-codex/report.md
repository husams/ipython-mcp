# Codex CLI validation and token measurements

The final compact profile plus repository skill used **10.3% fewer total tokens with Luna** and **19.5% fewer with Astra** than the full-profile runs on this task. Both models produced correct reports, reused live objects across calls, and reused unchanged helper files in a fresh server process. These are single-run developmental measurements, including skill-reading overhead.

Enable the final profile with `IPYTHON_MCP_PROFILE=compact` and restart the server. Use the [repo skill](../../skills/ipython-mcp/SKILL.md), discovered through `.agents/skills/ipython-mcp`; its [helper example](../../skills/ipython-mcp/assets/helpers.py) can be adapted into project-owned snippets.

## Actual model token usage

Codex CLI **0.153.4**, reasoning effort **high** for both models. Each case comprises initial analysis and a separate fresh-session reuse task.

| Model | Full, no skill | First compact skill | Tuned compact skill | Final reduction vs full |
|---|---:|---:|---:|---:|
| gpt-5.6-luna | 181,157 | 229,389 | 162,437 | 10.3% |
| gpt-6-astra | 159,153 | 164,704 | 128,064 | 19.5% |

The first compact skill increased usage by 26.6% for Luna and 3.5% for Astra. Its traces showed routine status probes, extra inspection, and repeated helper calls. We refined the skill and server instructions to batch known imports, computation, artifact writing and assertions; retain intermediate results; use a pure summarizer on live rows; and reserve status checks for abnormal execution. Both revisions remain archived.

| Model | Condition | Input | Cached input | Uncached input | Output | Reasoning output |
|---|---|---:|---:|---:|---:|---:|
| gpt-5.6-luna | full | 175,316 | 137,984 | 37,332 | 5,841 | 4,105 |
| gpt-5.6-luna | tuned compact | 158,352 | 127,488 | 30,864 | 4,085 | 2,125 |
| gpt-6-astra | full | 157,210 | 112,640 | 44,570 | 1,943 | 98 |
| gpt-6-astra | tuned compact | 125,970 | 87,168 | 38,802 | 2,094 | 184 |

Total = input + output from `turn.completed.usage`. Cached input is already part of input, and reasoning output is already part of output. Neither is added twice. Cache-write input was zero in all runs. Final uncached input decreased 17.3% for Luna and 12.9% for Astra.

The 12 benchmark CLI sessions consumed **1,024,904 total tokens** across both revisions and baselines. Four recorded setup/compatibility probes consumed another **158,428**, for **1,183,332 recorded CLI tokens** overall, including cached input. This is token workload, not a monetary cost estimate; attempts without a completed usage record are not counted as zero.

## Correctness and observed behavior

The initial fixture has 8 rows: 5 valid, 3 invalid, total 3,170 cents. A later MCP call uses the original live rows to build the report and a persisted `.ipython-mcp/snippets/order_report.py` exposes `summarize_orders(rows)` and `report_orders(path)`. A second CLI process starts a new MCP server and imports that file for a different fixture: 3 valid rows, 2 invalid, total 1,300 cents.

An independent interpreter checks both JSON files, the initial quality summary, both helper entry points, both fixtures, and the helper's SHA-256 before and after the fresh-session phase. **All six cases passed every oracle check**, with no failed MCP calls. Traces confirm Python execution stayed inside MCP.

| Model | Final phase | MCP calls | Total tokens | Wall time |
|---|---|---:|---:|---:|
| gpt-5.6-luna | initial | 2 | 90,493 | 53.6 s |
| gpt-5.6-luna | reuse | 2 | 71,944 | 36.9 s |
| gpt-6-astra | initial | 2 | 74,603 | 57.7 s |
| gpt-6-astra | reuse | 1 | 53,461 | 29.4 s |

Both tuned initial phases directly reused the live row object without staging or rereading it. Astra completed fresh-session reuse in one call. Luna still inspected/imported the known helper in a separate call and repeated its initial summary computation. Astra added extra validation and an unnecessary reload. The skill explicitly recommends batching, but model behavior remains variable.

## Protocol and responsiveness checks

The default full profile still exposes its 11 tools and structured results. Compact exposes 3 tools and emits one minified JSON text block, retaining falsy/null user values, errors, truncation indicators, retry hints and runtime epoch/recovery information.

The [reproducible wire measurement](wire/measurement.json) uses real FastMCP serialization with deterministic synthetic runtime responses. Its normalized catalog is **25,036 → 864 characters (96.5% smaller)**; a scalar reply is **1,026 → 172 characters (83.2% smaller)**. These are character counts, separate from actual model tokens.

A structured-only reply initially failed the actual CLI nonce probe: the tool produced a value but Astra could not retrieve it. The final single-text-block reply passed: the generated nonce and final answer matched. Both [failed](compatibility/structured-only-events.jsonl) and [fixed](compatibility/json-text-events.jsonl) traces are retained.

Validation: **64 pytest tests passed**; the final instruction-only refinement also passed all 6 compact tests and the CLI retests. Coverage includes tools/list and ping during sleeping and CPU-bound execution over stdio and in-memory transports, catalog changes and recovery, actual compact output truncation, and token parser/oracle checks. Changed-file lint and skill validation passed.

## Reproduce and inspect

```bash
uv run --no-sync python scripts/codex_benchmark.py \
  --output-dir /tmp/ipython-mcp-benchmark-new \
  --models gpt-5.6-luna gpt-6-astra --repeats 1
```

Use a fresh directory and an already authenticated Codex CLI. The runner bounds each process, preserves raw events while it runs, checks task correctness independently, and exits nonzero on failed cases. It passes invocation-only configuration for one local MCP server, workspace-write sandboxing, and approvals for that test server; it does not edit account credentials or global configuration. Host apps, plugins and memories are disabled, and unrelated skill catalog context is capped identically. Compact runs explicitly read the copied repo skill.

- [Combined measurements](comparison.json)
- [First matrix, raw traces and generated helpers](initial/results.json)
- [Tuned Luna, raw traces and generated helper](tuned-luna/results.json)
- [Tuned Astra, raw traces and generated helper](tuned-astra/results.json)
- [Initial skill](skill-v1.md), [initial source hashes](source-v1-sha256.txt), [final source hashes](source-v2-sha256.txt)
- [Wire measurement script](wire/measure.py)

Each case has `case.json` and initial/reuse phase directories containing `argv.json`, `prompt.json`, `events.jsonl`, `stderr.log` and `phase.json`. Matching workspaces retain synthetic CSVs, output JSON and helper modules. Original absolute temporary paths in traces are preserved for provenance.

One repeat per condition, fixed evaluation order, changing cache state, host context and model nondeterminism limit generalization. Tuning was informed by the same task, so these results are not an independent holdout evaluation. The comparison measures the combined profile, server instructions and skill; it does not isolate their individual effects or establish a latency claim.
