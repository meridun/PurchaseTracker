import sqlite3

import pytest

from purchase_tracker import cli
from purchase_tracker.config import ENV_VAR, resolve_data_dir
from purchase_tracker.db import _V1, SCHEMA_VERSION, connect, init_db, schema_version
from purchase_tracker.importers.amazon import import_csv
from purchase_tracker.models import (
    Item, Order, allocate, create_claim, items_for_order, list_orders, list_parties, unclaimed,
)


@pytest.fixture
def conn(tmp_path):
    c = connect(tmp_path / "t.db")
    init_db(c)
    return c


def test_data_dir_resolution(tmp_path, monkeypatch):
    monkeypatch.setenv(ENV_VAR, str(tmp_path / "env"))
    assert resolve_data_dir() == (tmp_path / "env").resolve()
    assert resolve_data_dir(tmp_path / "explicit") == (tmp_path / "explicit").resolve()


def test_fresh_db_is_current_and_seeded(conn):
    assert schema_version(conn) == SCHEMA_VERSION
    assert [p.name for p in list_parties(conn)] == ["Me", "Mom", "HSA"]
    assert init_db(conn) == 0  # idempotent


def test_upgrade_from_v1(tmp_path):
    path = tmp_path / "old.db"
    c = sqlite3.connect(path)
    c.executescript("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);" + _V1)
    c.execute("INSERT INTO meta VALUES ('schema_version','1')")
    c.executemany(
        "INSERT INTO purchases (vendor, order_id, order_date, item_name, quantity, total) VALUES (?,?,?,?,?,?)",
        [("amazon", "A-1", "2026-01-01", "Cable", 1, 5.0),
         ("amazon", "A-1", "2026-01-01", "Mouse", 1, 20.0),
         ("cvs", None, "2026-01-02", "Aspirin", 2, 8.0)],
    )
    c.commit(); c.close()

    conn = connect(path)
    assert init_db(conn) == 1
    assert schema_version(conn) == SCHEMA_VERSION
    orders = list_orders(conn)
    assert {(o["vendor"], o["n_items"], o["total"]) for o in orders} == {("amazon", 2, 25.0), ("cvs", 1, 8.0)}
    assert conn.execute("SELECT name FROM sqlite_master WHERE name='purchases'").fetchone() is None


def test_order_dedupe_and_items(conn):
    o = Order(vendor="amazon", order_ref="111-1", order_date="2026-01-02")
    oid, created = o.get_or_insert(conn)
    assert created
    oid2, created2 = Order(vendor="amazon", order_ref="111-1", order_date="2026-01-02").get_or_insert(conn)
    assert (oid2, created2) == (oid, False)
    assert Item(order_id=oid, name="Widget", line_key="w", total=9.99).insert(conn) is not None
    assert Item(order_id=oid, name="Widget", line_key="w", total=9.99).insert(conn) is None
    assert len(items_for_order(conn, oid)) == 1


def test_allocation_and_claim_flow(conn):
    oid, _ = Order(vendor="amazon", order_ref="222", order_date="2026-02-01", total=100).get_or_insert(conn)
    iid = Item(order_id=oid, name="Pills", line_key="p", total=100).insert(conn)

    a1 = allocate(conn, iid, "Mom", 60)
    with pytest.raises(ValueError):
        allocate(conn, iid, "HSA", 50)  # only 40 left
    a2 = allocate(conn, iid, "HSA")      # takes the remainder
    assert a2.amount == 40

    item = items_for_order(conn, oid)[0]
    assert (item["allocated"], item["unallocated"]) == (100, 0)
    assert {r["party"] for r in unclaimed(conn)} == {"Mom", "HSA"}

    claim = create_claim(conn, "Mom", title="Feb")
    t = conn.execute("SELECT * FROM v_claim_totals WHERE claim_id = ?", (claim.id,)).fetchone()
    assert (t["party"], t["n_allocations"], t["total"], t["status"]) == ("Mom", 1, 60, "draft")
    assert [r["allocation_id"] for r in unclaimed(conn)] == [a2.id]
    assert a1.id not in [r["allocation_id"] for r in unclaimed(conn)]


def test_amazon_import_groups_lines_into_orders(conn, tmp_path):
    csv_path = tmp_path / "orders.csv"
    csv_path.write_text(
        "Order ID,Order Date,ASIN,Product Name,Quantity,Unit Price,Total Owed,Currency\n"
        "112-1,2026-03-04T15:22:10Z,B001,USB-C Cable,2,$7.49,$14.98,USD\n"
        "112-1,2026-03-04T15:22:10Z,B002,Coffee Beans,1,12.00,12.00,USD\n"
        "112-1,2026-03-04T15:22:10Z,B002,Coffee Beans,1,12.00,12.00,USD\n"  # genuine repeat line
        "112-2,03/05/2026,,Mystery Item,1,3.00,3.00,USD\n",
        encoding="utf-8",
    )
    r = import_csv(conn, csv_path)
    assert (r.orders_new, r.items_new, r.items_skipped) == (2, 4, 0)
    r = import_csv(conn, csv_path)
    assert (r.orders_new, r.items_new, r.items_skipped) == (0, 0, 4)
    orders = {o["order_ref"]: o for o in list_orders(conn)}
    assert orders["112-1"]["n_items"] == 3 and orders["112-1"]["total"] == pytest.approx(38.98)
    assert orders["112-2"]["order_date"] == "2026-03-05"


