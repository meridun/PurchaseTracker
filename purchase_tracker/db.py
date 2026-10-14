"""SQLite schema, migrations, and connection helpers.

Schema versions are applied in order by ``init_db``; each migration is a callable
that takes an open connection. Never edit an applied migration: add a new one.
"""
from __future__ import annotations

import sqlite3
from collections.abc import Callable
from pathlib import Path

TS_DEFAULT = "(strftime('%Y-%m-%dT%H:%M:%fZ','now'))"

# ---------------------------------------------------------------------------
# v1: single flat purchases table (kept so old databases can be upgraded)
# ---------------------------------------------------------------------------
_V1 = f"""
CREATE TABLE IF NOT EXISTS purchases (
    id          INTEGER PRIMARY KEY,
    vendor      TEXT    NOT NULL,
    order_id    TEXT,
    order_date  TEXT    NOT NULL,
    item_name   TEXT    NOT NULL,
    quantity    INTEGER NOT NULL DEFAULT 1,
    unit_price  REAL,
    total       REAL,
    currency    TEXT    NOT NULL DEFAULT 'USD',
    category    TEXT,
    notes       TEXT,
    source      TEXT    NOT NULL DEFAULT 'manual',
    created_at  TEXT    NOT NULL DEFAULT {TS_DEFAULT}
);
"""

# ---------------------------------------------------------------------------
# v2: orders / items / allocations / claims / accounts / transactions
# ---------------------------------------------------------------------------
_V2 = f"""
-- Who ultimately bears a cost: you, Mom, the HSA, ...
CREATE TABLE parties (
    id      INTEGER PRIMARY KEY,
    name    TEXT NOT NULL UNIQUE,
    kind    TEXT NOT NULL CHECK (kind IN ('self','person','hsa','other')),
    notes   TEXT
);
INSERT INTO parties (name, kind) VALUES ('Me','self'), ('Mom','person'), ('HSA','hsa');

-- Money containers, each owned by a party. Thin slice of "finances".
CREATE TABLE accounts (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE,
    party_id    INTEGER NOT NULL REFERENCES parties(id),
    kind        TEXT NOT NULL CHECK (kind IN ('checking','savings','credit','hsa','cash','other')),
    institution TEXT,
    last4       TEXT,
    notes       TEXT
);

-- A purchase from a vendor. One order has many items.
CREATE TABLE orders (
    id           INTEGER PRIMARY KEY,
    vendor       TEXT NOT NULL,                 -- 'amazon', 'walmart', 'cvs', ...
    order_ref    TEXT,                          -- vendor's order number, if any
    order_date   TEXT NOT NULL,                 -- YYYY-MM-DD
    total        REAL,                          -- what was actually charged, if known
    currency     TEXT NOT NULL DEFAULT 'USD',
    source       TEXT NOT NULL DEFAULT 'manual',  -- 'manual', 'amazon-csv', ...
    document_ref TEXT,                          -- FilingCabinet id / path of the receipt
    notes        TEXT,
    created_at   TEXT NOT NULL DEFAULT {TS_DEFAULT}
);
CREATE UNIQUE INDEX ux_orders_vendor_ref ON orders (vendor, order_ref) WHERE order_ref IS NOT NULL;
CREATE INDEX ix_orders_date ON orders (order_date);

-- A line on an order. line_key makes re-imports idempotent (ASIN, or a hash of the line).
CREATE TABLE items (
    id          INTEGER PRIMARY KEY,
    order_id    INTEGER NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
    line_key    TEXT NOT NULL,
    name        TEXT NOT NULL,
    sku         TEXT,                           -- ASIN / UPC / vendor SKU
    quantity    INTEGER NOT NULL DEFAULT 1,
    unit_price  REAL,
    total       REAL,                           -- line total incl. tax/shipping share if known
    category    TEXT,
    notes       TEXT,
    UNIQUE (order_id, line_key)
);

-- Bank / card statement lines. amount is signed: negative = money out.
CREATE TABLE transactions (
    id          INTEGER PRIMARY KEY,
    account_id  INTEGER NOT NULL REFERENCES accounts(id),
    posted_date TEXT NOT NULL,
    amount      REAL NOT NULL,
    description TEXT,
    external_id TEXT NOT NULL,                  -- bank's id, or a hash of the line
    source      TEXT NOT NULL DEFAULT 'manual',
    notes       TEXT,
    created_at  TEXT NOT NULL DEFAULT {TS_DEFAULT},
    UNIQUE (account_id, external_id)
);
CREATE INDEX ix_transactions_date ON transactions (posted_date);

-- Which charges paid for which order. Many-to-many: Amazon charges per shipment.
CREATE TABLE order_payments (
    order_id       INTEGER NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
    transaction_id INTEGER NOT NULL REFERENCES transactions(id) ON DELETE CASCADE,
    amount         REAL NOT NULL,
    PRIMARY KEY (order_id, transaction_id)
);

-- A batch of allocations submitted to one party for reimbursement.
CREATE TABLE claims (
    id                  INTEGER PRIMARY KEY,
    party_id            INTEGER NOT NULL REFERENCES parties(id),
    title               TEXT,
    status              TEXT NOT NULL DEFAULT 'draft'
                        CHECK (status IN ('draft','submitted','paid','rejected')),
    submitted_at        TEXT,
    paid_at             TEXT,
    paid_amount         REAL,
    paid_transaction_id INTEGER REFERENCES transactions(id),  -- the reimbursement deposit
    document_ref        TEXT,                   -- claim package / confirmation in FilingCabinet
    notes               TEXT,
    created_at          TEXT NOT NULL DEFAULT {TS_DEFAULT}
);

-- Part (or all) of an item's cost assigned to a party. Unallocated remainder is yours.
CREATE TABLE allocations (
    id         INTEGER PRIMARY KEY,
    item_id    INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    party_id   INTEGER NOT NULL REFERENCES parties(id),
    amount     REAL NOT NULL,
    claim_id   INTEGER REFERENCES claims(id) ON DELETE SET NULL,
    notes      TEXT,
    created_at TEXT NOT NULL DEFAULT {TS_DEFAULT}
);
CREATE INDEX ix_allocations_item  ON allocations (item_id);
CREATE INDEX ix_allocations_party ON allocations (party_id);
CREATE INDEX ix_allocations_claim ON allocations (claim_id);

-- Convenience views -------------------------------------------------------
CREATE VIEW v_item_allocations AS
    SELECT i.id AS item_id, i.order_id, i.name, i.total,
           COALESCE(SUM(a.amount), 0)           AS allocated,
           COALESCE(i.total, 0) - COALESCE(SUM(a.amount), 0) AS unallocated
    FROM items i LEFT JOIN allocations a ON a.item_id = i.id
    GROUP BY i.id;

CREATE VIEW v_claim_totals AS
    SELECT c.id AS claim_id, c.party_id, p.name AS party, c.status, c.title,
           COUNT(a.id) AS n_allocations, COALESCE(SUM(a.amount), 0) AS total
    FROM claims c
    JOIN parties p ON p.id = c.party_id
    LEFT JOIN allocations a ON a.claim_id = c.id
    GROUP BY c.id;

-- Allocations not yet on a claim, per party: "what do I still need to ask for?"
CREATE VIEW v_unclaimed AS
    SELECT a.id AS allocation_id, p.name AS party, a.amount,
           i.name AS item, o.vendor, o.order_ref, o.order_date
    FROM allocations a
    JOIN parties p ON p.id = a.party_id
    JOIN items i   ON i.id = a.item_id
    JOIN orders o  ON o.id = i.order_id
    WHERE a.claim_id IS NULL AND p.kind <> 'self';
"""


