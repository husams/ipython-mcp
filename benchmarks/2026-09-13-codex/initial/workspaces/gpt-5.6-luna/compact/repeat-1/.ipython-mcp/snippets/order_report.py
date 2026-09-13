import csv


def _positive_integer(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        number = value
    elif isinstance(value, str):
        text = value.strip()
        if not text.isdecimal():
            return None
        number = int(text)
    else:
        return None
    return number if number > 0 else None


def summarize_orders(rows):
    valid_rows = 0
    invalid_rows = 0
    total_cents = 0
    by_region = {}

    for row in rows:
        region = row.get("region") if hasattr(row, "get") else None
        region = region.strip() if isinstance(region, str) else region
        units = _positive_integer(row.get("units") if hasattr(row, "get") else None)
        unit_price_cents = _positive_integer(
            row.get("unit_price_cents") if hasattr(row, "get") else None
        )
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
    with open(path, newline="") as source:
        rows = list(csv.DictReader(source))
    return summarize_orders(rows)
