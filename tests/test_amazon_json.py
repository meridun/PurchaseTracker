import json

import pytest

from purchase_tracker.db import SCHEMA_VERSION, connect, init_db, schema_version
from purchase_tracker.importers.amazon import _apportion, import_csv, import_json
from purchase_tracker.models import items_for_order, list_orders


@pytest.fixture
def conn(tmp_path):
    c = connect(tmp_path / "t.db")
    init_db(c)
    return c


SAMPLE = [
    {"order_ref": "112-1", "order_date": "October 13, 2026", "ship_to": "Pat Example",
     "payment_method": "Discover 1234", "subtotal": 48.47, "shipping": 2.99, "tax": 2.91,
     "grand_total": 51.38, "hsa_eligible_total": 24.89,
     "items": [
         {"asin": "B004M39G7O", "title": "Depend", "sold_by": "Amazon.com", "quantity": 1,
          "unit_price": 23.48, "line_price": 23.48, "hsa_eligible": True},
         {"asin": "B07YD4XCT3", "title": "Vacplus", "sold_by": "Vacplus-BAYL", "quantity": 1,
          "unit_price": 24.99, "line_price": 24.99, "hsa_eligible": False},
     ]},
    {"order_ref": "112-2", "order_date": "October 8, 2026", "ship_to": "Sam Example",
     "subtotal": 2.99, "shipping": 0.75, "tax": 0.08, "grand_total": 3.82,
     "items": [{"asin": "B0CQ8MPDQT", "title": "Mac & Cheese", "quantity": 1,
                "unit_price": 2.99, "line_price": 2.99, "hsa_eligible": False}]},
]


def test_schema_is_v3(conn):
    assert schema_version(conn) == SCHEMA_VERSION == 3


def test_apportion_sums_exactly():
    assert _apportion(51.38, [23.48, 24.99]) == [24.89, 26.49]
    assert sum(_apportion(10.0, [3.33, 3.33, 3.33])) == pytest.approx(10.0)
    assert _apportion(None, [1.0, 2.0]) == [1.0, 2.0]
    assert _apportion(5.0, [None, 2.0]) == [None, 5.0]


def test_import_json(conn, tmp_path):
    p = tmp_path / "orders.json"
    p.write_text(json.dumps(SAMPLE), encoding="utf-8")
    r = import_json(conn, p)
    assert (r.orders_new, r.items_new, r.items_skipped) == (2, 3, 0)
    r = import_json(conn, p)
    assert (r.orders_new, r.orders_seen, r.items_new, r.items_skipped) == (0, 2, 0, 3)

    orders = {o["order_ref"]: o for o in list_orders(conn)}
    o = orders["112-1"]
    assert (o["order_date"], o["ship_to"], o["total"], o["hsa_eligible_total"]) == \
        ("2026-10-13", "Pat Example", 51.38, 24.89)
    items = items_for_order(conn, o["id"])
    # tax+shipping apportioned so item totals sum to the grand total; Amazon's own
    # HSA-eligible figure (24.89 incl. tax) matches the apportioned Depend line.
    assert [i["total"] for i in items] == [24.89, 26.49]
    assert [i["unit_price"] for i in items] == [23.48, 24.99]
    assert [i["hsa_eligible"] for i in items] == [1, 0]
    assert items[0]["notes"] == "Sold by: Amazon.com"


def test_json_enriches_order_first_seen_in_csv(conn, tmp_path):
    csv_path = tmp_path / "o.csv"
    csv_path.write_text("Order ID,Order Date,ASIN,Product Name,Quantity,Unit Price,Total Owed\n"
                        "112-1,2026-10-13,B004M39G7O,Depend,1,23.48,24.89\n", encoding="utf-8")
    import_csv(conn, csv_path)
    p = tmp_path / "orders.json"
    p.write_text(json.dumps(SAMPLE[:1]), encoding="utf-8")
    r = import_json(conn, p)
    assert (r.orders_new, r.orders_seen, r.items_new, r.items_skipped) == (0, 1, 1, 1)
    o = list_orders(conn)[0]
    assert (o["ship_to"], o["n_items"], o["tax"]) == ("Pat Example", 2, 2.91)