def _migrate_v1_rows(conn: sqlite3.Connection) -> None:
    """Move any v1 ``purchases`` rows into orders/items, then drop the old table."""
    rows = conn.execute("SELECT * FROM purchases ORDER BY id").fetchall()
    order_ids: dict[tuple[str, str], int] = {}
    for r in rows:
        key = (r["vendor"], r["order_id"]) if r["order_id"] else None
        oid = order_ids.get(key) if key else None
        if oid is None:
            cur = conn.execute(
                "INSERT INTO orders (vendor, order_ref, order_date, currency, source, notes, created_at)"
                " VALUES (?,?,?,?,?,?,?)",
                (r["vendor"], r["order_id"], r["order_date"], r["currency"], r["source"],
                 r["notes"], r["created_at"]),
            )
            oid = cur.lastrowid
            if key:
                order_ids[key] = oid
        conn.execute(
            "INSERT INTO items (order_id, line_key, name, quantity, unit_price, total, category)"
            " VALUES (?,?,?,?,?,?,?)",
            (oid, f"legacy-{r['id']}", r["item_name"], r["quantity"], r["unit_price"],
             r["total"], r["category"]),
        )
    conn.execute(
        "UPDATE orders SET total = (SELECT SUM(total) FROM items WHERE items.order_id = orders.id)"
        " WHERE total IS NULL"
    )
    conn.execute("DROP TABLE purchases")


def _v2(conn: sqlite3.Connection) -> None:
    conn.executescript(_V2)
    _migrate_v1_rows(conn)


# ---------------------------------------------------------------------------
# v3: fields learned from Amazon's invoice pages
# ---------------------------------------------------------------------------
_V3 = """
ALTER TABLE orders ADD COLUMN ship_to            TEXT;   -- recipient name: strong hint for who pays
ALTER TABLE orders ADD COLUMN payment_method     TEXT;   -- e.g. 'Discover ...1234'
ALTER TABLE orders ADD COLUMN subtotal           REAL;   -- pre-tax item subtotal
ALTER TABLE orders ADD COLUMN shipping           REAL;
ALTER TABLE orders ADD COLUMN tax                REAL;
ALTER TABLE orders ADD COLUMN hsa_eligible_total REAL;   -- vendor-stated FSA/HSA-eligible amount incl. tax
ALTER TABLE items  ADD COLUMN hsa_eligible       INTEGER; -- 1/0 from the vendor, NULL = unknown
"""

MIGRATIONS: list[Callable[[sqlite3.Connection], None]] = [
    lambda c: c.executescript(_V1),  # 1
    _v2,                              # 2
    lambda c: c.executescript(_V3),  # 3
]
SCHEMA_VERSION = len(MIGRATIONS)


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def schema_version(conn: sqlite3.Connection) -> int:
    conn.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    row = conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
    return int(row["value"]) if row else 0


def init_db(conn: sqlite3.Connection) -> int:
    """Create or upgrade the schema. Returns the number of migrations applied."""
    current = schema_version(conn)
    applied = 0
    for version in range(current + 1, SCHEMA_VERSION + 1):
        MIGRATIONS[version - 1](conn)
        conn.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES ('schema_version', ?)", (str(version),)
        )
        conn.commit()
        applied += 1
    return applied
