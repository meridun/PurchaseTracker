"""Command-line entry point. Installed as ``pt``; also runnable as ``python -m purchase_tracker.cli``."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import __version__
from .config import ENV_VAR, db_path, resolve_data_dir
from .db import connect, init_db
from .models import Purchase, list_purchases


def _open(args):
    data_dir = resolve_data_dir(args.data_dir)
    conn = connect(db_path(data_dir))
    init_db(conn)
    return data_dir, conn


def cmd_where(args):
    data_dir = resolve_data_dir(args.data_dir)
    db = db_path(data_dir)
    print(f"data dir : {data_dir}")
    print(f"database : {db}  ({'exists' if db.exists() else 'not created yet'})")
    print(f"override : set {ENV_VAR}, pass --data-dir, or set data_dir in config.local.toml")


def cmd_init(args):
    data_dir, _ = _open(args)
    print(f"initialised {db_path(data_dir)}")


def cmd_add(args):
    _, conn = _open(args)
    total = args.total
    if total is None and args.price is not None:
        total = args.price * args.qty
    p = Purchase(
        vendor=args.vendor,
        order_date=args.date,
        item_name=args.item,
        order_id=args.order_id,
        quantity=args.qty,
        unit_price=args.price,
        total=total,
        category=args.category,
        notes=args.notes,
    )
    ok = p.insert(conn)
    conn.commit()
    print("added" if ok else "skipped (duplicate vendor/order_id/item)")


def cmd_list(args):
    _, conn = _open(args)
    rows = list_purchases(conn, vendor=args.vendor, limit=args.limit)
    if not rows:
        print("no purchases")
        return
    print(f"{'date':10}  {'vendor':10}  {'qty':>3}  {'total':>9}  item")
    for r in rows:
        total = f"{r['total']:.2f}" if r["total"] is not None else "-"
        print(f"{r['order_date']:10}  {r['vendor'][:10]:10}  {r['quantity']:>3}  {total:>9}  {r['item_name'][:60]}")


def cmd_import_amazon(args):
    from .importers.amazon import import_csv

    _, conn = _open(args)
    inserted, skipped = import_csv(conn, Path(args.csv))
    print(f"imported {inserted}, skipped {skipped} duplicates")


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="pt", description="Purchase Tracker")
    ap.add_argument("--version", action="version", version=f"purchase-tracker {__version__}")
    ap.add_argument("--data-dir", help=f"instance data directory (default: ${ENV_VAR} or platform app-data dir)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("where", help="show where instance data lives").set_defaults(fn=cmd_where)
    sub.add_parser("init", help="create the database").set_defaults(fn=cmd_init)

    a = sub.add_parser("add", help="add a purchase manually")
    a.add_argument("--vendor", required=True)
    a.add_argument("--date", required=True, help="YYYY-MM-DD")
    a.add_argument("--item", required=True)
    a.add_argument("--order-id")
    a.add_argument("--qty", type=int, default=1)
    a.add_argument("--price", type=float, help="unit price")
    a.add_argument("--total", type=float)
    a.add_argument("--category")
    a.add_argument("--notes")
    a.set_defaults(fn=cmd_add)

    l = sub.add_parser("list", help="list recent purchases")
    l.add_argument("--vendor")
    l.add_argument("--limit", type=int, default=50)
    l.set_defaults(fn=cmd_list)

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
