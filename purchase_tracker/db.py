"""SQLite schema and connection helpers."""
from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS purchases (
    id          INTEGER PRIMARY KEY,
    vendor      TEXT    NOT NULL,              -- 'amazon', 'walmart', 'local-store', ...
    order_id    TEXT,                          -- vendor order number, if any
    order_date  TEXT    NOT NULL,              -- ISO-8601 date (YYYY-MM-DD)
    item_name   TEXT    NOT NULL,
    quantity    INTEGER NOT NULL DEFAULT 1,
    unit_price  REAL,
    total       REAL,                          -- line total actually paid
    currency    TEXT    NOT NULL DEFAULT 'USD',
    category    TEXT,
    notes       TEXT,
    source      TEXT    NOT NULL DEFAULT 'manual',  -- 'manual', 'amazon-csv', ...
    created_at  TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

-- Makes re-running an import idempotent.
CREATE UNIQUE INDEX IF NOT EXISTS ux_purchases_vendor_order_item
    ON purchases (vendor, order_id, item_name)
    WHERE order_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS ix_purchases_date ON purchases (order_date);
"""


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.execute(
        "INSERT OR IGNORE INTO meta (key, value) VALUES ('schema_version', ?)",
        (str(SCHEMA_VERSION),),
    )
    conn.commit()
