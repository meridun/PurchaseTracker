"""Amazon importers.

Three inputs:

* ``import_csv``  - the Privacy Central "Your Orders" export (Retail.OrderHistory.*.csv).
* ``import_json`` - output of ``scripts/amazon_extract.js``: orders with line items, HSA
  flags, ship-to, payment method, and optionally the invoice HTML, which is saved to the
  data directory and referenced from ``orders.document_ref``.
* ``import_transactions_json`` - output of ``scripts/amazon_transactions.js``: the
  vendor-side list of card charges per order, stored in ``vendor_charges``.

``import_file`` dispatches on extension and on the JSON ``kind`` wrapper.
"""
from __future__ import annotations

import csv
import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from ..models import Item, Order

# Candidate header names for each CSV field, in priority order.
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
    kind: str = "orders"
    orders_new: int = 0
    orders_seen: int = 0
    items_new: int = 0
    items_skipped: int = 0
    documents_saved: int = 0
    charges_new: int = 0
    charges_updated: int = 0

    def summary(self) -> str:
        if self.kind == "transactions":
            return f"charges: {self.charges_new} new, {self.charges_updated} already present"
        s = (f"orders: {self.orders_new} new, {self.orders_seen} already present; "
             f"items: {self.items_new} new, {self.items_skipped} already present")
        if self.documents_saved:
            s += f"; invoices saved: {self.documents_saved}"
        return s


# --- helpers ------------------------------------------------------------------

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


def _line_key(sku: str | None, name: str, qty: int, total: float | None, seq: int) -> str:
    """Stable identity for a line within an order. Prefer ASIN; fall back to a content hash.

    ``seq`` disambiguates genuinely repeated lines (same ASIN twice on one order).
    """
    base = sku or hashlib.sha1(f"{name}|{qty}|{total}".encode()).hexdigest()[:12]
    return f"{base}#{seq}" if seq else base


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
        cents = round(p / base * total * 100)
        out.append(cents / 100)
        acc += cents
    drift = round(total * 100) - acc
    if drift:
        last = max(i for i, p in enumerate(parts) if p is not None)
        out[last] = round(out[last] + drift / 100, 2)  # type: ignore[operator]
    return out


# --- CSV (Privacy Central export) ---------------------------------------------

def import_csv(conn: sqlite3.Connection, path: Path) -> ImportResult:
    res = ImportResult()
    seen_keys: dict[tuple[int, str], int] = {}
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
            res.orders_new += created
            res.orders_seen += not created

            name = _pick(row, COLUMNS["item_name"]) or "(unknown item)"
            sku = _pick(row, COLUMNS["sku"])
            qty = int(float(_pick(row, COLUMNS["quantity"]) or 1))
            total = _money(_pick(row, COLUMNS["total"]))
            base = _line_key(sku, name, qty, total, 0)
            seq = seen_keys.get((oid, base), 0)
            seen_keys[(oid, base)] = seq + 1
            item = Item(
                order_id=oid, line_key=_line_key(sku, name, qty, total, seq), name=name, sku=sku,
                quantity=qty, unit_price=_money(_pick(row, COLUMNS["unit_price"])), total=total,
            )
            if item.insert(conn) is not None:
                res.items_new += 1
            else:
                res.items_skipped += 1
    conn.execute(
        "UPDATE orders SET total = (SELECT SUM(total) FROM items WHERE items.order_id = orders.id)"
        " WHERE source = 'amazon-csv' AND total IS NULL"
    )
    conn.commit()
    return res


# --- JSON (browser extraction) ------------------------------------------------

def _unwrap(path: Path) -> tuple[str, list]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, dict) and "data" in data:
        return data.get("kind", "amazon-orders"), data["data"]
    return "amazon-orders", data


