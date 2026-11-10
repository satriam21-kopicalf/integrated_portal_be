"""Monitoring Product / Outlet forms (app/visit_forms.py, app/routes/visit_forms.py): the items of the workbook
"Form Visit", the submit rules (every item rated; UNDER needs a note and a photo) and the access of each endpoint."""
from app import visit_forms as vf
import pytest

from tests.test_auth import anon, login, store  # noqa: F401 (fixtures)


@pytest.fixture(autouse=True)
def no_group(monkeypatch):
    """No database here: an outlet without a branch group unless a test sets one."""
    monkeypatch.setattr(vf, "group_of", lambda code: None)


def test_items_follow_the_form_visit_workbook():
    product, outlet = vf.all_items("product"), vf.all_items("outlet")
    assert len(product) == 19 + 19 + 6 + 16 + 5 + 9 == 74
    # Outlet Cleanliness = the Travel Path SOP (2026-10-30), 21 points in 5 zones since Ambience, Grooming & Kesiapan
    # Crew and Cupping & Test Food Daily were removed (2026-11-02)
    assert len(outlet) == 21 + 5 + 4 + 3 + 4 + 15 + 8 + 3 + 7 + 1 == 71  # + Administrasi & Finance (2026-10-21); − Merchandise (2026-11-04); + Utensil Tools (2026-11-09)
    assert not {"Ambience", "Grooming & Kesiapan Crew", "Cupping & Test Food Daily"} & {i["label"] for i in outlet}
    optional = {i["label"]: vf.is_required(i, None) for i in product + outlet}
    assert optional["Cupping Menu Specialty"] is False and optional["Mesin Espresso, Grinder & Kalibrasi"] is False
    assert [g["title"] for g in vf.DEFINITIONS["product"]["groups"]] == ["Ketersediaan", "Kualitas Produk"]
    assert [g["title"] for g in vf.DEFINITIONS["outlet"]["groups"]][0] == "Kebersihan dan Rating Outlet"
    assert [g["title"] for g in vf.DEFINITIONS["outlet"]["groups"]][-2:] == ["Kenyamanan Outlet", "Administrasi & Finance"]
    assert len({i["key"] for i in product}) == 74 and len({i["key"] for i in outlet}) == 71
    late = next(i for i in outlet if i["label"] == "Nama Barista Telat")
    assert late["kind"] == "text" and late["required"] is False  # a name, not a rating; optional
    # every item, section and group has its English text (ENG / IDN, 2026-10-27)
    for form in ("product", "outlet"):
        for g in vf.DEFINITIONS[form]["groups"]:
            assert g["titleEn"] and g["subtitleEn"]
            for sec in g["sections"]:
                assert sec["titleEn"] and all(i["labelEn"] for i in sec["items"])
                # the Travel Path points carry their photo points and standard in English too (2026-11-02)
                assert all(i.get("detailEn") and i.get("standardEn") for i in sec["items"] if i.get("detail"))
    assert any(i["label"] == "SO Daily" for i in outlet) and not any(i["label"] == "SO Packaging" for i in outlet)
    tp = [i for i in outlet if i["section"].startswith("tp-")]
    assert len(tp) == 22 and all(i["detail"] and i["standard"] and i["category"] in ("Kritikal", "Mayor", "Minor") for i in tp)
    assert sum(i["category"] == "Kritikal" for i in tp) == 10 and all(i["photo"] == "required" for i in tp)


def _required(form, group):
    return {i["label"] for i in vf.all_items(form) if i["kind"] == "rating" and vf.is_required(i, group)}


def test_required_items_per_branch_group():
    """Operations 2026-10-27: the core premixes, espresso and request forms are required; Beans Specialty only at CTGP
    and CCI; the rest optional."""
    ctg, ctgp, cci = _required("product", "CTG"), _required("product", "CTGP"), _required("product", "CCI")
    assert len(ctg) == 11 * 2 + 3 + 10 + 3 + 5 == 43  # Cupping Menu Specialty optional (2026-11-02) and ctgp == cci == ctg | {"Ketersediaan Stok Beans Specialty"}
    for label in ("Ketersediaan Stok Premix Premium", "Label Shelf Life Cream Base", "Ketersediaan Stok Espresso Regular",
                  "Ketersediaan Stok Beans Cold Brew", "Kesesuaian Form Request Bahan Baku Side Dish", "Cupping Premium"):
        assert label in ctg, label
    for label in ("Ketersediaan Stok Premix Jefferson", "Ketersediaan Stok Beans Premium", "Ketersediaan Cup Jefferson",
                  "Ketersediaan Set Spork", "Test Food Menu Side Dish", "Kesesuaian Form Request Ice Hot, Rich Gold"):
        assert label not in ctg, label
    outlet = _required("outlet", "CTG")
    assert len(outlet) == 14 + 5 + 2 + 3 + 4 + 10 + 6 + 3 + 6 + 1 == 54  # Travel Path: 15 required (Utensil Tools, 2026-11-09), the rest an outlet may lack
    for label in ("Kedisiplinan Barista", "Lap Gelas", "Sabun Toilet", "Kondisi Merchandise", "Form Self Pickup", "Mesin Espresso, Grinder & Kalibrasi"):
        assert label not in outlet, label


