"""Dataclasses mirroring the tables, plus the queries the app needs.

Each dataclass has ``insert(conn)`` which returns the new row id, or ``None`` if a
uniqueness constraint meant the row already existed (``INSERT OR IGNORE``).
"""
from __future__ import annotations

import sqlite3
from dataclasses import asdict, dataclass, field, fields
from typing import ClassVar


class _Row:
    __table__: ClassVar[str]

    def insert(self, conn: sqlite3.Connection) -> int | None:
        d = asdict(self)
        d.pop("id", None)
        d = {k: v for k, v in d.items() if v is not None}  # let DB defaults apply
        cols = ", ".join(d)
        marks = ", ".join("?" for _ in d)
        cur = conn.execute(
            f"INSERT OR IGNORE INTO {self.__table__} ({cols}) VALUES ({marks})", tuple(d.values())
        )
        if cur.rowcount != 1:
            return None
        self.id = cur.lastrowid  # type: ignore[attr-defined]
        return self.id

    @classmethod
    def from_row(cls, row: sqlite3.Row):
        names = {f.name for f in fields(cls)}
        return cls(**{k: row[k] for k in row.keys() if k in names})


@dataclass
class Party(_Row):
    __table__ = "parties"
    name: str
    kind: str = "person"  # self | person | hsa | other
    notes: str | None = None
    id: int | None = None


@dataclass
class Account(_Row):
    __table__ = "accounts"
    name: str
    party_id: int
    kind: str = "other"  # checking | savings | credit | hsa | cash | other
    institution: str | None = None
    last4: str | None = None
    notes: str | None = None
    id: int | None = None


@dataclass
class Order(_Row):
    __table__ = "orders"
    vendor: str
    order_date: str
    order_ref: str | None = None
    total: float | None = None
    currency: str = "USD"
    source: str = "manual"
    document_ref: str | None = None
    notes: str | None = None
    id: int | None = None

    def get_or_insert(self, conn: sqlite3.Connection) -> tuple[int, bool]:
        """Insert, or look up the existing order with the same (vendor, order_ref)."""
        new_id = self.insert(conn)
        if new_id is not None:
            return new_id, True
        row = conn.execute(
            "SELECT id FROM orders WHERE vendor = ? AND order_ref = ?", (self.vendor, self.order_ref)
        ).fetchone()
        self.id = row["id"]
        return self.id, False


@dataclass
class Item(_Row):
    __table__ = "items"
    order_id: int
    name: str
    line_key: str
    sku: str | None = None
    quantity: int = 1
    unit_price: float | None = None
    total: float | None = None
    category: str | None = None
    notes: str | None = None
    id: int | None = None


@dataclass
class Transaction(_Row):
    __table__ = "transactions"
    account_id: int
    posted_date: str
    amount: float
    external_id: str
    description: str | None = None
    source: str = "manual"
    notes: str | None = None
    id: int | None = None


@dataclass
class Claim(_Row):
    __table__ = "claims"
    party_id: int
    title: str | None = None
    status: str = "draft"  # draft | submitted | paid | rejected
    submitted_at: str | None = None
    paid_at: str | None = None
    paid_amount: float | None = None
    paid_transaction_id: int | None = None
    document_ref: str | None = None
    notes: str | None = None
    id: int | None = None


@dataclass
class Allocation(_Row):
    __table__ = "allocations"
    item_id: int
    party_id: int
    amount: float
    claim_id: int | None = None
    notes: str | None = None
    id: int | None = None


# --- queries ---------------------------------------------------------------

def get_party(conn: sqlite3.Connection, name: str) -> Party | None:
    row = conn.execute("SELECT * FROM parties WHERE name = ? COLLATE NOCASE", (name,)).fetchone()
    return Party.from_row(row) if row else None


def list_parties(conn: sqlite3.Connection) -> list[Party]:
    return [Party.from_row(r) for r in conn.execute("SELECT * FROM parties ORDER BY id")]


def list_orders(conn: sqlite3.Connection, vendor: str | None = None, limit: int = 50) -> list[sqlite3.Row]:
    """Orders newest first, with item count. Columns: orders.* + n_items."""
    sql = (
        "SELECT o.*, COUNT(i.id) AS n_items FROM orders o LEFT JOIN items i ON i.order_id = o.id"
    )
    args: list = []
    if vendor:
        sql += " WHERE o.vendor = ?"
        args.append(vendor)
    sql += " GROUP BY o.id ORDER BY o.order_date DESC, o.id DESC LIMIT ?"
    args.append(limit)
    return conn.execute(sql, args).fetchall()


def items_for_order(conn: sqlite3.Connection, order_id: int) -> list[sqlite3.Row]:
    """Items with their allocation totals (from v_item_allocations)."""
    return conn.execute(
        "SELECT i.*, v.allocated, v.unallocated FROM items i"
        " JOIN v_item_allocations v ON v.item_id = i.id WHERE i.order_id = ? ORDER BY i.id",
        (order_id,),
    ).fetchall()


def allocate(conn: sqlite3.Connection, item_id: int, party: str, amount: float | None = None,
             notes: str | None = None) -> Allocation:
    """Assign ``amount`` (default: the item's unallocated remainder) of an item to a party."""
    p = get_party(conn, party)
    if p is None:
        raise ValueError(f"unknown party: {party}")
    row = conn.execute("SELECT * FROM v_item_allocations WHERE item_id = ?", (item_id,)).fetchone()
    if row is None:
        raise ValueError(f"unknown item id: {item_id}")
    if amount is None:
        amount = row["unallocated"]
    if amount <= 0:
        raise ValueError("allocation amount must be positive")
    if amount > row["unallocated"] + 1e-9:
        raise ValueError(f"only {row['unallocated']:.2f} of item {item_id} is unallocated")
    a = Allocation(item_id=item_id, party_id=p.id, amount=round(amount, 2), notes=notes)
    a.insert(conn)
    return a


def unclaimed(conn: sqlite3.Connection, party: str | None = None) -> list[sqlite3.Row]:
    sql = "SELECT * FROM v_unclaimed"
    args: list = []
    if party:
        sql += " WHERE party = ? COLLATE NOCASE"
        args.append(party)
    return conn.execute(sql + " ORDER BY order_date, allocation_id", args).fetchall()


def create_claim(conn: sqlite3.Connection, party: str, title: str | None = None,
                 allocation_ids: list[int] | None = None) -> Claim:
    """Open a draft claim for a party and attach allocations (default: all unclaimed for them)."""
    p = get_party(conn, party)
    if p is None:
        raise ValueError(f"unknown party: {party}")
    c = Claim(party_id=p.id, title=title)
    c.insert(conn)
    if allocation_ids is None:
        allocation_ids = [r["allocation_id"] for r in unclaimed(conn, party)]
    if allocation_ids:
        marks = ",".join("?" for _ in allocation_ids)
        conn.execute(
            f"UPDATE allocations SET claim_id = ? WHERE claim_id IS NULL AND party_id = ? AND id IN ({marks})",
            (c.id, p.id, *allocation_ids),
        )
    return c
