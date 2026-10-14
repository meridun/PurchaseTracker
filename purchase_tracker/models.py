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
    ship_to: str | None = None
    payment_method: str | None = None
    subtotal: float | None = None
    shipping: float | None = None
    tax: float | None = None
    hsa_eligible_total: float | None = None
    id: int | None = None

    DETAIL_COLS = ("total", "ship_to", "payment_method", "subtotal", "shipping", "tax",
                   "hsa_eligible_total", "document_ref")

    def get_or_insert(self, conn: sqlite3.Connection) -> tuple[int, bool]:
        """Insert, or look up the existing order with the same (vendor, order_ref).

        On an existing order, fill in any detail columns that are still NULL.
        """
        new_id = self.insert(conn)
        if new_id is not None:
            return new_id, True
        row = conn.execute(
            "SELECT id FROM orders WHERE vendor = ? AND order_ref = ?", (self.vendor, self.order_ref)
        ).fetchone()
        self.id = row["id"]
        sets = ", ".join(f"{c} = COALESCE({c}, ?)" for c in self.DETAIL_COLS)
        conn.execute(f"UPDATE orders SET {sets} WHERE id = ?",
                     (*(getattr(self, c) for c in self.DETAIL_COLS), self.id))
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
    hsa_eligible: int | None = None  # 1 / 0 from the vendor, None = unknown
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


# --- claim lifecycle ---------------------------------------------------------

# status -> statuses it may move to
CLAIM_TRANSITIONS = {
    "draft": {"submitted"},
    "submitted": {"paid", "rejected", "draft"},
    "rejected": {"draft"},
    "paid": set(),
}


def _today() -> str:
    from datetime import date
    return date.today().isoformat()


def get_claim(conn: sqlite3.Connection, claim_id: int) -> Claim:
    row = conn.execute("SELECT * FROM claims WHERE id = ?", (claim_id,)).fetchone()
    if row is None:
        raise ValueError(f"no claim with id {claim_id}")
    return Claim.from_row(row)


def list_claims(conn: sqlite3.Connection, party: str | None = None,
                status: str | None = None) -> list[sqlite3.Row]:
    """Rows from v_claim_totals joined with dates and paid_amount."""
    sql = ("SELECT v.*, c.submitted_at, c.paid_at, c.paid_amount, c.created_at"
           " FROM v_claim_totals v JOIN claims c ON c.id = v.claim_id")
    where, args = [], []
    if party:
        where.append("v.party = ? COLLATE NOCASE"); args.append(party)
    if status:
        where.append("v.status = ?"); args.append(status)
    if where:
        sql += " WHERE " + " AND ".join(where)
    return conn.execute(sql + " ORDER BY v.claim_id", args).fetchall()


def claim_allocations(conn: sqlite3.Connection, claim_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT a.id AS allocation_id, a.amount, a.notes, i.id AS item_id, i.name AS item,"
        " o.id AS order_id, o.vendor, o.order_ref, o.order_date"
        " FROM allocations a JOIN items i ON i.id = a.item_id JOIN orders o ON o.id = i.order_id"
        " WHERE a.claim_id = ? ORDER BY o.order_date, a.id", (claim_id,)
    ).fetchall()


def _transition(conn: sqlite3.Connection, claim_id: int, to: str, **cols) -> Claim:
    c = get_claim(conn, claim_id)
    if to not in CLAIM_TRANSITIONS[c.status]:
        raise ValueError(f"claim {claim_id} is {c.status}; cannot move to {to}")
    cols["status"] = to
    sets = ", ".join(f"{k} = ?" for k in cols)
    conn.execute(f"UPDATE claims SET {sets} WHERE id = ?", (*cols.values(), claim_id))
    return get_claim(conn, claim_id)


def submit_claim(conn: sqlite3.Connection, claim_id: int, date: str | None = None,
                 document_ref: str | None = None) -> Claim:
    c = get_claim(conn, claim_id)
    if not claim_allocations(conn, claim_id):
        raise ValueError(f"claim {claim_id} has no allocations")
    cols = {"submitted_at": date or _today()}
    if document_ref:
        cols["document_ref"] = document_ref
    return _transition(conn, claim_id, "submitted", **cols)


def pay_claim(conn: sqlite3.Connection, claim_id: int, amount: float | None = None,
              date: str | None = None, transaction_id: int | None = None) -> Claim:
    """Mark paid. amount defaults to the claim total; transaction_id links the deposit."""
    total = conn.execute("SELECT total FROM v_claim_totals WHERE claim_id = ?", (claim_id,)).fetchone()["total"]
    if transaction_id is not None:
        t = conn.execute("SELECT amount FROM transactions WHERE id = ?", (transaction_id,)).fetchone()
        if t is None:
            raise ValueError(f"no transaction with id {transaction_id}")
        if amount is None:
            amount = abs(t["amount"])
    cols = {"paid_at": date or _today(), "paid_amount": round(amount if amount is not None else total, 2),
            "paid_transaction_id": transaction_id}
    return _transition(conn, claim_id, "paid", **cols)


