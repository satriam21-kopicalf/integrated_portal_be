from app import menu_costing as mc
from app.aggregator_quality import menu_key, missing_menus, price_spread, topic
from app.running_stock import mix
from app.stock_list_month import unexplained


def test_the_topic_of_a_review():
    assert topic("sedotannya ga ada") == "Pesanan kurang / salah" and topic("tumpah semua") == "Kemasan / tumpah"
    assert topic("") == "Tanpa komentar" and topic("oke") == "Lainnya"


def test_prices_that_differ_between_outlets():
    rows = [{"name": n, "price": p, "outlet": o, "platform": "GoFood"} for n, p, o in
            (("Es Kopi", 10, "A"), ("es  kopi", 10, "B"), ("Es-Kopi", 12, "C"), ("Teh", 5, "A"), ("Teh", 5, "B"), ("Teh", 5, "C"))]
    out = price_spread(rows)
    assert len(out) == 1 and out[0]["usual"] == 10 and out[0]["who"] == [{"outlet": "C", "price": 12.0}] and menu_key("Es-Kopi ") == "es kopi"


def test_a_menu_most_outlets_have():
    rows = [{"name": "Es Kopi", "outlet": o, "platform": "GoFood", "price": 1} for o in "ABCDE"] + [{"name": "Teh", "outlet": o, "platform": "GoFood", "price": 1} for o in "ABCD"]
    assert [(x["outlet"], x["menu"]) for x in missing_menus(rows)] == [("E", "Teh")]


def test_esb_usage_first_then_the_rest():
    out = mix({"MILK": (1000.0, "GR")}, {"MILK": {"code": "MILK", "qty": 9, "unit": "KG"}, "CLIP": {"code": "CLIP", "qty": 2, "unit": "PCS"}},
              {"MILK": {"code": "MILK", "qty": 0.5, "unit": "KG"}}, {}, mc.to_unit)
    assert out["MILK"]["qty"] == 1500 and out["CLIP"]["qty"] == 2


def test_items_nothing_explains():
    rows = [{"code": "CLIP", "system": 0.0, "actual": 30}, {"code": "MILK", "system": 5.0, "actual": 6}, {"code": "IDLE", "system": 0.0, "actual": 0}]
    assert unexplained(rows) == {"CLIP"}


def test_ticket_done_needs_the_handling_and_evidence():
    from app import aggregator_quality as q
    full = {"action": "REFUND", "root_cause": "OUTLET", "contacted": True, "reply_text": None}
    assert q.done_missing("Refund Rp 20.000", full, 1) == []
    assert q.done_missing("ok", full, 0) == ["lampirkan minimal 1 bukti penanganan"]
    assert "tulis balasan ulasan" in q.done_missing("ok", {**full, "action": "REPLY"}, 1)
    assert len(q.done_missing(None, {"contacted": None}, 0)) == 5
