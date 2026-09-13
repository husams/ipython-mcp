"""Helpers for validating and summarizing order rows."""

import csv
import re


_INTEGER_RE = re.compile(r"^[0-9]+$")


def _positive_integer(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    if isinstance(value, str):
        value = value.strip()
        if _INTEGER_RE.fullmatch(value):
            parsed = int(value)
            return parsed if parsed > 0 else None
    return None


def summarize_orders(rows):
    """Return validation counts and regional order totals for *rows*."""
    valid_rows = 0
    invalid_rows = 0
    total_cents = 0
    by_region = {}

    for row in rows:
        region = str(row.get("region", "")).strip()
        units = _positive_integer(row.get("units"))
        unit_price_cents = _positive_integer(row.get("unit_price_cents"))
        if not region or units is None or unit_price_cents is None:
            invalid_rows += 1
            continue

        amount = units * unit_price_cents
        valid_rows += 1
        total_cents += amount
        by_region[region] = by_region.get(region, 0) + amount

    return {
        "valid_rows": valid_rows,
        "invalid_rows": invalid_rows,
        "total_cents": total_cents,
        "by_region": by_region,
    }


def report_orders(path):
    """Parse *path* as a CSV file and summarize its order rows."""
    with open(path, newline="") as handle:
        rows = csv.DictReader(handle)
        return summarize_orders(rows)