def reject_claim(conn: sqlite3.Connection, claim_id: int, notes: str | None = None) -> Claim:
    cols = {}
    if notes:
        old = get_claim(conn, claim_id).notes
        cols["notes"] = f"{old}\n{notes}" if old else notes
    return _transition(conn, claim_id, "rejected", **cols)


def reopen_claim(conn: sqlite3.Connection, claim_id: int) -> Claim:
    """submitted/rejected -> draft, clearing submission details."""
    return _transition(conn, claim_id, "draft", submitted_at=None, paid_at=None, paid_amount=None)


def claim_add_allocations(conn: sqlite3.Connection, claim_id: int, allocation_ids: list[int]) -> int:
    c = get_claim(conn, claim_id)
    if c.status != "draft":
        raise ValueError(f"claim {claim_id} is {c.status}; only draft claims can be edited")
    marks = ",".join("?" for _ in allocation_ids)
    cur = conn.execute(
        f"UPDATE allocations SET claim_id = ? WHERE claim_id IS NULL AND party_id = ? AND id IN ({marks})",
        (claim_id, c.party_id, *allocation_ids))
    return cur.rowcount


def claim_remove_allocations(conn: sqlite3.Connection, claim_id: int, allocation_ids: list[int]) -> int:
    c = get_claim(conn, claim_id)
    if c.status != "draft":
        raise ValueError(f"claim {claim_id} is {c.status}; only draft claims can be edited")
    marks = ",".join("?" for _ in allocation_ids)
    cur = conn.execute(
        f"UPDATE allocations SET claim_id = NULL WHERE claim_id = ? AND id IN ({marks})",
        (claim_id, *allocation_ids))
    return cur.rowcount


def delete_allocation(conn: sqlite3.Connection, allocation_id: int) -> None:
    row = conn.execute(
        "SELECT a.claim_id, c.status FROM allocations a LEFT JOIN claims c ON c.id = a.claim_id WHERE a.id = ?",
        (allocation_id,)).fetchone()
    if row is None:
        raise ValueError(f"no allocation with id {allocation_id}")
    if row["claim_id"] is not None and row["status"] != "draft":
        raise ValueError(f"allocation {allocation_id} is on {row['status']} claim {row['claim_id']}")
    conn.execute("DELETE FROM allocations WHERE id = ?", (allocation_id,))


# --- accounts & transactions ------------------------------------------------

def get_account(conn: sqlite3.Connection, name: str) -> Account | None:
    row = conn.execute("SELECT * FROM accounts WHERE name = ? COLLATE NOCASE", (name,)).fetchone()
    return Account.from_row(row) if row else None


def list_accounts(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT a.*, p.name AS party, COUNT(t.id) AS n_tx, MAX(t.posted_date) AS last_tx"
        " FROM accounts a JOIN parties p ON p.id = a.party_id"
        " LEFT JOIN transactions t ON t.account_id = a.id GROUP BY a.id ORDER BY a.id"
    ).fetchall()


def add_transaction(conn: sqlite3.Connection, account: str, date: str, amount: float,
                    description: str | None, ref: str | None = None,
                    notes: str | None = None) -> Transaction | None:
    """Manual entry. Returns None if an identical (account, ref-or-content) row exists."""
    a = get_account(conn, account)
    if a is None:
        raise ValueError(f"unknown account: {account}")
    from .importers.statement import content_key
    ext = f"manual:{ref}" if ref else "manual:" + content_key(date, amount, description or "")
    t = Transaction(account_id=a.id, posted_date=date, amount=round(amount, 2),
                    description=description, external_id=ext, notes=notes)
    return t if t.insert(conn) is not None else None


def list_transactions(conn: sqlite3.Connection, account: str | None = None,
                      unmatched: bool = False, limit: int = 50) -> list[sqlite3.Row]:
    """Newest first. Columns: transactions.* + account, matched (sum linked to orders)."""
    sql = ("SELECT t.*, a.name AS account, COALESCE(SUM(op.amount), 0) AS matched"
           " FROM transactions t JOIN accounts a ON a.id = t.account_id"
           " LEFT JOIN order_payments op ON op.transaction_id = t.id")
    where, args = [], []
    if account:
        where.append("a.name = ? COLLATE NOCASE"); args.append(account)
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " GROUP BY t.id"
    if unmatched:
        sql += " HAVING t.amount < 0 AND matched = 0"
    sql += " ORDER BY t.posted_date DESC, t.id DESC LIMIT ?"
    args.append(limit)
    return conn.execute(sql, args).fetchall()
