"""Waste skeleton (app/routes/waste.py): every endpoint follows its page, like the menu (app/access.py)."""
from app import access
from tests.test_auth import anon, login, store  # noqa: F401 (fixtures)


def test_waste_pages_in_the_catalog():
    assert {"waste", "cc.waste-monitor", "cc.waste-review"} <= set(access.PAGE_KEYS)
    assert {"cc.waste-monitor", "cc.waste-review"} <= set(access.CC_PAGES)
    # the PICs (role user) and the outlet / hub accounts (role outlet) open the input (2026-10-28)
    assert "waste" in access.DEFAULT_ROLE_PAGES["user"] and access.DEFAULT_ROLE_PAGES["outlet"] == ["stock-opname", "waste", "form.pengajuan"]
    assert {"waste", "cc.waste-monitor", "cc.waste-review"} <= set(access.DEFAULT_ROLE_PAGES["cost_control"])


def test_waste_endpoints_follow_their_pages(anon, store, page_access):
    login(anon, "kasir", "Kasir1234")  # role user (a PIC), branch CCI01: the waste input is a page of the role
    for path in ("/api/waste/monitor", "/api/waste/reviews"):
        assert anon.get(path).status_code == 403
    r = anon.get("/api/waste/entries")
    assert r.status_code == 200 and r.json()["branches"] == ["CCI01"] and r.json()["data"] == []
    assert anon.get("/api/waste/monitor").status_code == 403  # entering waste does not open Cost Control
    page_access.overrides[str(store.user["id"])] = {"waste": False, "cc.waste-monitor": True, "cc.waste-review": True}
    assert anon.get("/api/waste/monitor").status_code == 200
    assert anon.get("/api/waste/reviews").status_code == 200
    assert anon.post("/api/waste/reviews/x").status_code == 501
    assert anon.get("/api/waste/entries").status_code == 403
    login(anon, "superadmin", "Admin1234")
    assert anon.get("/api/waste/entries").json()["branches"] is None  # every branch