def test_counts_and_what_blocks_the_submit():
    items = [i for i in vf.all_items("outlet") if i["kind"] == "rating"]
    answers = {i["key"]: {"rating": "MEET"} for i in items}
    answers[items[0]["key"]] = {"rating": "UNDER"}
    answers[items[1]["key"]] = {"rating": "EXCEED"}
    c = vf._counts("outlet", answers)
    assert (c["under"], c["exceed"], c["rated"], c["total"]) == (1, 1, 70, 70) and c["score"] == round(69 * 100 / 70, 1)
    required = [i for i in items if vf.is_required(i, "CTG") and not i.get("platform")]
    todo = vf.missing("outlet", answers, [], "CTG")
    by_key = {t["key"]: t["need"] for t in todo}
    assert len(todo) == len(required) and by_key[items[0]["key"]] == "photo"  # Travel Path: one photo per point
    speed = next(i for i in items if i["label"] == "Speed of Service")
    assert by_key[speed["key"]] == "note"  # a note, no photo needed
    assert all(i["key"] not in by_key for i in items if not vf.is_required(i, "CTG"))  # optional: no evidence needed
    for i in items:
        answers[i["key"]]["note"] = "Catatan"
    photos = [{"item": i["key"]} for i in items]
    assert vf.missing("outlet", answers, photos, "CTG") == []
    optional = next(i for i in items if not vf.is_required(i, "CTG"))
    del answers[optional["key"]]
    assert vf.missing("outlet", answers, photos, "CTG") == []  # an optional item may stay unrated
    del answers[required[0]["key"]]
    assert vf.missing("outlet", answers, photos, "CTG") == [
        {"key": required[0]["key"], "label": required[0]["label"], "labelEn": required[0]["labelEn"], "need": "rating"}]


def test_beans_specialty_is_required_at_ctgp_and_cci_only():
    items = [i for i in vf.all_items("product") if i["kind"] == "rating"]
    answers = {i["key"]: {"rating": "MEET", "note": "ok", **({"qty": 500} if i.get("qty") else {}), **({"link": "https://x"} if i.get("link") else {})} for i in items}
    photos = [{"item": i["key"]} for i in items]
    beans = next(i for i in items if i["label"] == "Ketersediaan Stok Beans Specialty")
    del answers[beans["key"]]
    assert vf.missing("product", answers, photos, "CTG") == []
    assert [t["key"] for t in vf.missing("product", answers, photos, "CTGP")] == [beans["key"]]
    assert [t["key"] for t in vf.missing("product", answers, photos, "CCI")] == [beans["key"]]


def test_grams_of_stock_preparation_espresso_and_beans(monkeypatch):
    """Operations 2026-10-28: Stock Preparation, Espresso and Beans take the stock in grams (net weight); a rated
    required item needs it, an optional one not; 0 g is a valid count."""
    items = vf.all_items("product")
    with_qty = {i["section"] for i in items if i.get("qty") == "g"}
    assert with_qty == {"stok-preparation", "espresso-beans"}
    assert all(i.get("qty") == "g" for i in items if i["section"] in with_qty)
    premix = next(i for i in items if i["label"] == "Ketersediaan Stok Premix Premium")
    jefferson = next(i for i in items if i["label"] == "Ketersediaan Stok Premix Jefferson")
    shelf = next(i for i in items if i["label"] == "Label Shelf Life Premix Premium")
    rated = {k["key"]: {"rating": "MEET", "note": "ok"} for k in (premix, jefferson, shelf)}
    photos = [{"item": k} for k in rated]
    need = {t["key"]: t["need"] for t in vf.missing("product", rated, photos, "CTG")}
    assert need[premix["key"]] == "qty" and jefferson["key"] not in need and shelf["key"] not in need
    rated[premix["key"]]["qty"] = 0
    assert premix["key"] not in {t["key"] for t in vf.missing("product", rated, photos, "CTG")}

    monkeypatch.setattr(vf, "own_draft", lambda form, user, vid: {"answers": {}, "branch_code": "CTG54"})
    monkeypatch.setattr(vf.db, "fetchrow", lambda q, p=None: {"id": "x"})
    out = vf.save_answers("product", {"id": "u"}, "v", {premix["key"]: {"rating": "MEET", "qty": "1250.46"}})
    assert out["answers"][premix["key"]] == {"rating": "MEET", "qty": 1250.5}
    assert out["answers"] == vf.save_answers("product", {"id": "u"}, "v", {premix["key"]: {"rating": "MEET", "qty": 1250.5}})["answers"]
    for bad in ({premix["key"]: {"qty": -1}}, {premix["key"]: {"qty": "x"}}, {premix["key"]: {"qty": 2_000_000}},
                {shelf["key"]: {"qty": 10}}):
        try:
            vf.save_answers("product", {"id": "u"}, "v", bad)
            raise AssertionError(bad)
        except vf.VisitError:
            pass