def test_cli_roundtrip(tmp_path, capsys):
    d = str(tmp_path / "data")
    cli.main(["--data-dir", d, "add", "--vendor", "walmart", "--date", "2026-02-01",
              "--item", "Batteries", "--price", "4.5", "--qty", "2"])
    cli.main(["--data-dir", d, "allocate", "1", "mom"])
    cli.main(["--data-dir", d, "unclaimed"])
    cli.main(["--data-dir", d, "claim", "new", "Mom", "--title", "test"])
    cli.main(["--data-dir", d, "claim", "submit", "1", "--date", "2026-02-10"])
    cli.main(["--data-dir", d, "claim", "paid", "1", "--date", "2026-02-20"])
    cli.main(["--data-dir", d, "claim", "show", "1"])
    cli.main(["--data-dir", d, "show", "1"])
    out = capsys.readouterr().out
    assert "Batteries" in out and "9.00" in out
    assert "paid       2026-02-20" in out and "paid 9.00" in out
    with pytest.raises(SystemExit):           # paid is terminal
        cli.main(["--data-dir", d, "claim", "reopen", "1"])


def test_claim_lifecycle(conn):
    from purchase_tracker.models import (
        claim_add_allocations, claim_remove_allocations, delete_allocation, get_claim,
        list_claims, pay_claim, reject_claim, reopen_claim, submit_claim,
    )
    oid, _ = Order(vendor="cvs", order_ref="c1", order_date="2026-03-01", total=50).get_or_insert(conn)
    i1 = Item(order_id=oid, name="Rx", line_key="a", total=30).insert(conn)
    i2 = Item(order_id=oid, name="Bandages", line_key="b", total=20).insert(conn)
    a1 = allocate(conn, i1, "HSA")
    a2 = allocate(conn, i2, "HSA")

    c = create_claim(conn, "HSA", allocation_ids=[a1.id])
    assert claim_add_allocations(conn, c.id, [a2.id]) == 1
    assert claim_remove_allocations(conn, c.id, [a2.id]) == 1
    assert claim_add_allocations(conn, c.id, [a2.id]) == 1
    assert claim_add_allocations(conn, c.id, [a2.id]) == 0   # already attached

    with pytest.raises(ValueError):            # can't pay a draft
        pay_claim(conn, c.id)
    submit_claim(conn, c.id, date="2026-03-10", document_ref="fc:123")
    c = get_claim(conn, c.id)
    assert (c.status, c.submitted_at, c.document_ref) == ("submitted", "2026-03-10", "fc:123")
    with pytest.raises(ValueError):            # locked once submitted
        claim_remove_allocations(conn, c.id, [a2.id])
    with pytest.raises(ValueError):
        delete_allocation(conn, a1.id)

    reject_claim(conn, c.id, notes="missing receipt")
    assert get_claim(conn, c.id).status == "rejected"
    reopen_claim(conn, c.id)
    c = get_claim(conn, c.id)
    assert (c.status, c.submitted_at) == ("draft", None)

    submit_claim(conn, c.id)
    pay_claim(conn, c.id, date="2026-04-01")
    c = get_claim(conn, c.id)
    assert (c.status, c.paid_amount, c.paid_at) == ("paid", 50.0, "2026-04-01")
    with pytest.raises(ValueError):            # paid is terminal
        reopen_claim(conn, c.id)
    assert [r["status"] for r in list_claims(conn, "hsa")] == ["paid"]
    assert unclaimed(conn) == []


def test_pay_claim_links_transaction(conn):
    from purchase_tracker.models import Account, Transaction, get_claim, pay_claim, submit_claim
    oid, _ = Order(vendor="x", order_ref="1", order_date="2026-01-01").get_or_insert(conn)
    iid = Item(order_id=oid, name="Thing", line_key="t", total=25).insert(conn)
    allocate(conn, iid, "Mom")
    c = create_claim(conn, "Mom")
    submit_claim(conn, c.id)
    acct = Account(name="My checking", party_id=1, kind="checking").insert(conn)
    tid = Transaction(account_id=acct, posted_date="2026-01-15", amount=25.0, external_id="dep1",
                      description="Zelle from Mom").insert(conn)
    pay_claim(conn, c.id, transaction_id=tid)
    c = get_claim(conn, c.id)
    assert (c.paid_amount, c.paid_transaction_id) == (25.0, tid)


def test_pay_claim_rejects_unknown_transaction(conn):
    from purchase_tracker.models import pay_claim, submit_claim
    oid, _ = Order(vendor="x", order_ref="2", order_date="2026-01-01").get_or_insert(conn)
    iid = Item(order_id=oid, name="Thing", line_key="t", total=5).insert(conn)
    allocate(conn, iid, "Mom")
    c = create_claim(conn, "Mom")
    submit_claim(conn, c.id)
    with pytest.raises(ValueError):
        pay_claim(conn, c.id, transaction_id=999)
