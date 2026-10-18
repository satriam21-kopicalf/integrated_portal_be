"""Forms & Monitoring skeleton (app/routes/monitoring.py): each form and monitoring page opens its own endpoints."""
from app import access
from tests.test_auth import anon, login, store  # noqa: F401 (fixtures)

FORMS = ("pic-check-in", "outlet", "product", "tools")


def test_pages_in_the_catalog_and_off_by_default():
    keys = set(access.PAGE_KEYS)
    for f in FORMS:
        assert f"form.{f}" in keys and f"mon.{f}" in keys
    for pages in access.DEFAULT_ROLE_PAGES.values():
        assert not any(p.startswith(("form.", "mon.")) for p in pages)
    assert not any(p.startswith(("form.", "mon.")) for p in access.CC_PAGES)


def test_endpoints_follow_their_pages(anon, store, page_access):
    login(anon, "kasir", "Kasir1234")  # role user, branch CCI01
    for f in FORMS:
        assert anon.get(f"/api/forms/{f}").status_code == 403
        assert anon.get(f"/api/monitoring/{f}").status_code == 403
    page_access.overrides[str(store.user["id"])] = {"form.pic-check-in": True}
    r = anon.get("/api/forms/pic-check-in")
    assert r.status_code == 200 and r.json()["branches"] == ["CCI01"]
    assert anon.post("/api/forms/pic-check-in").status_code == 501
    assert anon.get("/api/forms/outlet").status_code == 403  # one form does not open the others
    assert anon.get("/api/monitoring/pic-check-in").status_code == 403  # filling in does not open monitoring
    page_access.overrides[str(store.user["id"])] = {"mon.tools": True}
    assert anon.get("/api/monitoring/tools").status_code == 200
    assert anon.get("/api/monitoring/other").status_code == 404
    login(anon, "superadmin", "Admin1234")
    assert all(anon.get(f"/api/monitoring/{f}").status_code == 200 for f in FORMS)