def test_answers_are_checked_and_merged(monkeypatch):
    saved = {}
    monkeypatch.setattr(vf, "own_draft", lambda form, user, vid: {"answers": {"rating.1": {"rating": "MEET"}}, "branch_code": "CTG54"})
    monkeypatch.setattr(vf.db, "fetchrow", lambda q, p=None: saved.update(p=p) or {"id": "x"})
    out = vf.save_answers("outlet", {"id": "u"}, "v", {"rating.1": {"note": " Rating 4,2 "}, "tp-customer.1": {"rating": "UNDER"}})
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
    monkeypatch.setattr(vf, "missing", lambda *a: [])  # the required items are another test
    monkeypatch.setattr(vf, "group_of", lambda code: "CTG")
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
    assert anon.get("/api/forms/product").json()["definition"]["title"] == "Product"
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
    monkeypatch.setattr(vf, "missing", lambda *a: [])
    monkeypatch.setattr(vf, "group_of", lambda code: "CCI")
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


def test_the_pic_filling_a_form_never_gets_the_score():
    from app.routes.visit_forms import _no_score
    state = {"recent": [{"id": "v1", "score": 87.5, "counts": {"under": 1, "rated": 8, "total": 8, "score": 87.5}}],
             "drafts": [{"counts": {"rated": 2, "total": 8, "score": None}}], "user": {"name": "Andi"}}
    assert _no_score(state) == {"recent": [{"id": "v1", "counts": {"under": 1, "rated": 8, "total": 8}}],
                                "drafts": [{"counts": {"rated": 2, "total": 8}}], "user": {"name": "Andi"}}


def test_items_not_asked_at_ctg_outlets():
    """2026-11-02 (Store Performance reff.: ctg 0): some optional items are asked only at CTGP / CCI outlets; a CTG
    outlet does not see them and they are not counted there."""
    items = {i["label"]: i for i in vf.all_items("product")}
    for label in ("Ketersediaan Stok Beans Premium", "Ketersediaan Cup Jefferson", "Ketersediaan Straw 6 mm",
                  "Kesesuaian Form Request Bahan Baku Main Course", "Ketersediaan Stok Beans Specialty"):
        assert not vf.applies(items[label], "CTG") and vf.applies(items[label], "CCI") and vf.applies(items[label], None)
    assert vf.is_required(items["Ketersediaan Stok Beans Specialty"], "CCI") and not vf.is_required(items["Ketersediaan Stok Beans Specialty"], "CTG")
    rated = sum(1 for i in vf.all_items("product") if i["kind"] == "rating")
    assert vf._counts("product", {}, "CTG")["total"] == rated - 7 and vf._counts("product", {}, "CCI")["total"] == rated
    assert any(i["key"].startswith("product:") and i.get("onlyFor") for i in vf.all_items("wwwr"))


def test_grabfood_rating_is_read_but_the_pic_may_change_it(monkeypatch):
    """2026-11-09: GrabFood's official rating fills the Outlet form; a figure typed by the PIC stays."""
    monkeypatch.setattr(vf, "grab_rating", lambda code: {"stars": 4.9, "reviews": 2106, "source": "grabfood", "fetchedAt": "2026-11-07"})
    assert vf.AUTO_RATING["grabfood"]("CTG27")["stars"] == 4.9
    assert {it["platform"] for it in vf._auto_items("outlet")} == {"google", "grabfood"}


def test_raw_materials_need_a_note_only():
    """2026-11-10: the Raw Materials items ask a note only — no photo, no link (the link of 2026-11-09 is gone)."""
    raw = [i for i in vf.all_items("product") if i["key"].startswith("bahan-baku.")]
    assert raw and all(i["note"] == "required" and i["photo"] == "none" and not i.get("link") for i in raw)
    first = raw[0]
    assert [t["need"] for t in vf.missing("product", {first["key"]: {"rating": "MEET"}}, [], "CTG") if t["key"] == first["key"]] == ["note"]
    dimsum = next(i for i in vf.all_items("product") if i["label"] == "Ketersediaan Stok Dimsum")
    assert dimsum["units"] == ["gram", "porsi", "pax"] and vf.MAX_PHOTOS_PER_ITEM == 5
