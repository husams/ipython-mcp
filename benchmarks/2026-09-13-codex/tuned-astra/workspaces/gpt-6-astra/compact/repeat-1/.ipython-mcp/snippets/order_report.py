import csv
import re


def _positive_integer(value):
    if type(value) is int:
        return value if value > 0 else None
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not re.fullmatch(r"[+-]?[0-9]+", value):
        return None
    try:
        number = int(value)
    except ValueError:
        return None
    return number if number > 0 else None


def summarize_orders(rows):
    """Count valid orders and aggregate integer revenue by trimmed region."""
    valid_rows = invalid_rows = total_cents = 0
    by_region = {}
    for row in rows:
        if not isinstance(row, dict):
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
    """Parse an orders CSV and delegate to the in-memory summarizer."""
    with open(path, newline="", encoding="utf-8-sig") as source:
        return summarize_orders(csv.DictReader(source))
