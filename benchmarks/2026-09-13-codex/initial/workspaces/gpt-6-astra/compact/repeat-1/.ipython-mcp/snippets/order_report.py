import csv
import re
from collections.abc import Mapping


def _positive_integer(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    if isinstance(value, str) and re.fullmatch(r"[+-]?[0-9]+", value.strip()):
        try:
            number = int(value.strip())
        except ValueError:
            return None
        return number if number > 0 else None
    return None


def summarize_orders(rows):
    """Count valid orders and aggregate their value in cents by region."""
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
        amount = units * price
        valid_rows += 1
        total_cents += amount
        by_region[region] = by_region.get(region, 0) + amount
    return {"valid_rows": valid_rows, "invalid_rows": invalid_rows,
            "total_cents": total_cents, "by_region": by_region}


def report_orders(path):
    """Parse an orders CSV and summarize its rows."""
    with open(path, newline="") as source:
        return summarize_orders(csv.DictReader(source))
