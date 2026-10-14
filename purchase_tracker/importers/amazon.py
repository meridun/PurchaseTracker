"""Import from an Amazon "Your Orders" data export (Retail.OrderHistory.*.csv).

Request the export at Account -> Privacy Central -> Request My Data -> Your Orders.
Column names vary by export version; the mapping below is tolerant of the common
variants and can be extended.
"""
from __future__ import annotations

import csv
import sqlite3
from datetime import datetime
from pathlib import Path

from ..models import Purchase

# Candidate header names for each field we care about, in priority order.
COLUMNS = {
    "order_id": ["Order ID", "Order Id", "order_id"],
    "order_date": ["Order Date", "Ship Date", "order_date"],
    "item_name": ["Product Name", "Title", "Item Name"],
    "quantity": ["Quantity", "Qty"],
    "unit_price": ["Unit Price", "Purchase Price Per Unit", "Item Price"],
    "total": ["Total Owed", "Item Total", "Item Subtotal", "Total"],
    "currency": ["Currency", "Unit Price Currency"],
}


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


def import_csv(conn: sqlite3.Connection, path: Path) -> tuple[int, int]:
    """Returns (inserted, skipped_as_duplicate)."""
    inserted = skipped = 0
    with path.open(newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            qty = int(float(_pick(row, COLUMNS["quantity"]) or 1))
            p = Purchase(
                vendor="amazon",
                order_id=_pick(row, COLUMNS["order_id"]),
                order_date=_date(_pick(row, COLUMNS["order_date"])),
                item_name=_pick(row, COLUMNS["item_name"]) or "(unknown item)",
                quantity=qty,
                unit_price=_money(_pick(row, COLUMNS["unit_price"])),
                total=_money(_pick(row, COLUMNS["total"])),
                currency=_pick(row, COLUMNS["currency"]) or "USD",
                source="amazon-csv",
            )
            if p.insert(conn):
                inserted += 1
            else:
                skipped += 1
    conn.commit()
    return inserted, skipped
