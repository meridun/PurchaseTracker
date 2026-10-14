"""Command-line entry point. Installed as ``pt``; also runnable as ``python -m purchase_tracker.cli``."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import __version__
from .config import ENV_VAR, db_path, resolve_data_dir
from .db import SCHEMA_VERSION, connect, init_db, schema_version
from .models import (
    Item, Order, allocate, create_claim, items_for_order, list_orders, list_parties, unclaimed,
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
    print(f"{'id':>4}  {'date':10}  {'vendor':10}  {'items':>5}  {'total':>9}  order ref")
    for r in rows:
        print(f"{r['id']:>4}  {r['order_date']:10}  {r['vendor'][:10]:10}  {r['n_items']:>5}  "
              f"{_money(r['total']):>9}  {r['order_ref'] or ''}")


def cmd_show(args):
    _, conn = _open(args)
    o = conn.execute("SELECT * FROM orders WHERE id = ?", (args.order_id,)).fetchone()
    if o is None:
        sys.exit(f"no order with id {args.order_id}")
    print(f"order {o['id']}  {o['vendor']}  {o['order_ref'] or ''}  {o['order_date']}  total {_money(o['total'])}")
    print(f"{'item':>5}  {'qty':>3}  {'total':>9}  {'alloc':>9}  {'unalloc':>9}  name")
    for i in items_for_order(conn, o["id"]):
        print(f"{i['id']:>5}  {i['quantity']:>3}  {_money(i['total']):>9}  {_money(i['allocated']):>9}  "
              f"{_money(i['unallocated']):>9}  {i['name'][:55]}")


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


def cmd_claim(args):
    _, conn = _open(args)
    c = create_claim(conn, args.party, args.title, args.allocations or None)
    conn.commit()
    t = conn.execute("SELECT * FROM v_claim_totals WHERE claim_id = ?", (c.id,)).fetchone()
    print(f"claim {c.id} for {t['party']}: {t['n_allocations']} allocations, {t['total']:.2f} ({t['status']})")


def cmd_import_amazon(args):
    from .importers.amazon import import_csv

    _, conn = _open(args)
    r = import_csv(conn, Path(args.csv))
    print(f"orders: {r.orders_new} new; items: {r.items_new} new, {r.items_skipped} already present")


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

    c = sub.add_parser("claim", help="open a draft claim from unclaimed allocations")
    c.add_argument("party")
    c.add_argument("--title")
    c.add_argument("--allocations", type=int, nargs="*", help="specific allocation ids (default: all unclaimed)")
    c.set_defaults(fn=cmd_claim)

    i = sub.add_parser("import-amazon", help="import an Amazon order-history CSV export")
    i.add_argument("csv")
    i.set_defaults(fn=cmd_import_amazon)
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    args.fn(args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
