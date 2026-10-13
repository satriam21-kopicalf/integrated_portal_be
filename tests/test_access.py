"""Page access per role and per user (app/access.py), job title / department lists, stock opname paths → pages."""
from app import access
from tests.test_auth import anon, login, store  # noqa: F401 (fixtures)
from tests.test_stock_opname import gateway, payload  # noqa: F401 (fixtures)


def test_me_lists_pages_of_the_role(anon, store):
    me = login(anon, "kasir", "Kasir1234").json()["user"]
    assert me["pages"] == ["overview", "sales", "stock-opname", "guide"]
    login(anon, "superadmin", "Admin1234")
    assert anon.get("/api/auth/me").json()["user"]["pages"] == list(access.PAGE_KEYS)


def test_user_exceptions_open_and_close_pages(anon, store, page_access, gateway):
    login(anon, "kasir", "Kasir1234")
    assert anon.get("/api/cost-control/meta").status_code == 403
    assert anon.get("/api/so/counts/monitor").status_code == 403
    assert anon.post("/api/so/counts/x/review", json={}).status_code == 403

    # the superadmin adds COGS and Monitor, and takes Sales away
    login(anon, "superadmin", "Admin1234")
    uid = store.user["id"]
    r = anon.put(f"/api/access/users/{uid}", json={"overrides": {"cc.cogs": True, "cc.monitor": True, "sales": False,
                                                                   "overview": True}})
    assert r.status_code == 200
    assert r.json()["overrides"] == {"cc.cogs": True, "cc.monitor": True, "sales": False}  # overview: already the role's
    assert anon.put(f"/api/access/users/{uid}", json={"overrides": {"nope": True}}).status_code == 422

    login(anon, "kasir", "Kasir1234")
    assert anon.get("/api/auth/me").json()["user"]["pages"] == ["overview", "stock-opname", "guide", "cc.monitor", "cc.cogs"]
    assert anon.get("/api/cost-control/meta").status_code != 403
    assert anon.get("/api/transactions?dateFrom=2026-09-29&dateTo=2026-09-30").status_code == 403
    assert anon.get("/api/so/counts/monitor").status_code == 200
    assert payload(gateway[-1]["headers"])["role"] == "cost_control"  # Cost Control pages: Cost Control in kopicalf
    assert anon.post("/api/so/counts/x/review", json={}).status_code == 403  # no Review page
    assert anon.get("/api/users").status_code == 403  # user management stays superadmin-only


def test_role_pages_are_edited_by_superadmins_only(anon, store):
    login(anon, "kasir", "Kasir1234")
    assert anon.put("/api/access/roles/user", json={"pages": ["overview"]}).status_code == 403
    login(anon, "superadmin", "Admin1234")
    assert anon.put("/api/access/roles/superadmin", json={"pages": []}).status_code == 400
    assert anon.put("/api/access/roles/user", json={"pages": ["overview", "bogus"]}).status_code == 422
    r = anon.put("/api/access/roles/user", json={"pages": ["stock-opname", "overview"]})
    assert r.status_code == 200 and r.json()["roles"]["user"] == ["overview", "stock-opname"]
    pages = anon.get("/api/access/pages").json()
    assert pages["roles"]["user"] == ["overview", "stock-opname"] and len(pages["pages"]) == len(access.PAGE_KEYS)
    login(anon, "kasir", "Kasir1234")
    assert anon.get("/api/transactions?dateFrom=2026-09-29&dateTo=2026-09-30").status_code == 403


def test_user_form_sets_pages_and_registers_new_names(anon, store, page_access):
    login(anon, "superadmin", "Admin1234")
    r = anon.post("/api/users", json={"username": "pic.baru", "email": "pic.baru@kopicalf.co.id", "role": "user",
                                      "branches": ["CCI01"], "password": "Rahasia1234", "jobTitle": "Area  Manager",
                                      "department": "Operations", "pageOverrides": {"cc.ratio": True}})
    assert r.status_code == 201, r.text
    user = r.json()["user"]
    assert user["pageOverrides"] == {"cc.ratio": True} and "cc.ratio" in user["pages"]
    assert page_access.masters == {"jobTitle": ["Area Manager"], "department": ["Operations"]}
    assert anon.post("/api/access/masters/jobTitle", json={"name": "area manager"}).json()["created"] is False
    assert anon.post("/api/access/masters/department", json={"name": "Finance"}).status_code == 201
    assert [m["name"] for m in anon.get("/api/access/masters/department").json()] == ["Operations", "Finance"]
    login(anon, "kasir", "Kasir1234")
    assert anon.post("/api/access/masters/department", json={"name": "X"}).status_code == 403


def test_stock_opname_paths_map_to_pages():
    assert access.so_page("GET", "counts/today") == ("stock-opname",)
    assert set(access.so_page("POST", "counts")) == set(access.so_page("GET", "counts/abc")) - {"cc.reports"}
    assert "stock-opname" in access.so_page("PUT", "counts/abc/lines")
    assert access.so_page("POST", "counts/abc/review") == ("cc.reviews",)
    assert access.so_page("GET", "counts/abc/compare") == ("cc.esb-compare",)
    assert access.so_page("GET", "count-exports/ready") == ("cc.esb-export",)
    assert access.so_page("PUT", "count-settings/lists/1/items") == ("cc.lists", "cc.groups", "cc.types")
    assert "stock-opname" in access.so_page("GET", "count-settings/types")  # names of the count types
    assert access.so_page("PUT", "count-settings/types/X") == ("cc.types",)  # reading names is not editing types
    assert access.so_page("PUT", "recipes/1") == ("cc.premix",)
    assert access.so_page("POST", "counts/entries") == ("cc.entry",)
    assert access.so_page("POST", "counts/imports") == ("cc.entry",)
    assert "cc.entry" in access.so_page("PUT", "counts/abc/lines")
    assert access.so_page("GET", "count-reports/file") == ("cc.reports", "cc.reviews")
    assert "cc.reports" in access.so_page("GET", "counts") and "cc.reports" not in access.so_page("PUT", "counts/abc/lines")
    assert "stock-opname" in access.so_page("GET", "recipes/active")
    assert access.so_page("GET", "something-else") == ()
