import importlib.util
import json
import sys
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "codex_benchmark", Path(__file__).parents[1] / "scripts" / "codex_benchmark.py"
)
assert _SPEC is not None and _SPEC.loader is not None
benchmark = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(benchmark)
sys.modules["codex_benchmark"] = benchmark

from codex_benchmark import (  # noqa: E402  # deliberate path bootstrap for script tests
    INITIAL_ROWS,
    REUSE_ROWS,
    codex_command,
    create_fixtures,
    deduplicate_events,
    parse_event_lines,
    parse_mcp_result,
    parse_usage,
    run_oracle,
)


def test_parse_usage_calculates_total_and_uncached_input() -> None:
    usage = parse_usage(
        {
            "input_tokens": 100,
            "cached_input_tokens": 40,
            "cache_write_input_tokens": 3,
            "output_tokens": 25,
            "reasoning_output_tokens": 7,
        }
    )

    assert usage is not None
    assert usage.total_tokens == 125
    assert usage.uncached_input_tokens == 60


def test_parse_usage_missing_field_is_unavailable() -> None:
    assert parse_usage({"input_tokens": 1}) is None
    assert parse_usage(None) is None


def test_parse_mcp_result_accepts_structured_and_text_content() -> None:
    assert parse_mcp_result({"result": {"structured_content": {"ok": True}}}) == {"ok": True}
    assert parse_mcp_result({"result": {"content": [{"type": "text", "text": '{"ok": true}'}]}}) == {"ok": True}


def test_dedup_keeps_started_and_completed_but_drops_replayed_completion() -> None:
    events = [
        {"type": "item.started", "item": {"id": "x", "type": "mcp_tool_call"}},
        {"type": "item.completed", "item": {"id": "x", "type": "mcp_tool_call", "tool": "execute"}},
        {"type": "item.completed", "item": {"id": "x", "type": "mcp_tool_call", "tool": "execute"}},
    ]

    result = deduplicate_events(events)
    assert [event["type"] for event in result] == ["item.started", "item.completed"]


def test_parser_counts_only_completed_mcp_calls_and_reads_actual_usage() -> None:
    lines = [
        json.dumps({"type": "item.started", "item": {"id": "x", "type": "mcp_tool_call", "tool": "execute"}}),
        json.dumps({"type": "item.completed", "item": {"id": "x", "type": "mcp_tool_call", "tool": "execute"}}),
        json.dumps({"type": "item.completed", "item": {"id": "y", "type": "agent_message", "text": "done"}}),
        json.dumps({"type": "turn.completed", "usage": {"input_tokens": 10, "cached_input_tokens": 2, "cache_write_input_tokens": 0, "output_tokens": 4, "reasoning_output_tokens": 1}}),
    ]

    result = parse_event_lines(lines)
    assert result.mcp_tool_calls == 1
    assert result.mcp_tool_names == ["execute"]
    assert result.last_message == "done"
    assert result.usage is not None
    assert result.usage.total_tokens == 14
    assert result.errors == []


def test_parser_reports_malformed_and_missing_completion_usage() -> None:
    result = parse_event_lines(["not json", json.dumps({"type": "turn.completed", "usage": {}})])

    assert result.usage is None
    assert any("invalid JSON" in error for error in result.errors)
    assert any("usage unavailable" in error for error in result.errors)


def test_oracle_validates_helper_outputs_and_unchanged_hash(tmp_path: Path) -> None:
    _, _, initial_expected, reuse_expected = create_fixtures(tmp_path)
    helper = tmp_path / ".ipython-mcp" / "snippets" / "order_report.py"
    helper.parent.mkdir(parents=True)
    helper.write_text(
        """import csv\ndef summarize_orders(rows):\n    out = {'valid_rows': 0, 'invalid_rows': 0, 'total_cents': 0, 'by_region': {}}\n    for row in rows:\n        try:\n            region = row['region'].strip(); units = int(row['units']); price = int(row['unit_price_cents'])\n            if not region or units <= 0 or price <= 0: raise ValueError\n        except (KeyError, TypeError, ValueError):\n            out['invalid_rows'] += 1; continue\n        cents = units * price; out['valid_rows'] += 1; out['total_cents'] += cents\n        out['by_region'][region] = out['by_region'].get(region, 0) + cents\n    out['by_region'] = dict(sorted(out['by_region'].items())); return out\ndef report_orders(path):\n    with open(path, newline='', encoding='utf-8') as stream:\n        return summarize_orders(list(csv.DictReader(stream)))\n""",
        encoding="utf-8",
    )
    (tmp_path / "result.json").write_text(json.dumps({"initial_summary": {"rows": 8, "valid_rows": 5, "invalid_rows": 3}, "report": initial_expected}), encoding="utf-8")
    (tmp_path / "reuse.json").write_text(json.dumps(reuse_expected), encoding="utf-8")

    result = run_oracle(tmp_path, initial_expected, reuse_expected)
    assert result["ok"] is True
    assert result["unchanged_sha256"] is True


def test_oracle_reports_missing_artifacts(tmp_path: Path) -> None:
    _, _, initial_expected, reuse_expected = create_fixtures(tmp_path)

    result = run_oracle(tmp_path, initial_expected, reuse_expected)
    assert result["ok"] is False
    assert any("helper module missing" in error for error in result["errors"])


def test_fixtures_are_deterministic_and_commands_bound_profiles(tmp_path: Path) -> None:
    first = create_fixtures(tmp_path / "one")
    second = create_fixtures(tmp_path / "two")
    assert first[2:] == second[2:]
    assert (tmp_path / "one/orders_initial.csv").read_text() == (tmp_path / "two/orders_initial.csv").read_text()
    assert len(INITIAL_ROWS) == 8
    assert len(REUSE_ROWS) == 5

    full = codex_command("gpt-test", tmp_path / "full", "full", 240, Path("/repo"))
    compact = codex_command("gpt-test", tmp_path / "compact", "compact", 240, Path("/repo"))
    assert full[:2] == ["codex", "exec"]
    assert "--ignore-user-config" in full
    assert any("IPYTHON_MCP_PROFILE=\"full\"" in value for value in full)
    assert any("IPYTHON_MCP_PROFILE=\"compact\"" in value for value in compact)
    assert any("skills.max_context_tokens=1" in value for value in full)
    assert any("skills.max_context_tokens=1" in value for value in compact)
