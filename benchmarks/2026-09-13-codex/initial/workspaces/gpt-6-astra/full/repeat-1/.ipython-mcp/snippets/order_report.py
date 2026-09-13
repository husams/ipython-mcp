"""Validate order rows and aggregate revenue in cents by region."""
import csv
import re
from collections.abc import Mapping
from pathlib import Path


def _positive_integer(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    if not isinstance(value, str):
        return None
    value = value.strip()
    if re.fullmatch(r"[+]?[0-9]+", value) is None:
        return None
    try:
        number = int(value)
    except ValueError:
        return None
    return number if number > 0 else None


def summarize_orders(rows):
    """Count valid/invalid rows and sum units * unit_price_cents by region."""
    valid_rows = invalid_rows = total_cents = 0
    by_region = {}
    for row in rows:
        if not isinstance(row, Mapping):
            invalid_rows += 1
            continue
        region = row.get("region")
        region = region.strip() if isinstance(region, str) else ""
        units = _positive_integer(row.get("units"))
        price = _positive_integer(row.get("unit_price_cents"))
        if not region or units is None or price is None:
            invalid_rows += 1
            continue
        cents = units * price
        valid_rows += 1
        total_cents += cents
        by_region[region] = by_region.get(region, 0) + cents
    return {"valid_rows": valid_rows, "invalid_rows": invalid_rows,
            "total_cents": total_cents, "by_region": by_region}


def report_orders(path):
    """Parse a CSV path and delegate its rows to summarize_orders."""
    with Path(path).open(newline="", encoding="utf-8-sig") as source:
        return summarize_orders(csv.DictReader(source))
