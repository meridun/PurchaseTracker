import pytest

from purchase_tracker import cli
from purchase_tracker.db import connect, init_db
from purchase_tracker.importers.statement import detect, import_csv
from purchase_tracker.models import Account, add_transaction, list_transactions


@pytest.fixture
def conn(tmp_path):
    c = connect(tmp_path / "t.db")
    init_db(c)
    Account(name="Visa", party_id=1, kind="credit").insert(c)
    return c


def _csv(tmp_path, name, text):
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return p


def _amounts(conn):
    return [(t["posted_date"], t["amount"], t["description"]) for t in list_transactions(conn)]


SAMPLES = {
    # profile -> (csv text, expected newest-first rows)
    "chase": (
        "Transaction Date,Post Date,Description,Category,Type,Amount,Memo\n"
        "09/28/2026,09/30/2026,AMAZON.COM*AB12CD34,Shopping,Sale,-87.32,\n"
        "09/25/2026,09/26/2026,Payment Thank You-Mobile,,Payment,500.00,\n",
        [("2026-09-28", -87.32, "AMAZON.COM*AB12CD34"), ("2026-09-25", 500.0, "Payment Thank You-Mobile")],
    ),
    "amex": (
        "Date,Description,Card Member,Account #,Amount,Reference\n"
        "09/28/2026,AMAZON MARKETPLACE,J DOE,-11111,87.32,'320262710001'\n"
        "09/25/2026,ONLINE PAYMENT - THANK YOU,J DOE,-11111,-500.00,'320262710002'\n",
        [("2026-09-28", -87.32, "AMAZON MARKETPLACE"), ("2026-09-25", 500.0, "ONLINE PAYMENT - THANK YOU")],
    ),
    "capitalone": (
        "Transaction Date,Posted Date,Card No.,Description,Category,Debit,Credit\n"
        "2026-09-28,2026-09-30,1234,AMAZON.COM,Merchandise,87.32,\n"
        "2026-09-25,2026-09-25,1234,CAPITAL ONE MOBILE PYMT,Payment/Credit,,500.00\n",
        [("2026-09-28", -87.32, "AMAZON.COM"), ("2026-09-25", 500.0, "CAPITAL ONE MOBILE PYMT")],
    ),
    "discover": (
        "Trans. Date,Post Date,Description,Amount,Category\n"
        "09/28/2026,09/30/2026,AMAZON.COM,87.32,Merchandise\n"
        "09/25/2026,09/25/2026,INTERNET PAYMENT - THANK YOU,-500.00,Payments and Credits\n",
        [("2026-09-28", -87.32, "AMAZON.COM"), ("2026-09-25", 500.0, "INTERNET PAYMENT - THANK YOU")],
    ),
    "citi": (
        "Status,Date,Description,Debit,Credit,Member Name\n"
        "Cleared,09/28/2026,AMAZON.COM,87.32,,J DOE\n"
        "Cleared,09/25/2026,AUTOPAY PAYMENT,,-500.00,J DOE\n",
        # Citi reports credits as negative numbers in the Credit column; profile normalises
        [("2026-09-28", -87.32, "AMAZON.COM"), ("2026-09-25", 500.0, "AUTOPAY PAYMENT")],
    ),
    "bofa": (
        "Posted Date,Reference Number,Payee,Address,Amount\n"
        "09/28/2026,24692166271000001,AMAZON.COM,\"SEATTLE WA\",-87.32\n",
        [("2026-09-28", -87.32, "AMAZON.COM")],
    ),
    "generic": (
        "Date,Description,Amount\n2026-09-28,Amazon,\"($87.32)\"\n",
        [("2026-09-28", -87.32, "Amazon")],
    ),
}


@pytest.mark.parametrize("profile", list(SAMPLES))
def test_profiles_detect_and_normalise(conn, tmp_path, profile):
    text, expected = SAMPLES[profile]
    path = _csv(tmp_path, f"{profile}.csv", text)
    r = import_csv(conn, path, 1)
    assert r.profile == profile
    assert (r.new, r.skipped) == (len(expected), 0)
    assert _amounts(conn) == expected
    r = import_csv(conn, path, 1)                      # idempotent
    assert (r.new, r.skipped) == (0, len(expected))


def test_identical_lines_same_day_both_import(conn, tmp_path):
    path = _csv(tmp_path, "d.csv",
                "Date,Description,Amount\n2026-09-28,Coffee,-4.50\n2026-09-28,Coffee,-4.50\n")
    assert import_csv(conn, path, 1).new == 2
    assert import_csv(conn, path, 1).skipped == 2


def test_flip_sign_and_explicit_format(conn, tmp_path):
    path = _csv(tmp_path, "g.csv", "Date,Description,Amount\n2026-09-28,Shop,12.00\n")
    import_csv(conn, path, 1, fmt="generic", flip_sign=True)
    assert list_transactions(conn)[0]["amount"] == -12.0


def test_unknown_format_errors(tmp_path):
    with pytest.raises(ValueError, match="unrecognised"):
        detect(["Foo", "Bar"])


def test_manual_transaction_dedupe(conn):
    assert add_transaction(conn, "visa", "2026-10-01", -20, "Pharmacy") is not None
    assert add_transaction(conn, "visa", "2026-10-01", -20, "Pharmacy") is None
    assert add_transaction(conn, "visa", "2026-10-01", -20, "Pharmacy", ref="2") is not None
    with pytest.raises(ValueError):
        add_transaction(conn, "nope", "2026-10-01", -20, "x")


def test_unmatched_filter(conn):
    t = add_transaction(conn, "Visa", "2026-10-01", -20, "Shop")
    add_transaction(conn, "Visa", "2026-10-02", 20, "Refund")
    assert [r["id"] for r in list_transactions(conn, unmatched=True)] == [t.id]
    conn.execute("INSERT INTO orders (vendor, order_date) VALUES ('x','2026-10-01')")
    conn.execute("INSERT INTO order_payments VALUES (1, ?, 20)", (t.id,))
    assert list_transactions(conn, unmatched=True) == []


def test_cli_accounts_tx_and_import(tmp_path, capsys):
    d = str(tmp_path / "data")
    cli.main(["--data-dir", d, "account", "add", "Mom checking", "--party", "Mom", "--kind", "checking"])
    cli.main(["--data-dir", d, "tx", "add", "--account", "Mom checking", "--date", "2026-10-05",
              "--amount", "-45.10", "--desc", "Walgreens"])
    path = _csv(tmp_path, "s.csv", SAMPLES["chase"][0])
    cli.main(["--data-dir", d, "import-statement", str(path), "--account", "Mom checking"])
    cli.main(["--data-dir", d, "accounts"])
    cli.main(["--data-dir", d, "tx", "list", "--unmatched"])
    out = capsys.readouterr().out
    assert "format chase: 2 new, 0 already present" in out
    assert "Walgreens" in out and "AMAZON.COM*AB12CD34" in out
    assert "Payment Thank You" not in out.split("matched")[1]   # credits excluded from --unmatched
    with pytest.raises(SystemExit):
        cli.main(["--data-dir", d, "tx", "add", "--account", "Mom checking", "--date", "2026-10-05",
                  "--amount", "-45.10", "--desc", "Walgreens"])
