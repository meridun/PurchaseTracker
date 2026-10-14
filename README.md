# Purchase Tracker

A small local application for tracking Amazon and other purchases. Python 3.11+ and
SQLite, no external dependencies. Designed to evolve: the CLI is the first interface,
and the database/model layer is kept separate so a web UI or reports can be added later.

## Code vs. data

This repo holds **code only**. Your purchase data lives in a separate, private
**instance data directory** that is never committed. See `purchase_tracker/config.py`
for the resolution order; the short version:

| Priority | Source |
|---|---|
| 1 | `--data-dir PATH` on the command line |
| 2 | `PURCHASE_TRACKER_DATA_DIR` environment variable |
| 3 | `data_dir = "..."` in `config.local.toml` (git-ignored, repo root) |
| 4 | Platform app-data dir, e.g. `%LOCALAPPDATA%\PurchaseTracker` on Windows |

Run `pt where` to see what is currently in effect.

## Quick start

```bash
python -m venv .venv
.venv\Scripts\activate        # Windows
pip install -e .[dev]
pt where
pt init
pt add --vendor amazon --date 2026-10-01 --item "USB-C cable" --price 7.49 --qty 2
pt list
pt import-amazon path/to/Retail.OrderHistory.1.csv
pytest
```

## Amazon import

Amazon no longer offers an order-history CSV from the Orders page. Request a data
export instead: Account -> Privacy Central -> *Request My Data* -> *Your Orders*. The
export arrives as a zip containing `Retail.OrderHistory.*.csv`. Column names vary by
export version; `purchase_tracker/importers/amazon.py` accepts the common variants and
is easy to extend. Re-running an import is idempotent (duplicates are skipped).

## Layout

```
purchase_tracker/
  config.py        data-directory resolution
  db.py            SQLite schema + connection
  models.py        Purchase dataclass + queries
  cli.py           `pt` command-line interface
  importers/       vendor-specific importers (amazon.py)
tests/
```

## Roadmap ideas

- More importers (Walmart, Target, bank/credit-card statements, email receipts)
- Categories, tags, and monthly/yearly spend reports
- Optional local web UI
- Backup/export (CSV, JSON)
