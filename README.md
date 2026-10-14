# Purchase Tracker

A small local application for tracking Amazon and other purchases at the line-item
level, splitting costs between payers (you, a family member whose finances you manage,
an HSA), and batching the reimbursable parts into claims. Python 3.11+ and SQLite, no
external dependencies. See [docs/schema.md](docs/schema.md) for the data model.

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
pt show 1                  # items + allocations for order 1
pt allocate 3 HSA          # whole remainder of item 3 is HSA-reimbursable
pt allocate 4 Mom 12.50    # part of item 4 is Mom's
pt unclaimed               # what you are owed, not yet claimed
pt claim new Mom --title "Oct 2026"
pt claim submit 1 --document "FilingCabinet:claims/2026-10-mom.pdf"
pt claim paid 1 --transaction 42   # link the deposit once statements are imported
pt claims
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
  db.py            SQLite schema, migrations, connection
  models.py        table dataclasses + queries (allocate, claims, ...)
  cli.py           `pt` command-line interface
  importers/       vendor-specific importers (amazon.py)
docs/schema.md     data model and workflow
tests/
```

## Roadmap ideas

- Statement importers (card, bank, HSA) and order-to-charge reconciliation
- Claim package export (summary + receipts as one PDF) for HSA submission
- More order importers (Walmart, Target, email receipts)
- Spend reports; optional local web UI