def _safe_name(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", s)


def import_json(conn: sqlite3.Connection, path: Path, docs_dir: Path | None = None) -> ImportResult:
    """Import orders extracted by ``scripts/amazon_extract.js``.

    Per item: ``unit_price`` is the pre-tax unit price; ``total`` is the item's share of the
    grand total (tax and shipping apportioned by line price) so items sum to what was charged.
    If an order carries ``invoice_html`` and ``docs_dir`` is given, the invoice is written to
    ``docs_dir/amazon/<order_ref>.html`` and ``orders.document_ref`` points at it.
    """
    kind, orders = _unwrap(path)
    if kind != "amazon-orders":
        raise ValueError(f"{path.name} is {kind!r}, not an orders file")
    res = ImportResult()
    for o in orders:
        doc_ref = None
        html = o.get("invoice_html")
        if html and docs_dir is not None:
            rel = Path("amazon") / f"{_safe_name(o['order_ref'])}.html"
            target = docs_dir / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            if not target.exists():
                target.write_text(html, encoding="utf-8")
                res.documents_saved += 1
            doc_ref = str(rel).replace("\\", "/")

        order = Order(
            vendor="amazon", order_ref=o["order_ref"], order_date=_date(o["order_date"]),
            total=o.get("grand_total"), source="amazon-invoice", document_ref=doc_ref,
            ship_to=o.get("ship_to"), payment_method=o.get("payment_method"),
            subtotal=o.get("subtotal"), shipping=o.get("shipping"), tax=o.get("tax"),
            hsa_eligible_total=o.get("hsa_eligible_total"),
        )
        oid, created = order.get_or_insert(conn)
        res.orders_new += created
        res.orders_seen += not created

        items = o.get("items", [])
        totals = _apportion(o.get("grand_total"), [i.get("line_price") for i in items])
        seen: dict[str, int] = {}
        for it, total in zip(items, totals):
            name = it.get("title") or "(unknown item)"
            qty = int(it.get("quantity") or 1)
            base = _line_key(it.get("asin"), name, qty, it.get("line_price"), 0)
            seq = seen.get(base, 0)
            seen[base] = seq + 1
            item = Item(
                order_id=oid, line_key=_line_key(it.get("asin"), name, qty, it.get("line_price"), seq),
                name=name, sku=it.get("asin"), quantity=qty, unit_price=it.get("unit_price"),
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


# --- JSON (browser extraction of the Transactions page) -----------------------

_LAST4 = re.compile(r"(\d{4})\s*$")


def import_transactions_json(conn: sqlite3.Connection, path: Path) -> ImportResult:
    """Store Amazon's per-order card charges in ``vendor_charges``.

    Keyed on (order_ref, amount, instrument, merchant) with a per-file sequence, not on
    date, so a charge seen as *pending* and later as *completed* updates in place.
    """
    kind, rows = _unwrap(path)
    if kind != "amazon-transactions":
        raise ValueError(f"{path.name} is {kind!r}, not a transactions file")
    res = ImportResult(kind="transactions")
    seen: dict[str, int] = {}
    for r in rows:
        order_ref = r.get("order_ref")
        if not order_ref:
            continue
        amount = round(float(r["amount"]), 2)
        instrument = r.get("instrument")
        merchant = r.get("merchant")
        base = f"amazon|{order_ref}|{amount:.2f}|{instrument or ''}|{merchant or ''}"
        seq = seen.get(base, 0)
        seen[base] = seq + 1
        ext = f"{base}#{seq}" if seq else base
        status = "pending" if str(r.get("status", "")).lower().startswith("pend") else "completed"
        m = _LAST4.search(instrument or "")
        date = _date(r["date"])
        existing = conn.execute("SELECT id FROM vendor_charges WHERE external_id = ?", (ext,)).fetchone()
        if existing:
            conn.execute("UPDATE vendor_charges SET charge_date = ?, status = ? WHERE id = ?",
                         (date, status, existing["id"]))
            res.charges_updated += 1
        else:
            conn.execute(
                "INSERT INTO vendor_charges (vendor, order_ref, charge_date, amount, instrument, last4,"
                " merchant, status, external_id) VALUES ('amazon',?,?,?,?,?,?,?,?)",
                (order_ref, date, amount, instrument, m.group(1) if m else None, merchant, status, ext),
            )
            res.charges_new += 1
    conn.commit()
    return res


def import_file(conn: sqlite3.Connection, path: Path, docs_dir: Path | None = None) -> ImportResult:
    if path.suffix.lower() == ".csv":
        return import_csv(conn, path)
    kind, _ = _unwrap(path)
    if kind == "amazon-transactions":
        return import_transactions_json(conn, path)
    return import_json(conn, path, docs_dir)
