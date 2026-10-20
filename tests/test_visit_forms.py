"""Monitoring Product / Outlet forms (app/visit_forms.py, app/routes/visit_forms.py): the items of the workbook
"Form Visit", the submit rules (every item rated; UNDER needs a note and a photo) and the access of each endpoint."""
from app import visit_forms as vf
from tests.test_auth import anon, login, store  # noqa: F401 (fixtures)


def test_items_follow_the_form_visit_workbook():
    product, outlet = vf.all_items("product"), vf.all_items("outlet")
    assert len(product) == 19 + 19 + 6 + 16 + 5 + 9 == 74
    assert len(outlet) == 10 + 5 + 4 + 3 + 4 + 15 + 8 + 4 == 53
    assert [g["title"] for g in vf.DEFINITIONS["product"]["groups"]] == ["Availability", "Kualitas Produk"]
    assert [g["title"] for g in vf.DEFINITIONS["outlet"]["groups"]][0] == "Kebersihan dan Rating Outlet"
    assert vf.DEFINITIONS["outlet"]["groups"][-1]["title"] == "Kenyamanan Outlet"
    assert len({i["key"] for i in product}) == 74 and len({i["key"] for i in outlet}) == 53
    late = next(i for i in outlet if i["label"] == "Nama Barista Telat")
    assert late["kind"] == "text"  # a name, not a rating


def test_counts_and_what_blocks_the_submit():
    items = [i for i in vf.all_items("outlet") if i["kind"] == "rating"]
    answers = {i["key"]: {"rating": "MEET"} for i in items}
    answers[items[0]["key"]] = {"rating": "UNDER"}
    answers[items[1]["key"]] = {"rating": "EXCEED"}
    c = vf._counts("outlet", answers)
    assert (c["under"], c["exceed"], c["rated"], c["total"]) == (1, 1, 52, 52) and c["score"] == round(51 * 100 / 52, 1)
    todo = vf.missing("outlet", answers, [])
    assert todo == [{"key": items[0]["key"], "label": items[0]["label"], "need": "note+photo"}]
    answers[items[0]["key"]]["note"] = "Lantai bar basah"
    assert vf.missing("outlet", answers, [{"item": items[0]["key"]}]) == []
    del answers[items[5]["key"]]
    assert vf.missing("outlet", answers, [{"item": items[0]["key"]}])[0]["need"] == "rating"


def test_answers_are_checked_and_merged(monkeypatch):
    saved = {}
    monkeypatch.setattr(vf, "own_draft", lambda form, user, vid: {"answers": {"rating.1": {"rating": "MEET"}}})
    monkeypatch.setattr(vf.db, "fetchrow", lambda q, p=None: saved.update(p=p) or {"id": "x"})
    out = vf.save_answers("outlet", {"id": "u"}, "v", {"rating.1": {"note": " Rating 4,2 "}, "kebersihan.2": {"rating": "UNDER"}})
    assert out["answers"]["rating.1"] == {"rating": "MEET", "note": "Rating 4,2"} and out["counts"]["under"] == 1
    for bad in ({"nope.1": {"rating": "MEET"}}, {"rating.1": {"rating": "GOOD"}}, {"barista.3": {"rating": "MEET"}}):
        try:
            vf.save_answers("outlet", {"id": "u"}, "v", bad)
            raise AssertionError(bad)
        except vf.VisitError:
            pass


def test_endpoints_follow_their_pages(anon, store, page_access, monkeypatch):
    monkeypatch.setattr(vf, "form_state", lambda form, user: {"definition": vf.DEFINITIONS[form], "branches": [], "drafts": [], "recent": []})
    monkeypatch.setattr(vf, "monitor", lambda *a, **k: {"data": [], "summary": {}})
    monkeypatch.setattr(vf, "open_visit", lambda form, user, code: (_ for _ in ()).throw(vf.VisitError("Cabang tidak tersedia untuk akun Anda")))
    login(anon, "kasir", "Kasir1234")  # role user: fills both forms, no monitoring
    assert anon.get("/api/forms/product").json()["definition"]["title"] == "Monitoring Product"
    r = anon.post("/api/forms/product/visits", json={"branchCode": "CTG54"})
    assert r.status_code == 422 and "Cabang" in r.json()["error"]
    assert anon.get("/api/monitoring/product").status_code == 403
    assert anon.get("/api/visit-photos/00000000-0000-0000-0000-000000000000").status_code in (403, 404)
    page_access.overrides[str(store.user["id"])] = {"form.product": False, "mon.product": True}
    assert anon.get("/api/forms/product").status_code == 403
    assert anon.get("/api/monitoring/product").status_code == 200
    assert anon.get("/api/monitoring/outlet").status_code == 403  # one monitoring page does not open the other
