"""Validate and aggregate orders from live rows or a CSV file."""
import csv
import re
from collections.abc import Mapping


def _positive_integer(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, str):
        value = value.strip()
        if not re.fullmatch(r"[+-]?[0-9]+", value):
            return None
        try:
            value = int(value)
        except ValueError:
            return None
    if not isinstance(value, int) or value <= 0:
        return None
    return value


def summarize_orders(rows):
    """Count valid/invalid rows and sum valid order cents by trimmed region."""
    result = {"valid_rows": 0, "invalid_rows": 0, "total_cents": 0, "by_region": {}}
    for row in rows:
        if not isinstance(row, Mapping):
            result["invalid_rows"] += 1
            continue
        region = row.get("region")
        region = region.strip() if isinstance(region, str) else ""
        units = _positive_integer(row.get("units"))
        price = _positive_integer(row.get("unit_price_cents"))
        if not region or units is None or price is None:
            result["invalid_rows"] += 1
            continue
        cents = units * price
        result["valid_rows"] += 1
        result["total_cents"] += cents
        result["by_region"][region] = result["by_region"].get(region, 0) + cents
    return result


def report_orders(path):
    """Parse a CSV path and delegate validation and aggregation."""
    with open(path, newline="", encoding="utf-8-sig") as source:
        return summarize_orders(csv.DictReader(source))
