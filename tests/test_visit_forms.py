"""Monitoring Product / Outlet forms (app/visit_forms.py, app/routes/visit_forms.py): the items of the workbook
"Form Visit", the submit rules (every item rated; UNDER needs a note and a photo) and the access of each endpoint."""
from app import visit_forms as vf
from tests.test_auth import anon, login, store  # noqa: F401 (fixtures)


def test_items_follow_the_form_visit_workbook():
    product, outlet = vf.all_items("product"), vf.all_items("outlet")
    assert len(product) == 19 + 19 + 6 + 16 + 5 + 9 == 74
    assert len(outlet) == 10 + 5 + 4 + 3 + 4 + 15 + 8 + 4 + 7 == 60  # + Administrasi & Finance (2026-10-21)
    assert [g["title"] for g in vf.DEFINITIONS["product"]["groups"]] == ["Availability", "Kualitas Produk"]
    assert [g["title"] for g in vf.DEFINITIONS["outlet"]["groups"]][0] == "Kebersihan dan Rating Outlet"
    assert [g["title"] for g in vf.DEFINITIONS["outlet"]["groups"]][-2:] == ["Kenyamanan Outlet", "Administrasi & Finance"]
    assert len({i["key"] for i in product}) == 74 and len({i["key"] for i in outlet}) == 60
    late = next(i for i in outlet if i["label"] == "Nama Barista Telat")
    assert late["kind"] == "text"  # a name, not a rating


def test_counts_and_what_blocks_the_submit():
    items = [i for i in vf.all_items("outlet") if i["kind"] == "rating"]
    answers = {i["key"]: {"rating": "MEET"} for i in items}
    answers[items[0]["key"]] = {"rating": "UNDER"}
    answers[items[1]["key"]] = {"rating": "EXCEED"}
    c = vf._counts("outlet", answers)
    assert (c["under"], c["exceed"], c["rated"], c["total"]) == (1, 1, 59, 59) and c["score"] == round(58 * 100 / 59, 1)
    todo = vf.missing("outlet", answers, [])
    assert len(todo) == len([i for i in items if not i.get("platform")]) and todo[0] == {"key": items[0]["key"], "label": items[0]["label"], "need": "note+photo"}  # every rated item (2026-10-23)
    for i in items:
        answers[i["key"]]["note"] = "Catatan"
    photos = [{"item": i["key"]} for i in items]
    assert vf.missing("outlet", answers, photos) == []
    del answers[items[5]["key"]]
    assert vf.missing("outlet", answers, photos[:5] + photos[6:]) == []  # unrated items do not block (2026-10-21)
    assert vf.unrated("outlet", answers) == 1


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


def test_platform_stars_manual_and_google_automatic(monkeypatch):
    monkeypatch.setattr(vf, "done_today", lambda form, uid: {})
    import json
    platforms = {it["key"]: it.get("platform") for it in vf.all_items("outlet") if it["section"] == "rating"}
    assert platforms == {"rating.1": "google", "rating.2": "gofood", "rating.3": "grabfood", "rating.4": "shopeefood", "rating.5": None}
    monkeypatch.setattr(vf, "own_draft", lambda form, user, vid: {"answers": {"rating.1": {"rating": "MEET", "stars": 1.0}}, "branch_code": "CTG54", "branch_name": "Test"})
    saved = {}
    monkeypatch.setattr(vf.db, "fetchrow", lambda q, p=None: saved.update(q=q, p=p) or {"id": "x"})
    out = vf.save_answers("outlet", {"id": "u"}, "v", {"rating.2": {"stars": "4.56", "rating": "MEET"}})
    assert out["answers"]["rating.2"] == {"stars": 4.6, "source": "manual", "rating": "MEET"}
    assert "rating.3" not in vf.save_answers("outlet", {"id": "u"}, "v", {"rating.3": {"stars": None}})["answers"]
    for bad in ({"rating.2": {"stars": 6}}, {"rating.2": {"stars": "x"}}, {"rating.1": {"stars": 4.5}}, {"rating.5": {"stars": 4}}):
        try:
            vf.save_answers("outlet", {"id": "u"}, "v", bad)
            raise AssertionError(bad)
        except vf.VisitError:
            pass
    # sending keeps the Google rating of that moment (a stale figure in the draft is replaced)
    monkeypatch.setattr(vf, "photos_of", lambda vid: [])
    monkeypatch.setattr(vf, "_outlet", lambda code: None)
    monkeypatch.setattr(vf, "full", lambda vid: {"id": vid})
    monkeypatch.setattr(vf, "google_rating", lambda code: {"stars": 4.7, "reviews": 812, "source": "google", "fetchedAt": "2026-10-21T06:00:00+07:00", "mapsUri": "u"})
    vf.submit("outlet", {"id": "u"}, "v", None, None, None, None, None)
    assert json.loads(saved["p"][0])["rating.1"] == {"rating": "MEET", "stars": 4.7, "reviews": 812, "source": "google", "fetchedAt": "2026-10-21T06:00:00+07:00"}


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


