import json

import pytest

from purchase_tracker import cli
from purchase_tracker.db import SCHEMA_VERSION, connect, init_db, schema_version
from purchase_tracker.importers.amazon import import_file, import_json, import_transactions_json
from purchase_tracker.models import Account, Transaction, charges_for_order, list_orders, reconcile


@pytest.fixture
def conn(tmp_path):
    c = connect(tmp_path / "t.db")
    init_db(c)
    return c


ORDERS = {"kind": "amazon-orders", "data": [
    {"order_ref": "112-1", "order_date": "October 7, 2026", "grand_total": 46.85, "subtotal": 43.0,
     "items": [{"asin": "B1", "title": "Thing", "quantity": 1, "unit_price": 43.0, "line_price": 43.0}],
     "invoice_html": "<html><body>Order 112-1 invoice</body></html>"},
]}

TX = {"kind": "amazon-transactions", "data": [
    {"date": "2026-10-08", "instrument": "Discover ****1234", "amount": -5.00, "status": "Completed",
     "order_ref": "112-1", "merchant": "Amazon Tips"},
    {"date": "2026-10-07", "instrument": "Discover ****1234", "amount": -41.85, "status": "Completed",
     "order_ref": "112-1", "merchant": "AMZN Mktp US"},
    {"date": "2026-10-13", "instrument": "Discover ****1234", "amount": -16.95, "status": "Pending",
     "order_ref": "112-9", "merchant": "AMZN Mktp US"},
]}


def _write(tmp_path, name, obj):
    p = tmp_path / name
    p.write_text(json.dumps(obj), encoding="utf-8")
    return p


def test_schema_is_v4(conn):
    assert schema_version(conn) == SCHEMA_VERSION == 4


def test_invoice_html_saved_and_referenced(conn, tmp_path):
    docs = tmp_path / "documents"
    r = import_json(conn, _write(tmp_path, "o.json", ORDERS), docs_dir=docs)
    assert (r.orders_new, r.documents_saved) == (1, 1)
    o = list_orders(conn)[0]
    assert o["document_ref"] == "amazon/112-1.html"
    assert (docs / "amazon" / "112-1.html").read_text(encoding="utf-8").startswith("<html>")
    r = import_json(conn, _write(tmp_path, "o.json", ORDERS), docs_dir=docs)
    assert r.documents_saved == 0          # not rewritten


def test_transactions_import_and_pending_update(conn, tmp_path):
    r = import_transactions_json(conn, _write(tmp_path, "t.json", TX))
    assert (r.charges_new, r.charges_updated) == (3, 0)
    rows = conn.execute("SELECT order_ref, amount, last4, status FROM vendor_charges ORDER BY id").fetchall()
    assert [tuple(x) for x in rows] == [("112-1", -5.0, "1234", "completed"),
                                        ("112-1", -41.85, "1234", "completed"),
                                        ("112-9", -16.95, "1234", "pending")]
    completed = json.loads(json.dumps(TX))
    completed["data"][2]["status"] = "Completed"
    completed["data"][2]["date"] = "2026-10-14"
    r = import_transactions_json(conn, _write(tmp_path, "t2.json", completed))
    assert (r.charges_new, r.charges_updated) == (0, 3)
    row = conn.execute("SELECT status, charge_date FROM vendor_charges WHERE order_ref = '112-9'").fetchone()
    assert tuple(row) == ("completed", "2026-10-14")


def test_reconcile_links_split_charges_to_order_and_bank(conn, tmp_path):
    acct = Account(name="Discover", party_id=1, kind="credit", last4="1234").insert(conn)
    other = Account(name="Visa", party_id=1, kind="credit", last4="1111").insert(conn)
    t1 = Transaction(account_id=acct, posted_date="2026-10-09", amount=-41.85, external_id="a").insert(conn)
    t2 = Transaction(account_id=acct, posted_date="2026-10-10", amount=-5.00, external_id="b").insert(conn)
    Transaction(account_id=other, posted_date="2026-10-09", amount=-41.85, external_id="c").insert(conn)  # wrong card
    Transaction(account_id=acct, posted_date="2026-11-09", amount=-41.85, external_id="d").insert(conn)   # too late

    import_transactions_json(conn, _write(tmp_path, "t.json", TX))
    rr = reconcile(conn)
    assert (rr.linked_transactions, rr.linked_orders, rr.charges_without_order) == (2, 0, 2)

    import_json(conn, _write(tmp_path, "o.json", ORDERS))
    rr = reconcile(conn)
    assert (rr.linked_transactions, rr.linked_orders) == (0, 2)
    oid = list_orders(conn)[0]["id"]
    pays = conn.execute("SELECT transaction_id, amount FROM order_payments WHERE order_id = ? ORDER BY amount",
                        (oid,)).fetchall()
    assert [tuple(p) for p in pays] == [(t2, 5.0), (t1, 41.85)]
    assert sum(p["amount"] for p in pays) == pytest.approx(46.85)
    assert [c["transaction_id"] for c in charges_for_order(conn, oid)] == [t1, t2]
    assert reconcile(conn).linked_orders == 0   # idempotent


def test_import_file_dispatch(conn, tmp_path):
    assert import_file(conn, _write(tmp_path, "t.json", TX)).kind == "transactions"
    assert import_file(conn, _write(tmp_path, "o.json", ORDERS)).kind == "orders"
    with pytest.raises(ValueError):
        import_json(conn, _write(tmp_path, "t.json", TX))


def test_cli_import_and_show(tmp_path, capsys):
    d = tmp_path / "data"
    cli.main(["--data-dir", str(d), "account", "add", "Discover", "--last4", "1234"])
    cli.main(["--data-dir", str(d), "tx", "add", "--account", "Discover", "--date", "2026-10-09",
              "--amount", "-41.85", "--desc", "AMAZON MKTPLACE"])
    cli.main(["--data-dir", str(d), "import-amazon", str(_write(tmp_path, "o.json", ORDERS))])
    cli.main(["--data-dir", str(d), "import-amazon", str(_write(tmp_path, "t.json", TX))])
    cli.main(["--data-dir", str(d), "show", "1"])
    out = capsys.readouterr().out
    assert "invoices saved: 1" in out
    assert "charges: 3 new" in out
    assert "receipt: amazon/112-1.html" in out
    assert "-41.85" in out and "matched Discover 2026-10-09" in out
    assert "-5.00" in out and "no bank line" in out
    assert (d / "documents" / "amazon" / "112-1.html").exists()
