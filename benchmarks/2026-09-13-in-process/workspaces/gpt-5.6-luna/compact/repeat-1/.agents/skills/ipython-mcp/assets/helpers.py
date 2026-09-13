"""Small, JSON-compatible helpers suitable for a persistent IPython session."""

from __future__ import annotations

import csv
import math
from collections import defaultdict
from pathlib import Path
from typing import Any


def csv_summary(
    path: str | Path,
    numeric_column: str,
    group_by: str | None = None,
    sample_limit: int = 5,
    group_limit: int = 20,
) -> dict[str, Any]:
    """Return bounded finite row counts, sums, and a sample from a CSV file.

    At most ``group_limit`` real groups are retained. Additional groups are
    combined into one final entry marked ``overflow=True`` with ``key=None``;
    this cannot collide with a real group named ``<other>`` or any other key.
    """

    if sample_limit < 0:
        raise ValueError("sample_limit must be non-negative")
    if group_limit < 1:
        raise ValueError("group_limit must be positive")

    counts: dict[str, int] = defaultdict(int)
    sums: dict[str, float] = defaultdict(float)
    sample: list[dict[str, str]] = []
    rows = valid_numeric_rows = invalid_numeric_rows = aggregate_overflow_rows = 0
    overflow_count = 0
    overflow_sum = 0.0

    with Path(path).open(newline="", encoding="utf-8") as stream:
        reader = csv.DictReader(stream)
        fieldnames = reader.fieldnames or []
        if numeric_column not in fieldnames:
            raise ValueError(f"missing numeric column: {numeric_column}")
        if group_by is not None and group_by not in fieldnames:
            raise ValueError(f"missing grouping column: {group_by}")

        for row in reader:
            rows += 1
            if len(sample) < sample_limit:
                sample.append(dict(row))
            try:
                value = float(row.get(numeric_column, ""))
            except (TypeError, ValueError):
                invalid_numeric_rows += 1
                continue
            if not math.isfinite(value):
                invalid_numeric_rows += 1
                continue

            valid_numeric_rows += 1
            key = row.get(group_by, "") if group_by is not None else "<all>"
            if key not in counts and len(counts) >= group_limit:
                next_sum = overflow_sum + value
                if not math.isfinite(next_sum):
                    aggregate_overflow_rows += 1
                    continue
                overflow_count += 1
                overflow_sum = next_sum
                continue

            next_sum = sums[key] + value
            if not math.isfinite(next_sum):
                aggregate_overflow_rows += 1
                continue
            counts[key] += 1
            sums[key] = next_sum

    groups = [
        {"key": key, "count": counts[key], "sum": sums[key], "overflow": False}
        for key in counts
    ]
    if overflow_count:
        groups.append(
            {
                "key": None,
                "count": overflow_count,
                "sum": overflow_sum,
                "overflow": True,
            }
        )

    return {
        "rows": rows,
        "valid_numeric_rows": valid_numeric_rows,
        "invalid_numeric_rows": invalid_numeric_rows,
        "aggregate_overflow_rows": aggregate_overflow_rows,
        "groups": groups,
        "sample": sample,
        "sample_truncated": rows > len(sample),
    }
