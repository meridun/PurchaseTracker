# Data model

Scope: line-item purchase tracking, allocation of costs to payers, and reimbursement
claims, plus the thin slice of account/transaction data needed to reconcile them.
Full personal finance and investments are out of scope and belong in a separate tool.

```
parties ──< accounts ──< transactions >──┐
   │                                     │ order_payments (M:N, per-shipment charges)
   │                                     │
   ├──< claims ──< allocations >── items >── orders
   │                  (payer, amount)
   └──────────────────┘
```

| Table | One row is | Notes |
|---|---|---|
| `parties` | a payer: Me, Mom, HSA | Seeded on first run. `kind` drives behaviour (`self` is never "owed"). |
| `accounts` | a money container owned by a party | Card, checking, HSA. Thin: no balances. |
| `orders` | a purchase from a vendor | Unique on `(vendor, order_ref)`. `ship_to`, `payment_method`, `subtotal`/`shipping`/`tax`, and the vendor's `hsa_eligible_total` when known. `document_ref` points at the receipt in FilingCabinet. |
| `items` | a line on an order | `line_key` (ASIN or content hash) makes re-imports idempotent. `unit_price` is pre-tax; `total` is the line's share of the order grand total (tax/shipping apportioned) so lines sum to the charge. `hsa_eligible` is the vendor's flag. |
| `allocations` | part of an item's cost assigned to a party | Sum per item never exceeds the item total. Unallocated remainder is yours. |
| `claims` | a batch of allocations sent to one party | `draft → submitted → paid/rejected`. `paid_transaction_id` links the deposit that closed it. |
| `transactions` | a bank/card statement line | Signed amount, negative = out. Unique on `(account_id, external_id)`. |
| `order_payments` | "this charge paid for this order" | Many-to-many because Amazon charges per shipment. |
| `vendor_charges` | a charge as the vendor reports it (Amazon Transactions page) | Carries `order_ref`, card last-4, pending/completed. `pt reconcile` sets `transaction_id` to the matching bank line and writes `order_payments`. |

Views: `v_item_allocations` (allocated / unallocated per item), `v_claim_totals`,
`v_unclaimed` (allocations to non-self parties not yet on a claim).

## Workflow

1. Import orders (`pt import-amazon`) or add them by hand (`pt add`).
2. `pt show <order>` and `pt allocate <item> Mom|HSA [amount]` for anything reimbursable.
3. `pt unclaimed` shows what you are owed; `pt claim new Mom` batches it into a draft claim.
   Adjust with `pt claim add|remove <claim> <alloc ids>` while it is a draft.
4. `pt claim submit <id> [--document REF]`, then `pt claim paid <id> [--transaction ID]`
   or `pt claim reject <id>`. `pt claim reopen <id>` returns a submitted/rejected claim
   to draft. Paid is terminal. `pt claims [--party X] [--status S]` lists them.

Transitions: `draft -> submitted -> paid | rejected`, `submitted | rejected -> draft`.
Allocations on a non-draft claim are locked (no remove, no delete).

## Migrations

`purchase_tracker/db.py` holds an ordered list of migrations; `init_db` applies any
that a database is missing and records `schema_version` in `meta`. Add a new entry,
never edit an applied one.
