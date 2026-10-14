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
pt account add "Chase Visa" --kind credit --last4 1234
pt import-statement path/to/Chase_Activity.csv --account "Chase Visa"   # auto-detects format
pt tx add --account "Chase Visa" --date 2026-10-05 --amount -45.10 --desc "Walgreens"
pt tx list --unmatched          # charges not yet linked to an order
pt reconcile                    # vendor charges -> bank lines -> order payments
pytest
```

## Amazon import

Two routes, both idempotent (re-imports skip what is already there):

**Browser extraction (richer, immediate).** Two console scripts, run while logged in
to Amazon in Chrome (F12 -> Console, paste, Enter). Each downloads a JSON file that
`pt import-amazon <file>` ingests. Run them in this order:

1. `scripts/amazon_transactions.js` on *Your Payments -> Transactions*: every card
   charge with its order number, including Amazon's per-shipment splits, paging back to
   `CONFIG.STOP_BEFORE`. Stored in `vendor_charges`; `pt reconcile` (run automatically
   after imports) matches them to statement lines by card last-4, amount, and date, and
   records `order_payments`.
2. `pt amazon-refs` prints the order numbers from those charges that have no invoice
   yet. Paste the list into `CONFIG.ORDER_REFS` of `scripts/amazon_extract.js` and run
   it on any Amazon page: it fetches each order's printable invoice and extracts line
   items, prices, the vendor's FSA/HSA flags and total, ship-to, payment method, and the
   invoice HTML, gzipped (`.json.gz`, which `pt import-amazon` reads directly). The
   importer saves each invoice under
   `<data dir>/documents/amazon/<order>.html` and sets `document_ref`, so the receipt
   needed for an HSA claim is already on disk. Per item, `unit_price` is pre-tax and
   `total` is the item's share of the grand total with tax and shipping apportioned.

If Chrome blocks the download (it does after one automatic download per tab), open a
fresh tab on amazon.com and the script's data is still in IndexedDB; or re-run with
`DOWNLOAD: false` and save the console output.

Why this order: Amazon's order-list pages only fill in order numbers client-side, so
the Transactions page is the reliable index of what was bought. Orders paid entirely
with a gift-card balance may not appear there; run `amazon_extract.js` without
`ORDER_REFS` on the relevant list page to pick those up.

**Privacy Central CSV (bulk history).** Account -> Privacy Central -> *Request My
Data* -> *Your Orders*; the zip contains `Retail.OrderHistory.*.csv`. Run
`pt import-amazon Retail.OrderHistory.1.csv`. Orders first seen via CSV are enriched
when the same order later arrives via the JSON route.

## Statement import

`pt import-statement` auto-detects Chase, Amex, Capital One, Discover, Citi, Bank of
America, and a generic `Date,Description,Amount` layout from the CSV headers, and
normalises amounts so that negative = money out. Use `--format` to force a profile and
`--flip-sign` if a bank's purchases come out positive. Adding a bank is one `Profile`
entry in `purchase_tracker/importers/statement.py`. Re-imports of overlapping date
ranges skip rows already present.

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

- Returns and refunds (CSV returns file, invoice refund lines)
- Item review state and the review-queue report
- Claim package export (summary + receipts as one PDF) for HSA submission
- More order importers (Walmart, Target, email receipts)
- Spend reports; optional local web UI
