"""Form WW-WR (app/wwwr.py): one form with every item of Product and Outlet, the SWAT team only, and the comparison of
an audit with the PIC's visits."""
from datetime import datetime, timezone

from app import visit_forms as vf, wwwr


def test_wwwr_has_every_item_of_product_and_outlet_prefixed():
    items = vf.all_items("wwwr")
    assert len(items) == len(vf.all_items("product")) + len(vf.all_items("outlet")) == 148
    assert items[0]["key"] == "product:stok-preparation.1" and items[-1]["key"].startswith("outlet:administrasi-finance.")
    assert [g["part"] for g in vf.DEFINITIONS["wwwr"]["groups"]][:2] == ["product", "product"]
    assert vf.DEFINITIONS["wwwr"]["groups"][0]["title"] == "Product · Availability"


def test_only_the_team_fills_an_audit(monkeypatch):
    monkeypatch.setattr(vf, "audit_of", lambda vid: {"id": "a", "lead_id": "u1", "status": "scheduled", "members": ["u1", "u2"], "day": None})
    row = {"id": "v1", "form": "wwwr", "user_id": "u1"}
    assert vf.can_fill(row, {"id": "u2"}) and not vf.can_fill(row, {"id": "u3"})
    assert vf.can_fill({"id": "v2", "form": "product", "user_id": "u3"}, {"id": "u3"})


def test_audit_against_the_pic_item_by_item():
    keys = [it["key"] for it in vf.all_items("product") if it["kind"] == "rating"][:4]
    swat = {"answers": {f"product:{keys[0]}": {"rating": "UNDER"}, f"product:{keys[1]}": {"rating": "MEET"},
                        f"product:{keys[2]}": {"rating": "UNDER"}, f"product:{keys[3]}": {"rating": "EXCEED"}}}
    pic = {"id": "p1", "user_id": "u9", "full_name": "Andi", "username": "andi", "score": 100,
           "submitted_at": datetime(2026, 11, 3, tzinfo=timezone.utc),
           "answers": {keys[0]: {"rating": "MEET"}, keys[1]: {"rating": "EXCEED"}, keys[2]: {"rating": "UNDER"}, keys[3]: {"rating": "UNDER"}}}
    out = wwwr._compare(swat, "product", pic, with_items=True)
    assert (out["pic"]["compared"], out["pic"]["agree"], out["pic"]["overrated"], out["pic"]["underrated"]) == (4, 2, 1, 1)
    assert out["pic"]["conformity"] == 50.0 and out["swatScore"] == 50.0 and out["swatUnder"] == 2
    assert [r["verdict"] for r in out["items"][:4]] == ["overrated", "agree", "agree", "underrated"]
    assert wwwr._compare(swat, "product", None)["pic"] is None
