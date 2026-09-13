import csv


def _positive_integer(value):
    if isinstance(value, bool):
        return None
    try:
        text = str(value).strip()
        if not text or (text.startswith("+") and not text[1:].isdigit()) or (
            not text.startswith("+") and not text.isdigit()
        ):
            return None
        number = int(text)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def summarize_orders(rows):
    valid_rows = 0
    invalid_rows = 0
    total_cents = 0
    by_region = {}

    for row in rows:
        region = row.get("region")
        units = _positive_integer(row.get("units"))
        unit_price_cents = _positive_integer(row.get("unit_price_cents"))
        if not isinstance(region, str) or not region.strip() or units is None or unit_price_cents is None:
            invalid_rows += 1
            continue
        amount = units * unit_price_cents
        valid_rows += 1
        total_cents += amount
        by_region[region.strip()] = by_region.get(region.strip(), 0) + amount

    return {
        "valid_rows": valid_rows,
        "invalid_rows": invalid_rows,
        "total_cents": total_cents,
        "by_region": by_region,
    }


def report_orders(path):
    with open(path, newline="", encoding="utf-8") as handle:
        rows = csv.DictReader(handle)
        return summarize_orders(rows)
