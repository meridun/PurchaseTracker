from __future__ import annotations

import sqlite3
from dataclasses import asdict, dataclass, field


@dataclass
class Purchase:
    vendor: str
    order_date: str
    item_name: str
    order_id: str | None = None
    quantity: int = 1
    unit_price: float | None = None
    total: float | None = None
    currency: str = "USD"
    category: str | None = None
    notes: str | None = None
    source: str = "manual"
    id: int | None = field(default=None)

    def insert(self, conn: sqlite3.Connection) -> bool:
        """Insert; returns False if an identical (vendor, order_id, item) already exists."""
        d = asdict(self)
        d.pop("id")
        cols = ", ".join(d)
        marks = ", ".join("?" for _ in d)
        cur = conn.execute(
            f"INSERT OR IGNORE INTO purchases ({cols}) VALUES ({marks})", tuple(d.values())
        )
        return cur.rowcount == 1


def list_purchases(conn: sqlite3.Connection, vendor: str | None = None, limit: int = 50):
    sql = "SELECT * FROM purchases"
    args: list = []
    if vendor:
        sql += " WHERE vendor = ?"
        args.append(vendor)
    sql += " ORDER BY order_date DESC, id DESC LIMIT ?"
    args.append(limit)
    return conn.execute(sql, args).fetchall()
