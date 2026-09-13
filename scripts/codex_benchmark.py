"""Run the Codex CLI benchmark for persistent IPython workflows.

The runner deliberately keeps the model-facing part small.  It creates a
deterministic workspace and two CSV fixtures, invokes two independent
``codex exec`` processes, and validates the files produced by the model in a
separate Python process.  Unit tests can exercise the parser and oracle
without invoking Codex.
"""

import argparse
import csv
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict, dataclass
from typing import Any, Iterable, Sequence


DEFAULT_MODELS = ("gpt-5.6-luna", "gpt-6-astra")
DEFAULT_CONDITIONS = ("full", "compact")
CSV_FIELDS = ("region", "units", "unit_price_cents")
SKILL_SOURCE = Path(__file__).resolve().parents[1] / "skills" / "ipython-mcp"

INITIAL_ROWS: tuple[tuple[str, str, str], ...] = (
    ("north", "3", "125"),
    ("south", "2", "250"),
    ("north", "1", "1000"),
    ("west", "4", "75"),
    ("south", "5", "199"),
    ("bad-units", "0", "400"),
    ("bad-price", "2", "nope"),
    ("", "3", "300"),
)
REUSE_ROWS: tuple[tuple[str, str, str], ...] = (
    ("north", "2", "100"),
    ("west", "1", "500"),
    ("south", "3", "200"),
    ("bad-units", "-1", "100"),
    ("bad-price", "2", "bad"),
)


class BenchmarkError(RuntimeError):
    """Raised for a benchmark setup or validation error."""


@dataclass
class Usage:
    input_tokens: int
    cached_input_tokens: int
    cache_write_input_tokens: int
    output_tokens: int
    reasoning_output_tokens: int

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    @property
    def uncached_input_tokens(self) -> int:
        return self.input_tokens - self.cached_input_tokens


@dataclass
class ParsedRun:
    events: list[dict[str, Any]]
    last_message: str
    usage: Usage | None
    mcp_tool_calls: int
    mcp_tool_names: list[str]
    mcp_tool_evidence: list[dict[str, Any]]
    mcp_tool_successes: int
    mcp_tool_failures: int
    errors: list[str]
    turn_completed: bool


