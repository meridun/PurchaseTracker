from purchase_tracker import cli
from purchase_tracker.config import ENV_VAR, resolve_data_dir
from purchase_tracker.db import connect, init_db
from purchase_tracker.importers.amazon import import_csv
from purchase_tracker.models import Purchase, list_purchases


def test_data_dir_resolution(tmp_path, monkeypatch):
    monkeypatch.setenv(ENV_VAR, str(tmp_path / "env"))
    assert resolve_data_dir() == (tmp_path / "env").resolve()
    assert resolve_data_dir(tmp_path / "explicit") == (tmp_path / "explicit").resolve()


def test_insert_and_dedupe(tmp_path):
    conn = connect(tmp_path / "t.db")
    init_db(conn)
    p = Purchase(vendor="amazon", order_id="111-1", order_date="2026-01-02", item_name="Widget", total=9.99)
    assert p.insert(conn) is True
    assert p.insert(conn) is False
    assert len(list_purchases(conn)) == 1


def test_amazon_import(tmp_path):
    csv_path = tmp_path / "orders.csv"
    csv_path.write_text(
        "Order ID,Order Date,Product Name,Quantity,Unit Price,Total Owed,Currency\n"
        "112-0000001-0000001,2026-03-04T15:22:10Z,USB-C Cable,2,$7.49,$14.98,USD\n"
        "112-0000001-0000002,03/05/2026,Coffee Beans,1,12.00,12.00,USD\n",
        encoding="utf-8",
    )
    conn = connect(tmp_path / "t.db")
    init_db(conn)
    assert import_csv(conn, csv_path) == (2, 0)
    assert import_csv(conn, csv_path) == (0, 2)
    rows = list_purchases(conn)
    assert rows[0]["order_date"] == "2026-03-05"
    assert rows[1]["total"] == 14.98


def test_cli_roundtrip(tmp_path, capsys):
    d = str(tmp_path / "data")
    cli.main(["--data-dir", d, "add", "--vendor", "walmart", "--date", "2026-02-01",
              "--item", "Batteries", "--price", "4.5", "--qty", "2"])
    cli.main(["--data-dir", d, "list"])
    out = capsys.readouterr().out
    assert "Batteries" in out and "9.00" in out