def test_signature_is_optional_and_needs_a_name(monkeypatch):
    monkeypatch.setattr(vf, "done_today", lambda form, uid: {})
    from tests.test_pic_checkin import _jpeg
    saved = {}
    monkeypatch.setattr(vf, "own_draft", lambda form, user, vid: {"answers": {"rating.1": {"rating": "MEET"}}, "branch_code": "CCI01", "branch_name": "Test"})
    monkeypatch.setattr(vf, "photos_of", lambda vid: [])
    monkeypatch.setattr(vf, "missing", lambda form, answers, photos: [])
    monkeypatch.setattr(vf, "_outlet", lambda code: None)
    monkeypatch.setattr(vf, "full", lambda vid: {"id": vid})
    monkeypatch.setattr(vf.db, "fetchrow", lambda q, p=None: saved.update(p=p) or {"id": "v"})
    user = {"id": "u"}
    vf.submit("outlet", user, "v", None, None, None, None, None)  # unsigned: fine
    # params end with: signature, signature_type, signed_name, signed (bool), visit id
    assert saved["p"][-5:-1] == (None, None, None, False)
    try:
        vf.submit("outlet", user, "v", None, None, None, None, None, _jpeg(), "  ")
        raise AssertionError("a signature needs a name")
    except vf.VisitError:
        pass
    vf.submit("outlet", user, "v", None, None, None, None, None, _jpeg(), "Budi  Santoso")
    sig, kind, name, signed = saved["p"][-5:-1]
    assert sig.startswith(b"\xff\xd8") and kind == "image/jpeg" and name == "Budi Santoso" and signed is True


def test_unreadable_photo_is_refused_not_an_error(anon, store, monkeypatch):
    monkeypatch.setattr(vf, "own_draft", lambda form, user, vid: {"answers": {}, "branch_code": "CCI01"})
    monkeypatch.setattr(vf.db, "fetchrow", lambda q, p=None: {"n": 0})
    login(anon, "kasir", "Kasir1234")
    r = anon.post("/api/forms/outlet/visits/v/photos", json={"item": "rating.1", "photo": "data:image/png;base64,bm90LWFuLWltYWdlLWF0LWFsbA=="})
    assert r.status_code == 422


def test_one_visit_per_branch_per_day(monkeypatch):
    monkeypatch.setattr(vf, "done_today", lambda form, uid: {"CTG54": {"id": "v", "submittedAt": "x"}})
    try:
        vf._not_today("product", {"id": "u"}, "CTG54", "Kopi Calf Cempaka Putih")
        raise AssertionError("daily")
    except vf.VisitError as e:
        assert "besok" in str(e)
    vf._not_today("product", {"id": "u"}, "CCI01", "Other")  # another branch is fine
