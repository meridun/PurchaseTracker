"""Command-line entry point. Installed as ``pt``; also runnable as ``python -m purchase_tracker.cli``."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import __version__
from .config import ENV_VAR, db_path, resolve_data_dir
from .db import SCHEMA_VERSION, connect, init_db, schema_version
from .models import (
    Account, Item, Order, add_transaction, allocate, claim_add_allocations, claim_allocations,
    claim_remove_allocations, create_claim, delete_allocation, get_account, get_party,
    items_for_order, list_accounts, list_claims, list_orders, list_parties, list_transactions,
    pay_claim, reject_claim, reopen_claim, submit_claim, unclaimed,
)


def _open(args):
    data_dir = resolve_data_dir(args.data_dir)
    conn = connect(db_path(data_dir))
    applied = init_db(conn)
    if applied:
        print(f"(upgraded database to schema v{SCHEMA_VERSION})", file=sys.stderr)
    return data_dir, conn


def _money(v) -> str:
    return f"{v:.2f}" if v is not None else "-"


def cmd_where(args):
    data_dir = resolve_data_dir(args.data_dir)
    db = db_path(data_dir)
    print(f"data dir : {data_dir}")
    if db.exists():
        print(f"database : {db}  (schema v{schema_version(connect(db))}, code expects v{SCHEMA_VERSION})")
    else:
        print(f"database : {db}  (not created yet)")
    print(f"override : set {ENV_VAR}, pass --data-dir, or set data_dir in config.local.toml")


def cmd_init(args):
    data_dir, _ = _open(args)
    print(f"ready: {db_path(data_dir)} (schema v{SCHEMA_VERSION})")


def cmd_add(args):
    """Add a one-item order by hand."""
    _, conn = _open(args)
    total = args.total
    if total is None and args.price is not None:
        total = round(args.price * args.qty, 2)
    order = Order(vendor=args.vendor, order_date=args.date, order_ref=args.order_id,
                  total=total, notes=args.notes)
    oid, created = order.get_or_insert(conn)
    item = Item(order_id=oid, name=args.item, line_key=f"manual:{args.item}",
                quantity=args.qty, unit_price=args.price, total=total, category=args.category)
    iid = item.insert(conn)
    conn.commit()
    if iid is None:
        print(f"skipped: order {oid} already has item {args.item!r}")
    else:
        print(f"{'added order' if created else 'added to existing order'} {oid}, item {iid}")


def cmd_list(args):
    _, conn = _open(args)
    rows = list_orders(conn, vendor=args.vendor, limit=args.limit)
    if not rows:
        print("no orders")
        return
    print(f"{'id':>4}  {'date':10}  {'vendor':10}  {'items':>5}  {'total':>9}  {'ship to':16}  order ref")
    for r in rows:
        print(f"{r['id']:>4}  {r['order_date']:10}  {r['vendor'][:10]:10}  {r['n_items']:>5}  "
              f"{_money(r['total']):>9}  {(r['ship_to'] or '')[:16]:16}  {r['order_ref'] or ''}")


def cmd_show(args):
    _, conn = _open(args)
    o = conn.execute("SELECT * FROM orders WHERE id = ?", (args.order_id,)).fetchone()
    if o is None:
        sys.exit(f"no order with id {args.order_id}")
    print(f"order {o['id']}  {o['vendor']}  {o['order_ref'] or ''}  {o['order_date']}  total {_money(o['total'])}")
    extra = [f"ship to: {o['ship_to']}" if o["ship_to"] else "",
             f"paid with: {o['payment_method']}" if o["payment_method"] else "",
             f"HSA-eligible: {_money(o['hsa_eligible_total'])}" if o["hsa_eligible_total"] else ""]
    if any(extra):
        print("  " + "   ".join(e for e in extra if e))
    print(f"{'item':>5}  {'qty':>3}  {'total':>9}  {'alloc':>9}  {'unalloc':>9}  {'hsa':3}  name")
    for i in items_for_order(conn, o["id"]):
        hsa = {1: "yes", 0: "no"}.get(i["hsa_eligible"], "")
        print(f"{i['id']:>5}  {i['quantity']:>3}  {_money(i['total']):>9}  {_money(i['allocated']):>9}  "
              f"{_money(i['unallocated']):>9}  {hsa:3}  {i['name'][:55]}")


def cmd_parties(args):
    _, conn = _open(args)
    for p in list_parties(conn):
        print(f"{p.id:>3}  {p.name:12} {p.kind}")


def cmd_allocate(args):
    _, conn = _open(args)
    a = allocate(conn, args.item_id, args.party, args.amount, args.notes)
    conn.commit()
    print(f"allocation {a.id}: {a.amount:.2f} of item {a.item_id} -> {args.party}")


def cmd_unclaimed(args):
    _, conn = _open(args)
    rows = unclaimed(conn, args.party)
    if not rows:
        print("nothing unclaimed")
        return
    total = 0.0
    print(f"{'alloc':>5}  {'party':8}  {'date':10}  {'amount':>8}  item")
    for r in rows:
        total += r["amount"]
        print(f"{r['allocation_id']:>5}  {r['party'][:8]:8}  {r['order_date']:10}  {r['amount']:>8.2f}  {r['item'][:50]}")
    print(f"{'':5}  {'':8}  {'':10}  {total:>8.2f}  total")


CLAIM_HEADER = f"{'id':>4}  {'party':8}  {'status':9}  {'date':10}  {'n':>3}  {'total':>9}  title"


def _claim_line(t) -> str:
    when = t["paid_at"] or t["submitted_at"] or t["created_at"][:10]
    paid = f"  paid {t['paid_amount']:.2f}" if t["paid_amount"] is not None else ""
    return (f"{t['claim_id']:>4}  {t['party'][:8]:8}  {t['status']:9}  {when:10}  "
            f"{t['n_allocations']:>3}  {t['total']:>9.2f}{paid}  {t['title'] or ''}")


def _print_claim(conn, claim_id: int):
    t = next(r for r in list_claims(conn) if r["claim_id"] == claim_id)
    print(CLAIM_HEADER)
    print(_claim_line(t))


def cmd_claims(args):
    _, conn = _open(args)
    rows = list_claims(conn, args.party, args.status)
    if not rows:
        print("no claims")
        return
    print(CLAIM_HEADER)
    for t in rows:
        print(_claim_line(t))


def cmd_claim_new(args):
    _, conn = _open(args)
    c = create_claim(conn, args.party, args.title, args.allocations or None)
    conn.commit()
    _print_claim(conn, c.id)


def cmd_claim_show(args):
    _, conn = _open(args)
    c = conn.execute("SELECT * FROM claims WHERE id = ?", (args.claim_id,)).fetchone()
    if c is None:
        sys.exit(f"error: no claim with id {args.claim_id}")
    _print_claim(conn, args.claim_id)
    for k in ("submitted_at", "paid_at", "paid_transaction_id", "document_ref", "notes"):
        if c[k] is not None:
            print(f"  {k:20} {c[k]}")
    print(f"\n{'alloc':>5}  {'date':10}  {'order':>5}  {'amount':>8}  item")
    for r in claim_allocations(conn, args.claim_id):
        print(f"{r['allocation_id']:>5}  {r['order_date']:10}  {r['order_id']:>5}  {r['amount']:>8.2f}  {r['item'][:50]}")


def _lifecycle(fn):
    """Open the db, run a claim mutation, commit, print the claim row. ValueError -> exit."""
    def run(args):
        _, conn = _open(args)
        try:
            fn(conn, args)
        except ValueError as e:
            sys.exit(f"error: {e}")
        conn.commit()
        _print_claim(conn, args.claim_id)
    return run


@_lifecycle
def cmd_claim_submit(conn, args):
    submit_claim(conn, args.claim_id, args.date, args.document)


@_lifecycle
def cmd_claim_paid(conn, args):
    c = pay_claim(conn, args.claim_id, args.amount, args.date, args.transaction)
    total = conn.execute("SELECT total FROM v_claim_totals WHERE claim_id = ?", (c.id,)).fetchone()["total"]
    if abs(c.paid_amount - total) > 0.005:
        print(f"note: paid {c.paid_amount:.2f} differs from claim total {total:.2f}", file=sys.stderr)


@_lifecycle
def cmd_claim_reject(conn, args):
    reject_claim(conn, args.claim_id, args.notes)


@_lifecycle
def cmd_claim_reopen(conn, args):
    reopen_claim(conn, args.claim_id)


@_lifecycle
def cmd_claim_add(conn, args):
    n = claim_add_allocations(conn, args.claim_id, args.allocations)
    print(f"attached {n} of {len(args.allocations)} allocations")


@_lifecycle
def cmd_claim_remove(conn, args):
    n = claim_remove_allocations(conn, args.claim_id, args.allocations)
    print(f"detached {n} of {len(args.allocations)} allocations")


def cmd_unallocate(args):
    _, conn = _open(args)
    try:
        delete_allocation(conn, args.allocation_id)
    except ValueError as e:
        sys.exit(f"error: {e}")
    conn.commit()
    print(f"deleted allocation {args.allocation_id}")


def cmd_accounts(args):
    _, conn = _open(args)
    rows = list_accounts(conn)
    if not rows:
        print("no accounts (pt account add ...)")
        return
    print(f"{'id':>3}  {'name':20}  {'party':8}  {'kind':9}  {'inst':12}  {'last4':5}  {'tx':>4}  last")
    for a in rows:
        print(f"{a['id']:>3}  {a['name'][:20]:20}  {a['party'][:8]:8}  {a['kind']:9}  "
              f"{(a['institution'] or '')[:12]:12}  {a['last4'] or '':5}  {a['n_tx']:>4}  {a['last_tx'] or ''}")


def cmd_account_add(args):
    _, conn = _open(args)
    p = get_party(conn, args.party)
    if p is None:
        sys.exit(f"error: unknown party {args.party!r} (see pt parties)")
    acct = Account(name=args.name, party_id=p.id, kind=args.kind, institution=args.institution,
                   last4=args.last4, notes=args.notes)
    if acct.insert(conn) is None:
        sys.exit(f"error: account {args.name!r} already exists")
    conn.commit()
    print(f"account {acct.id}: {acct.name} ({args.kind}, {p.name})")


def cmd_tx_add(args):
    _, conn = _open(args)
    try:
        t = add_transaction(conn, args.account, args.date, args.amount, args.desc, args.ref, args.notes)
    except ValueError as e:
        sys.exit(f"error: {e}")
    conn.commit()
    if t is None:
        sys.exit("skipped: identical transaction already recorded (use --ref to disambiguate)")
    print(f"transaction {t.id}: {t.posted_date} {t.amount:+.2f} {t.description or ''}")


def cmd_tx_list(args):
    _, conn = _open(args)
    rows = list_transactions(conn, args.account, args.unmatched, args.limit)
    if not rows:
        print("no transactions")
        return
    print(f"{'id':>4}  {'date':10}  {'account':14}  {'amount':>9}  {'matched':>8}  description")
    for t in rows:
        print(f"{t['id']:>4}  {t['posted_date']:10}  {t['account'][:14]:14}  {t['amount']:>9.2f}  "
              f"{t['matched']:>8.2f}  {(t['description'] or '')[:50]}")


def cmd_import_statement(args):
    from .importers.statement import import_csv

    _, conn = _open(args)
    acct = get_account(conn, args.account)
    if acct is None:
        sys.exit(f"error: unknown account {args.account!r} (see pt accounts)")
    try:
        r = import_csv(conn, Path(args.csv), acct.id, args.format, args.flip_sign)
    except ValueError as e:
        sys.exit(f"error: {e}")
    print(f"format {r.profile}: {r.new} new, {r.skipped} already present")


def cmd_import_amazon(args):
    from .importers.amazon import import_csv, import_json

    _, conn = _open(args)
    path = Path(args.file)
    r = (import_json if path.suffix.lower() == ".json" else import_csv)(conn, path)
    print(f"orders: {r.orders_new} new, {r.orders_seen} already present; "
          f"items: {r.items_new} new, {r.items_skipped} already present")


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="pt", description="Purchase Tracker")
    ap.add_argument("--version", action="version", version=f"purchase-tracker {__version__}")
    ap.add_argument("--data-dir", help=f"instance data directory (default: ${ENV_VAR} or platform app-data dir)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("where", help="show where instance data lives").set_defaults(fn=cmd_where)
    sub.add_parser("init", help="create or upgrade the database").set_defaults(fn=cmd_init)
    sub.add_parser("parties", help="list payers (Me, Mom, HSA, ...)").set_defaults(fn=cmd_parties)

    a = sub.add_parser("add", help="add a one-item order by hand")
    a.add_argument("--vendor", required=True)
    a.add_argument("--date", required=True, help="YYYY-MM-DD")
    a.add_argument("--item", required=True)
    a.add_argument("--order-id", help="vendor order number")
    a.add_argument("--qty", type=int, default=1)
    a.add_argument("--price", type=float, help="unit price")
    a.add_argument("--total", type=float)
    a.add_argument("--category")
    a.add_argument("--notes")
    a.set_defaults(fn=cmd_add)

    l = sub.add_parser("list", help="list recent orders")
    l.add_argument("--vendor")
    l.add_argument("--limit", type=int, default=50)
    l.set_defaults(fn=cmd_list)

    s = sub.add_parser("show", help="show an order's items and allocations")
    s.add_argument("order_id", type=int)
    s.set_defaults(fn=cmd_show)

    al = sub.add_parser("allocate", help="assign (part of) an item's cost to a party")
    al.add_argument("item_id", type=int)
    al.add_argument("party", help="Me, Mom, HSA, ...")
    al.add_argument("amount", type=float, nargs="?", help="default: whole unallocated remainder")
    al.add_argument("--notes")
    al.set_defaults(fn=cmd_allocate)

    u = sub.add_parser("unclaimed", help="allocations not yet on a claim")
    u.add_argument("--party")
    u.set_defaults(fn=cmd_unclaimed)

    un = sub.add_parser("unallocate", help="delete an allocation (blocked once its claim is submitted)")
    un.add_argument("allocation_id", type=int)
    un.set_defaults(fn=cmd_unallocate)

    cl = sub.add_parser("claims", help="list claims")
    cl.add_argument("--party")
    cl.add_argument("--status", choices=["draft", "submitted", "paid", "rejected"])
    cl.set_defaults(fn=cmd_claims)

    c = sub.add_parser("claim", help="claim lifecycle: new, show, submit, paid, reject, reopen, add, remove")
    cs = c.add_subparsers(dest="claim_cmd", required=True)

    x = cs.add_parser("new", help="open a draft claim from unclaimed allocations")
    x.add_argument("party")
    x.add_argument("--title")
    x.add_argument("--allocations", type=int, nargs="*", help="specific allocation ids (default: all unclaimed)")
    x.set_defaults(fn=cmd_claim_new)

    x = cs.add_parser("show", help="claim details and its allocations")
    x.add_argument("claim_id", type=int)
    x.set_defaults(fn=cmd_claim_show)

    x = cs.add_parser("submit", help="draft -> submitted")
    x.add_argument("claim_id", type=int)
    x.add_argument("--date", help="YYYY-MM-DD (default today)")
    x.add_argument("--document", help="FilingCabinet ref / path of the submission package")
    x.set_defaults(fn=cmd_claim_submit)

    x = cs.add_parser("paid", help="submitted -> paid")
    x.add_argument("claim_id", type=int)
    x.add_argument("--amount", type=float, help="default: claim total, or the linked transaction amount")
    x.add_argument("--date", help="YYYY-MM-DD (default today)")
    x.add_argument("--transaction", type=int, help="id of the reimbursing deposit in transactions")
    x.set_defaults(fn=cmd_claim_paid)

    x = cs.add_parser("reject", help="submitted -> rejected")
    x.add_argument("claim_id", type=int)
    x.add_argument("--notes")
    x.set_defaults(fn=cmd_claim_reject)

    x = cs.add_parser("reopen", help="submitted/rejected -> draft")
    x.add_argument("claim_id", type=int)
    x.set_defaults(fn=cmd_claim_reopen)

    x = cs.add_parser("add", help="attach unclaimed allocations to a draft claim")
    x.add_argument("claim_id", type=int)
    x.add_argument("allocations", type=int, nargs="+")
    x.set_defaults(fn=cmd_claim_add)

    x = cs.add_parser("remove", help="detach allocations from a draft claim")
    x.add_argument("claim_id", type=int)
    x.add_argument("allocations", type=int, nargs="+")
    x.set_defaults(fn=cmd_claim_remove)

    sub.add_parser("accounts", help="list accounts").set_defaults(fn=cmd_accounts)
    ac = sub.add_parser("account", help="account management: add")
    acs = ac.add_subparsers(dest="account_cmd", required=True)
    x = acs.add_parser("add", help="add an account")
    x.add_argument("name")
    x.add_argument("--party", default="Me", help="owner (default Me)")
    x.add_argument("--kind", default="credit", choices=["checking", "savings", "credit", "hsa", "cash", "other"])
    x.add_argument("--institution")
    x.add_argument("--last4")
    x.add_argument("--notes")
    x.set_defaults(fn=cmd_account_add)

    tx = sub.add_parser("tx", help="transactions: add, list")
    txs = tx.add_subparsers(dest="tx_cmd", required=True)
    x = txs.add_parser("add", help="record a transaction by hand (negative = money out)")
    x.add_argument("--account", required=True)
    x.add_argument("--date", required=True, help="YYYY-MM-DD")
    x.add_argument("--amount", required=True, type=float, help="negative = money out, positive = in")
    x.add_argument("--desc", help="description")
    x.add_argument("--ref", help="bank reference / check number; also disambiguates identical entries")
    x.add_argument("--notes")
    x.set_defaults(fn=cmd_tx_add)
    x = txs.add_parser("list", help="list transactions, newest first")
    x.add_argument("--account")
    x.add_argument("--unmatched", action="store_true", help="only charges not yet linked to an order")
    x.add_argument("--limit", type=int, default=50)
    x.set_defaults(fn=cmd_tx_list)

    from .importers.statement import PROFILE_BY_NAME
    st = sub.add_parser("import-statement", help="import a card/bank statement CSV")
    st.add_argument("csv")
    st.add_argument("--account", required=True, help="account name (see pt accounts)")
    st.add_argument("--format", choices=list(PROFILE_BY_NAME), help="default: auto-detect from headers")
    st.add_argument("--flip-sign", action="store_true", help="if purchases come out positive")
    st.set_defaults(fn=cmd_import_statement)

    i = sub.add_parser("import-amazon",
                       help="import Amazon orders: .csv (Privacy Central export) or .json (scripts/amazon_extract.js)")
    i.add_argument("file")
    i.set_defaults(fn=cmd_import_amazon)
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    args.fn(args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