def _json_object(value: str) -> dict[str, Any]:
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise BenchmarkError(f"invalid JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise BenchmarkError("expected a JSON object")
    return parsed


def deduplicate_events(events: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Remove replayed JSON events while preserving stream order."""

    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for event in events:
        item = event.get("item")
        event_id = item.get("id") if isinstance(item, dict) else event.get("id")
        if isinstance(event_id, str):
            # Codex emits an item.started event and then an item.completed
            # event with the same item id.  Keep both; repeated completed
            # events are the duplicates that would inflate MCP-call counts.
            key = f"id:{event_id}:{event.get('type', '')}"
        elif event.get("type") == "turn.completed":
            # A Codex exec has one completed turn.  This also protects usage
            # aggregation when a transport retries the final event.
            key = "turn.completed"
        else:
            key = "event:" + json.dumps(event, sort_keys=True, separators=(",", ":"))
        if key in seen:
            continue
        seen.add(key)
        result.append(event)
    return result


def parse_usage(value: Any) -> Usage | None:
    """Parse a turn.completed usage object; missing fields stay unavailable."""

    if not isinstance(value, dict):
        return None
    names = (
        "input_tokens",
        "cached_input_tokens",
        "cache_write_input_tokens",
        "output_tokens",
        "reasoning_output_tokens",
    )
    if any(not isinstance(value.get(name), int) or value[name] < 0 for name in names):
        return None
    return Usage(*(value[name] for name in names))


def parse_mcp_result(item: dict[str, Any]) -> dict[str, Any] | None:
    """Read either structured MCP output or the JSON text fallback."""

    result = item.get("result")
    if not isinstance(result, dict):
        return None
    structured = result.get("structured_content")
    if isinstance(structured, dict):
        return structured
    content = result.get("content")
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and isinstance(block.get("text"), str):
                try:
                    parsed = json.loads(block["text"])
                except json.JSONDecodeError:
                    continue
                if isinstance(parsed, dict):
                    return parsed
    return None


def parse_event_lines(lines: Iterable[str]) -> ParsedRun:
    """Parse Codex ``--json`` output and count completed MCP calls once."""

    events: list[dict[str, Any]] = []
    errors: list[str] = []
    for line_number, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError as exc:
            errors.append(f"line {line_number}: invalid JSON ({exc.msg})")
            continue
        if isinstance(event, dict):
            events.append(event)
        else:
            errors.append(f"line {line_number}: event is not an object")
    events = deduplicate_events(events)
    usage: Usage | None = None
    turn_completed = False
    last_message = ""
    mcp_tool_names: list[str] = []
    mcp_tool_evidence: list[dict[str, Any]] = []
    mcp_tool_successes = mcp_tool_failures = 0
    for event in events:
        if event.get("type") == "turn.completed":
            turn_completed = True
            usage = parse_usage(event.get("usage"))
            if usage is None:
                errors.append("turn.completed has missing or invalid usage")
        item = event.get("item")
        if not isinstance(item, dict) or event.get("type") != "item.completed":
            continue
        item_type = item.get("type")
        if item_type == "agent_message":
            text = item.get("text")
            if isinstance(text, str):
                last_message = text
        elif item_type == "mcp_tool_call":
            name = item.get("name") or item.get("tool_name") or item.get("tool")
            tool_name = str(name) if name else "<unknown>"
            mcp_tool_names.append(tool_name)
            arguments = item.get("arguments")
            if isinstance(arguments, dict):
                argument_text = json.dumps(arguments, sort_keys=True)
                if len(argument_text) > 4000:
                    arguments = {"_truncated": argument_text[:4000]}
            result_payload = parse_mcp_result(item)
            status = item.get("status")
            error = item.get("error")
            successful = status == "completed" and error is None and result_payload is not None and result_payload.get("ok", True) is not False
            if successful:
                mcp_tool_successes += 1
            else:
                mcp_tool_failures += 1
            mcp_tool_evidence.append(
                {
                    "tool": tool_name,
                    "arguments": arguments,
                    "status": item.get("status"),
                    "error": item.get("error"),
                    "result": result_payload,
                    "successful": successful,
                }
            )
    if not turn_completed:
        errors.append("no turn.completed event")
    if turn_completed and usage is None:
        errors.append("usage unavailable")
    return ParsedRun(
        events=events,
        last_message=last_message,
        usage=usage,
        mcp_tool_calls=len(mcp_tool_names),
        mcp_tool_names=mcp_tool_names,
        mcp_tool_evidence=mcp_tool_evidence,
        mcp_tool_successes=mcp_tool_successes,
        mcp_tool_failures=mcp_tool_failures,
        errors=list(dict.fromkeys(errors)),
        turn_completed=turn_completed,
    )


# Short alias useful to callers that already have a JSONL event stream.
parse_events = parse_event_lines


def _valid_row(row: dict[str, str]) -> tuple[str, int, int] | None:
    region = row.get("region", "").strip()
    if not region:
        return None
    try:
        units = int(row.get("units", ""))
        price = int(row.get("unit_price_cents", ""))
    except (TypeError, ValueError):
        return None
    if units <= 0 or price <= 0:
        return None
    return region, units, price


def expected_report(path: Path) -> dict[str, Any]:
    """Compute the CSV contract independently of the model helper."""

    valid_rows = invalid_rows = total_cents = 0
    by_region: dict[str, int] = {}
    with path.open(newline="", encoding="utf-8") as stream:
        for row in csv.DictReader(stream):
            parsed = _valid_row(row)
            if parsed is None:
                invalid_rows += 1
                continue
            region, units, price = parsed
            valid_rows += 1
            cents = units * price
            total_cents += cents
            by_region[region] = by_region.get(region, 0) + cents
    return {
        "valid_rows": valid_rows,
        "invalid_rows": invalid_rows,
        "total_cents": total_cents,
        "by_region": dict(sorted(by_region.items())),
    }


def write_fixture(path: Path, rows: Sequence[tuple[str, str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.writer(stream, lineterminator="\n")
        writer.writerow(CSV_FIELDS)
        writer.writerows(rows)


def create_fixtures(workspace: Path) -> tuple[Path, Path, dict[str, Any], dict[str, Any]]:
    initial = workspace / "orders_initial.csv"
    reuse = workspace / "orders_reuse.csv"
    write_fixture(initial, INITIAL_ROWS)
    write_fixture(reuse, REUSE_ROWS)
    return initial, reuse, expected_report(initial), expected_report(reuse)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


_ORACLE_PROGRAM = r'''
import csv, hashlib, importlib.util, json, pathlib, sys
workspace = pathlib.Path(sys.argv[1])
initial_expected = json.loads(sys.argv[2])
reuse_expected = json.loads(sys.argv[3])
expected_helper_sha256 = sys.argv[4] or None
helper_path = workspace / ".ipython-mcp" / "snippets" / "order_report.py"
before = hashlib.sha256(helper_path.read_bytes()).hexdigest() if helper_path.exists() else None
result = {"ok": False, "errors": []}
if before is None:
    result["errors"].append("helper module missing")
else:
    spec = importlib.util.spec_from_file_location("order_report_oracle", helper_path)
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
        first = module.report_orders(str(workspace / "orders_initial.csv"))
        second = module.report_orders(str(workspace / "orders_reuse.csv"))
        with (workspace / "orders_initial.csv").open(newline="", encoding="utf-8") as stream:
            initial_rows_data = list(csv.DictReader(stream))
        summarize = getattr(module, "summarize_orders", None)
        summarized = summarize(initial_rows_data) if callable(summarize) else None
        result_path = workspace / "result.json"
        reuse_path = workspace / "reuse.json"
        if not result_path.exists() or not reuse_path.exists():
            result["errors"].append("result.json or reuse.json missing")
        else:
            result_data = json.loads(result_path.read_text(encoding="utf-8"))
            reuse_data = json.loads(reuse_path.read_text(encoding="utf-8"))
            result["result_match"] = result_data.get("report") == initial_expected
            initial_rows = initial_valid = initial_invalid = 0
            with (workspace / "orders_initial.csv").open(newline="", encoding="utf-8") as stream:
                for row in csv.DictReader(stream):
                    initial_rows += 1
                    try:
                        if not row.get("region", "").strip() or int(row.get("units", "")) <= 0 or int(row.get("unit_price_cents", "")) <= 0:
                            raise ValueError
                    except (AttributeError, TypeError, ValueError):
                        initial_invalid += 1
                    else:
                        initial_valid += 1
            result["initial_summary_match"] = result_data.get("initial_summary") == {"rows": initial_rows, "valid_rows": initial_valid, "invalid_rows": initial_invalid}
            result["reuse_match"] = reuse_data == reuse_expected
            result["helper_initial_match"] = first == initial_expected
            result["helper_reuse_match"] = second == reuse_expected
            result["helper_summarize_match"] = summarized == initial_expected
            if not result["result_match"]: result["errors"].append("result report mismatch")
            if not result["initial_summary_match"]: result["errors"].append("initial summary mismatch")
            if not result["reuse_match"]: result["errors"].append("reuse report mismatch")
            if not result["helper_initial_match"]: result["errors"].append("helper initial mismatch")
            if not result["helper_reuse_match"]: result["errors"].append("helper reuse mismatch")
            if not result["helper_summarize_match"]: result["errors"].append("helper summarize_orders mismatch or missing")
    except Exception as exc:
        result["errors"].append(f"oracle import/call failed: {type(exc).__name__}: {exc}")
    after = hashlib.sha256(helper_path.read_bytes()).hexdigest()
    result["unchanged_sha256"] = before == after
    result["helper_sha256"] = after
    result["helper_matches_initial_sha256"] = expected_helper_sha256 is None or before == expected_helper_sha256
    if not result["helper_matches_initial_sha256"]:
        result["errors"].append("helper differs from post-initial-phase hash")
    if before != after:
        result["errors"].append("helper changed during oracle validation")
result["ok"] = not result["errors"]
print(json.dumps(result, sort_keys=True))
'''


def run_oracle(workspace: Path, initial_expected: dict[str, Any], reuse_expected: dict[str, Any], expected_helper_sha256: str | None = None) -> dict[str, Any]:
    """Validate model artifacts in an isolated interpreter."""

    command = [
        sys.executable,
        "-I",
        "-c",
        _ORACLE_PROGRAM,
        str(workspace),
        json.dumps(initial_expected, sort_keys=True),
        json.dumps(reuse_expected, sort_keys=True),
        expected_helper_sha256 or "",
    ]
    try:
        completed = subprocess.run(command, cwd=workspace, capture_output=True, text=True, timeout=30)
    except subprocess.TimeoutExpired:
        return {"ok": False, "errors": ["oracle timed out after 30s"]}
    if completed.returncode != 0:
        return {"ok": False, "errors": [f"oracle exited {completed.returncode}: {completed.stderr.strip()}"]}
    try:
        return _json_object(completed.stdout.strip())
    except BenchmarkError as exc:
        return {"ok": False, "errors": [str(exc), completed.stdout[-500:]]}


def codex_command(model: str, workspace: Path, profile: str, timeout: float, repo_root: Path) -> list[str]:
    """Build the exact unattended Codex invocation for one fresh phase."""

    python = Path(os.path.abspath(repo_root / ".venv" / "bin" / "python"))
    config = [
        "model_reasoning_effort=\"high\"",
        f"mcp_servers.ipython.command={json.dumps(str(python))}",
        'mcp_servers.ipython.args=["-m","ipython_mcp.server"]',
        "mcp_servers.ipython.required=true",
        'mcp_servers.ipython.default_tools_approval_mode="approve"',
        f"mcp_servers.ipython.cwd={json.dumps(str(workspace))}",
        f"mcp_servers.ipython.startup_timeout_sec={max(30, int(timeout))}",
        f"mcp_servers.ipython.tool_timeout_sec={max(30, int(timeout))}",
        f"mcp_servers.ipython.env.IPYTHON_MCP_PROFILE={json.dumps(profile)}",
        "skills.max_context_tokens=1",
    ]
    command = [
        "codex",
        "exec",
        "--ignore-user-config",
        "--ephemeral",
        "--skip-git-repo-check",
        "--sandbox",
        "workspace-write",
        "--json",
        "-m",
        model,
        "-C",
        str(workspace),
    ]
    for value in config:
        command.extend(("-c", value))
    command.extend(("--disable", "apps", "--disable", "plugins", "--disable", "memories"))
    return command


def _phase_prompt(workspace: Path, phase: str, optimized: bool) -> str:
    skill = workspace / ".agents" / "skills" / "ipython-mcp" / "SKILL.md"
    skill_line = f"Read and follow the repository skill at {skill}." if optimized else "Do not use an agent skill."
    if phase == "initial":
        return f"""Work in {workspace}. {skill_line}
Use exactly the configured IPython MCP server for the Python work. Make at least two MCP calls: first execute code that loads {workspace / 'orders_initial.csv'} once into live rows, then use a later MCP call to reuse those live rows for the report; do not reread the initial CSV for that report. Return a small quality summary with the exact keys rows, valid_rows, and invalid_rows, with counts derived from the live rows. Define a reusable helper in {workspace / '.ipython-mcp/snippets/order_report.py'} exposing summarize_orders(rows) and report_orders(path) -> {{valid_rows, invalid_rows, total_cents, by_region}}. The helper must count only rows with a nonempty region and positive integer units and unit_price_cents, count all other rows as invalid, and aggregate units*unit_price_cents by region; report_orders must parse its path and delegate to summarize_orders. Use the live objects from the initial analysis when producing the final report. Write {workspace / 'result.json'} as JSON with exactly {{"initial_summary": <object with rows, valid_rows, invalid_rows>, "report": <report_orders result>}}. Keep MCP responses bounded. Do not create other helper modules."""
    return f"""Start a fresh Codex session in {workspace}. {skill_line}
Use the configured IPython MCP execute or call_function tool for all Python work. Use the existing unchanged helper at {workspace / '.ipython-mcp/snippets/order_report.py'}; do not rewrite, touch, or regenerate it. Load {workspace / 'orders_reuse.csv'} and write {workspace / 'reuse.json'} as exactly the report_orders result for that file. Reuse the persistent IPython state where available, but the helper file must remain byte-for-byte unchanged. Finish with a concise statement of the MCP calls and state or module reuse; do not print the whole CSV."""


def run_phase(command: Sequence[str], prompt: str, output_dir: Path, timeout: float) -> dict[str, Any]:
    """Run one owned process group and persist raw stdout/stderr immediately."""

    output_dir.mkdir(parents=True, exist_ok=True)
    stdout_path = output_dir / "events.jsonl"
    stderr_path = output_dir / "stderr.log"
    _atomic_write_json(output_dir / "prompt.json", {"prompt": prompt})
    _atomic_write_json(output_dir / "argv.json", {"argv": list(command)})
    started = time.monotonic()
    timed_out = False
    completed: subprocess.CompletedProcess[str] | None = None
    process: subprocess.Popen[str] | None = None
    try:
        with stdout_path.open("w", encoding="utf-8") as stdout_stream, stderr_path.open("w", encoding="utf-8") as stderr_stream:
            process = subprocess.Popen(
                list(command),
                stdin=subprocess.PIPE,
                stdout=stdout_stream,
                stderr=stderr_stream,
                text=True,
                cwd=output_dir.parent.parent,
                start_new_session=True,
            )
            try:
                process.communicate(prompt, timeout=timeout)
            except subprocess.TimeoutExpired:
                timed_out = True
                os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait()
            completed = subprocess.CompletedProcess(process.args, process.returncode)
    except subprocess.TimeoutExpired:
        timed_out = True
        if process is not None:
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
            completed = subprocess.CompletedProcess(process.args, process.returncode)
        else:
            completed = subprocess.CompletedProcess(list(command), -signal.SIGTERM)
    duration = time.monotonic() - started
    if completed is None:
        raise BenchmarkError("Codex process did not produce a completion result")
    stdout_text = stdout_path.read_text(encoding="utf-8")
    parsed = parse_event_lines(stdout_text.splitlines())
    (output_dir / "last_message.txt").write_text(parsed.last_message, encoding="utf-8")
    errors = list(parsed.errors)
    if timed_out:
        errors.append(f"timeout after {timeout:g}s")
    if completed.returncode != 0:
        errors.append(f"codex exited {completed.returncode}")
    record: dict[str, Any] = {
        "status": "success" if not errors else "failed",
        "returncode": completed.returncode,
        "duration_seconds": round(duration, 6),
        "timed_out": timed_out,
        "last_message": parsed.last_message,
        "mcp_tool_calls": parsed.mcp_tool_calls,
        "mcp_tool_names": parsed.mcp_tool_names,
        "mcp_tool_evidence": parsed.mcp_tool_evidence,
        "mcp_tool_successes": parsed.mcp_tool_successes,
        "mcp_tool_failures": parsed.mcp_tool_failures,
        "state_usage": {
            "mcp_tool_call_count": parsed.mcp_tool_calls,
            "successful_tool_calls": parsed.mcp_tool_successes,
            "failed_tool_calls": parsed.mcp_tool_failures,
            "tool_names": parsed.mcp_tool_names,
            "evidence": parsed.mcp_tool_evidence,
        },
        "turn_completed": parsed.turn_completed,
        "errors": list(dict.fromkeys(errors)),
    }
    if parsed.usage is not None:
        record["usage"] = asdict(parsed.usage)
        record["usage"].update(
            total_tokens=parsed.usage.total_tokens,
            uncached_input_tokens=parsed.usage.uncached_input_tokens,
        )
    else:
        record["usage"] = None
    record["usage_status"] = "available" if parsed.usage is not None else "unavailable"
    _atomic_write_json(output_dir / "phase.json", record)
    return record


def _atomic_write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as stream:
        json.dump(value, stream, indent=2, sort_keys=True)
        stream.write("\n")
        temporary = Path(stream.name)
    os.replace(temporary, path)


def _atomic_write_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as stream:
        stream.write(value)
        temporary = Path(stream.name)
    os.replace(temporary, path)


def _case_selected(model: str, condition: str, filters: Sequence[str]) -> bool:
    if not filters:
        return True
    names = {model, condition, f"{model}/{condition}", f"{model}:{condition}", f"{condition}/{model}"}
    return any(value in names for value in filters)


def run_case(model: str, condition: str, repeat: int, output_root: Path, timeout: float, repo_root: Path) -> dict[str, Any]:
    workspace = output_root / "workspaces" / model / condition / f"repeat-{repeat}"
    if workspace.exists() and any(workspace.iterdir()):
        raise BenchmarkError(f"case workspace already contains artifacts: {workspace}")
    workspace.mkdir(parents=True, exist_ok=True)
    if condition == "compact":
        destination = workspace / ".agents" / "skills" / "ipython-mcp"
        shutil.copytree(SKILL_SOURCE, destination, dirs_exist_ok=True)
    initial, reuse, initial_expected, reuse_expected = create_fixtures(workspace)
    phase_root = output_root / "runs" / model / condition / f"repeat-{repeat}"
    first_prompt = _phase_prompt(workspace, "initial", condition == "compact")
    second_prompt = _phase_prompt(workspace, "reuse", condition == "compact")
    first = run_phase(codex_command(model, workspace, condition, timeout, repo_root), first_prompt, phase_root / "initial", timeout)
    helper_path = workspace / ".ipython-mcp" / "snippets" / "order_report.py"
    helper_hash_after_initial = sha256(helper_path) if helper_path.exists() else None
    second = run_phase(codex_command(model, workspace, condition, timeout, repo_root), second_prompt, phase_root / "reuse", timeout)
    helper_hash_after_reuse = sha256(helper_path) if helper_path.exists() else None
    oracle = run_oracle(workspace, initial_expected, reuse_expected, helper_hash_after_initial)
    oracle["helper_unchanged_between_phases"] = helper_hash_after_initial == helper_hash_after_reuse and helper_hash_after_initial is not None
    if not oracle["helper_unchanged_between_phases"]:
        oracle.setdefault("errors", []).append("helper changed or was missing between phases")
        oracle["ok"] = False
    correctness = (
        first["status"] == second["status"] == "success"
        and first["mcp_tool_successes"] >= 2
        and second["mcp_tool_successes"] >= 1
        and bool(oracle.get("ok"))
    )
    metrics: dict[str, Any] = {"status": "success" if correctness else "failed", "phases": {"initial": first, "reuse": second}, "oracle": oracle, "task_correctness": correctness}
    if first.get("usage") and second.get("usage"):
        metrics["usage_total"] = {
            name: first["usage"][name] + second["usage"][name]
            for name in ("input_tokens", "cached_input_tokens", "cache_write_input_tokens", "output_tokens", "reasoning_output_tokens", "total_tokens", "uncached_input_tokens")
        }
    _atomic_write_json(phase_root / "case.json", metrics)
    return {"model": model, "condition": condition, "repeat": repeat, **metrics}


def markdown_summary(records: Sequence[dict[str, Any]]) -> str:
    lines = ["# Codex benchmark", "", "| model | condition | repeat | status | total tokens | correctness |", "|---|---|---:|---|---:|---|"]
    for record in records:
        usage = record.get("usage_total") or {}
        total = usage.get("total_tokens", "unavailable")
        lines.append(f"| {record['model']} | {record['condition']} | {record['repeat']} | {record['status']} | {total} | {record.get('task_correctness', False)} |")
    lines.extend(["", "## Paired savings", "", "| model | repeat | full tokens | compact tokens | saved | saved % |", "|---|---:|---:|---:|---:|---:|"])
    for saving in paired_savings(records):
        lines.append(f"| {saving['model']} | {saving['repeat']} | {saving['full_total_tokens']} | {saving['compact_total_tokens']} | {saving['saved_tokens']} | {saving['saved_percent']:.2f}% |")
    lines.extend(["", "Token savings include only paired successful full/compact cases. A single repeat is subject to normal prompt and cache effects.", ""])
    return "\n".join(lines)


def paired_savings(records: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return savings only where both paired conditions completed correctly."""

    by_key: dict[tuple[str, int], dict[str, dict[str, Any]]] = {}
    for record in records:
        key = (str(record.get("model")), int(record.get("repeat", 0)))
        by_key.setdefault(key, {})[str(record.get("condition"))] = record
    result: list[dict[str, Any]] = []
    for (model, repeat), pair in sorted(by_key.items()):
        full = pair.get("full")
        compact = pair.get("compact")
        if not full or not compact or full.get("status") != "success" or compact.get("status") != "success":
            continue
        full_total = (full.get("usage_total") or {}).get("total_tokens")
        compact_total = (compact.get("usage_total") or {}).get("total_tokens")
        if not isinstance(full_total, int) or not isinstance(compact_total, int):
            continue
        saved = full_total - compact_total
        result.append({
            "model": model,
            "repeat": repeat,
            "full_total_tokens": full_total,
            "compact_total_tokens": compact_total,
            "saved_tokens": saved,
            "saved_percent": (saved / full_total * 100) if full_total else 0.0,
        })
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=Path("codex-benchmark-results"))
    parser.add_argument("--models", nargs="+", default=list(DEFAULT_MODELS))
    parser.add_argument("--conditions", nargs="+", choices=DEFAULT_CONDITIONS, default=list(DEFAULT_CONDITIONS))
    parser.add_argument("--timeout", type=float, default=240.0)
    parser.add_argument("--repeats", type=int, default=1)
    parser.add_argument("--case", "--filter", dest="filters", action="append", default=[], help="Run only model, condition, or model/condition")
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1], help="Repository root containing .venv and skills/")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.timeout <= 0 or args.repeats < 1:
        raise SystemExit("--timeout must be positive and --repeats must be at least 1")
    output_root = args.output_dir.resolve()
    output_root.mkdir(parents=True, exist_ok=True)
    cli_version = subprocess.run(["codex", "--version"], capture_output=True, text=True, check=False)
    records: list[dict[str, Any]] = []
    existing = output_root / "results.json"
    if existing.exists():
        try:
            loaded = json.loads(existing.read_text(encoding="utf-8"))
            records = loaded.get("records", []) if isinstance(loaded, dict) else []
        except (OSError, json.JSONDecodeError):
            records = []
    for model in args.models:
        for condition in args.conditions:
            if not _case_selected(model, condition, args.filters):
                continue
            for repeat in range(1, args.repeats + 1):
                record = run_case(model, condition, repeat, output_root, args.timeout, args.root.resolve())
                records = [r for r in records if not (r.get("model") == model and r.get("condition") == condition and r.get("repeat") == repeat)]
                records.append(record)
                _atomic_write_json(output_root / "results.json", {"cli_version": cli_version.stdout.strip() or None, "records": records, "paired_savings": paired_savings(records)})
                _atomic_write_text(output_root / "summary.md", markdown_summary(records))
    selected = [
        record
        for record in records
        if _case_selected(str(record.get("model")), str(record.get("condition")), args.filters)
    ]
    return 0 if selected and all(record.get("status") == "success" for record in selected) else 1


if __name__ == "__main__":
    raise SystemExit(main())
