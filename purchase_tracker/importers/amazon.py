"""Import from an Amazon "Your Orders" data export (Retail.OrderHistory.*.csv).

Request the export at Account -> Privacy Central -> Request My Data -> Your Orders.
One CSV row is one line item; rows sharing an Order ID become one order. Column
names vary by export version; the mapping below is tolerant of the common variants.
"""
from __future__ import annotations

import csv
import hashlib
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from ..models import Item, Order

# Candidate header names for each field we care about, in priority order.
COLUMNS = {
    "order_id": ["Order ID", "Order Id", "order_id"],
    "order_date": ["Order Date", "Ship Date", "order_date"],
    "item_name": ["Product Name", "Title", "Item Name"],
    "sku": ["ASIN", "ASIN/ISBN", "SKU"],
    "quantity": ["Quantity", "Qty"],
    "unit_price": ["Unit Price", "Purchase Price Per Unit", "Item Price"],
    "total": ["Total Owed", "Item Total", "Item Subtotal", "Total"],
    "currency": ["Currency", "Unit Price Currency"],
}


@dataclass
class ImportResult:
    orders_new: int = 0
    orders_seen: int = 0
    items_new: int = 0
    items_skipped: int = 0


def _pick(row: dict, keys: list[str]) -> str | None:
    for k in keys:
        if k in row and row[k] not in (None, "", "Not Available"):
            return row[k].strip()
    return None


def _money(s: str | None) -> float | None:
    if not s:
        return None
    return float(s.replace("$", "").replace(",", "").strip())


def _date(s: str | None) -> str:
    if not s:
        raise ValueError("missing order date")
    for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y"):
        try:
            return datetime.strptime(s[:20] if "T" in s else s, fmt).date().isoformat()
        except ValueError:
            continue
    raise ValueError(f"unrecognised date: {s!r}")


def _line_key(sku: str | None, name: str, qty: int, total: float | None, seq: int) -> str:
    """Stable identity for a line within an order. Prefer ASIN; fall back to a content hash.

    ``seq`` disambiguates genuinely repeated lines (same ASIN twice on one order).
    """
    base = sku or hashlib.sha1(f"{name}|{qty}|{total}".encode()).hexdigest()[:12]
    return f"{base}#{seq}" if seq else base


def import_csv(conn: sqlite3.Connection, path: Path) -> ImportResult:
    res = ImportResult()
    seen_keys: dict[tuple[int, str], int] = {}  # (order_id, base key) -> repeat count
    with path.open(newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            order = Order(
                vendor="amazon",
                order_ref=_pick(row, COLUMNS["order_id"]),
                order_date=_date(_pick(row, COLUMNS["order_date"])),
                currency=_pick(row, COLUMNS["currency"]) or "USD",
                source="amazon-csv",
            )
            oid, created = order.get_or_insert(conn)
            if created:
                res.orders_new += 1
            else:
                res.orders_seen += 1

            name = _pick(row, COLUMNS["item_name"]) or "(unknown item)"
            sku = _pick(row, COLUMNS["sku"])
            qty = int(float(_pick(row, COLUMNS["quantity"]) or 1))
            total = _money(_pick(row, COLUMNS["total"]))
            base = _line_key(sku, name, qty, total, 0)
            seq = seen_keys.get((oid, base), 0)
            seen_keys[(oid, base)] = seq + 1
            item = Item(
                order_id=oid,
                line_key=_line_key(sku, name, qty, total, seq),
                name=name,
                sku=sku,
                quantity=qty,
                unit_price=_money(_pick(row, COLUMNS["unit_price"])),
                total=total,
            )
            if item.insert(conn) is not None:
                res.items_new += 1
            else:
                res.items_skipped += 1

    # Order total = sum of its lines (Amazon's export has no per-order total column).
    conn.execute(
        "UPDATE orders SET total = (SELECT SUM(total) FROM items WHERE items.order_id = orders.id)"
        " WHERE source = 'amazon-csv'"
    )
    conn.commit()
    return res
