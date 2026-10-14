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
    for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%B %d, %Y"):
        try:
            return datetime.strptime(s[:20] if "T" in s else s, fmt).date().isoformat()
        except ValueError:
            continue
    raise ValueError(f"unrecognised date: {s!r}")


def _apportion(total: float | None, parts: list[float | None]) -> list[float | None]:
    """Spread ``total`` across ``parts`` in proportion, in cents, so the pieces sum exactly.

    Used to fold order-level tax/shipping into per-item totals. Unknown parts stay None.
    """
    known = [p for p in parts if p is not None]
    if total is None or not known or sum(known) <= 0:
        return parts
    base = sum(known)
    out: list[float | None] = []
    acc = 0
    for p in parts:
        if p is None:
            out.append(None)
            continue
        acc_cents = round(p / base * total * 100)
        out.append(acc_cents / 100)
        acc += acc_cents
    # push rounding drift onto the last known item
    drift = round(total * 100) - acc
    if drift:
        last = max(i for i, p in enumerate(parts) if p is not None)
        out[last] = round(out[last] + drift / 100, 2)  # type: ignore[operator]
    return out


def import_json(conn: sqlite3.Connection, path: Path) -> ImportResult:
    """Import orders extracted by ``scripts/amazon_extract.js`` (printable-invoice data).

    Per item: ``unit_price`` is the pre-tax unit price; ``total`` is the item's share of the
    grand total (tax and shipping apportioned by line price) so items sum to what was charged.
    """
    import json

    res = ImportResult()
    orders = json.loads(path.read_text(encoding="utf-8"))
    for o in orders:
        order = Order(
            vendor="amazon",
            order_ref=o["order_ref"],
            order_date=_date(o["order_date"]),
            total=o.get("grand_total"),
            source="amazon-invoice",
            ship_to=o.get("ship_to"),
            payment_method=o.get("payment_method"),
            subtotal=o.get("subtotal"),
            shipping=o.get("shipping"),
            tax=o.get("tax"),
            hsa_eligible_total=o.get("hsa_eligible_total"),
        )
        oid, created = order.get_or_insert(conn)
        res.orders_new += created
        res.orders_seen += not created

        items = o.get("items", [])
        line_prices = [i.get("line_price") for i in items]
        totals = _apportion(o.get("grand_total"), line_prices)
        seen: dict[str, int] = {}
        for it, total in zip(items, totals):
            name = it.get("title") or "(unknown item)"
            qty = int(it.get("quantity") or 1)
            base = _line_key(it.get("asin"), name, qty, it.get("line_price"), 0)
            seq = seen.get(base, 0)
            seen[base] = seq + 1
            item = Item(
                order_id=oid,
                line_key=_line_key(it.get("asin"), name, qty, it.get("line_price"), seq),
                name=name,
                sku=it.get("asin"),
                quantity=qty,
                unit_price=it.get("unit_price"),
                total=total,
                hsa_eligible=None if it.get("hsa_eligible") is None else int(bool(it["hsa_eligible"])),
                notes=f"Sold by: {it['sold_by']}" if it.get("sold_by") else None,
            )
            if item.insert(conn) is not None:
                res.items_new += 1
            else:
                res.items_skipped += 1
    conn.commit()
    return res


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
