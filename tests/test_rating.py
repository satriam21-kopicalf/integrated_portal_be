"""Rating & Review skeleton (app/routes/rating.py): each source opens with its own page, off by default."""
from app import access
from tests.test_auth import anon, login, store  # noqa: F401 (fixtures)

SOURCES = ("google-maps", "shopeefood", "gofood", "grabfood")


def test_rating_pages_off_by_default():
    assert {f"rating.{s}" for s in SOURCES} <= set(access.PAGE_KEYS)
    for pages in access.DEFAULT_ROLE_PAGES.values():
        assert not any(p.startswith("rating.") for p in pages)


def test_rating_endpoints_follow_their_pages(anon, store, page_access):
    login(anon, "kasir", "Kasir1234")  # role user, branch CCI01
    assert all(anon.get(f"/api/rating/{s}").status_code == 403 for s in SOURCES)
    page_access.overrides[str(store.user["id"])] = {"rating.gofood": True}
    r = anon.get("/api/rating/gofood")
    assert r.status_code == 200 and r.json()["branches"] == ["CCI01"]
    assert anon.get("/api/rating/grabfood").status_code == 403
    assert anon.get("/api/rating/tiktok").status_code == 404
    login(anon, "superadmin", "Admin1234")
    assert all(anon.get(f"/api/rating/{s}").status_code == 200 for s in SOURCES)
