"""Damaged Equipment (app/damaged_equipment.py): flow, time tracker, checks before sending, page access, share link, PDF."""
from datetime import datetime, timedelta, timezone

from app import access
from app import damaged_equipment as de
from tests.test_auth import anon, login, store  # noqa: F401 (fixtures)

T0 = datetime(2026, 10, 21, 1, 0, tzinfo=timezone.utc)


def _row(**kw):
    base = {"id": "11111111-1111-1111-1111-111111111111", "number": "DE-2610-0001", "kind": "REPAIR", "status": "PENDING",
            "hold_from": None, "user_id": "u", "username": "pic", "full_name": "PIC Satu", "branch_code": "CCI01",
            "branch_name": "Kopi Calf Test", "analysis": "Grinder mati", "cause": "WEAR", "urgency": "CRITICAL", "latitude": None,
            "longitude": None, "accuracy_m": None, "distance_m": 12, "outside_radius": False, "signed_name": "PIC Satu",
            "signed_at": T0, "has_signature": True, "share_token": "tok", "share_expires_at": T0 + timedelta(days=30), "est_cost": None,
            "actual_cost": None, "vendor": None, "duplicate_of": None, "duplicate_number": None, "created_at": T0, "updated_at": T0,
            "submitted_at": T0, "closed_at": None}
    return {**base, **kw}


def test_pages_replace_the_tools_skeleton():
    keys = set(access.PAGE_KEYS)
    assert {"form.damaged-equipment", "mon.damaged-equipment"} <= keys and not {"form.tools", "mon.tools"} & keys
    assert "form.damaged-equipment" in access.DEFAULT_ROLE_PAGES["user"]  # the PICs fill it in, like the other forms


def test_flow_and_resume_after_hold():
    assert de.allowed_next(_row(status="PENDING")) == ["VALIDATION", "ON_HOLD", "REJECTED"]
    assert de.allowed_next(_row(status="IN_PROGRESS")) == ["HANDOVER", "ON_HOLD"]
    assert de.allowed_next(_row(status="HANDOVER")) == ["DONE", "IN_PROGRESS", "ON_HOLD"]
    assert de.allowed_next(_row(status="ON_HOLD", hold_from="REVIEW")) == ["REVIEW", "REJECTED"]  # resumes where it stopped
    assert de.allowed_next(_row(status="DONE")) == []


def test_status_rules(monkeypatch):
    monkeypatch.setattr(de, "recompute", lambda rid: None)
    row = _row(status="PENDING")
    monkeypatch.setattr(de, "get_row", lambda rid: row)
    monkeypatch.setattr(de.db, "fetchrow", lambda q, p=None: {"n": 0})
    for to, note, why in (("DONE", None, "tidak bisa langsung"), ("REJECTED", " ", "Alasan wajib")):
        try:
            de.change_status({"id": "x", "username": "t"}, row["id"], to, note)
            raise AssertionError(to)
        except de.DeError as e:
            assert why in str(e)
    row.update(status="ACCEPTED")
    try:
        de.change_status({"id": "x", "username": "t"}, row["id"], "IN_PROGRESS", None)
        raise AssertionError("needs a technician")
    except de.DeError as e:
        assert "Tugaskan tukang" in str(e)


def test_time_tracker_counts_hold_apart_and_sla():
    ev = [{"kind": "SUBMIT", "at": T0.isoformat(), "to": "PENDING"},
          {"kind": "STATUS", "at": (T0 + timedelta(hours=2)).isoformat(), "to": "VALIDATION"},
          {"kind": "STATUS", "at": (T0 + timedelta(hours=3)).isoformat(), "to": "ON_HOLD"},
          {"kind": "STATUS", "at": (T0 + timedelta(hours=13)).isoformat(), "to": "VALIDATION"},
          {"kind": "STATUS", "at": (T0 + timedelta(hours=14)).isoformat(), "to": "REVIEW"}]
    t = de.tracker(_row(status="REVIEW", closed_at=T0 + timedelta(hours=40)), ev)  # closed_at = "now" of the test
    hours = {s["status"]: s["hours"] for s in t["stages"]}
    assert hours == {"PENDING": 2.0, "VALIDATION": 2.0, "ON_HOLD": 10.0, "REVIEW": 26.0}
    assert t["totalHours"] == 40 and t["holdHours"] == 10 and t["workHours"] == 30
    assert t["slaHours"] == 24 and t["overdue"] is True and t["progress"] == 33  # critical: 24 h without the hold
    assert t["dueAt"] == (T0 + timedelta(hours=34)).isoformat() and t["targetAdjusted"] is False
    adj = de.tracker(_row(status="REVIEW", closed_at=T0 + timedelta(hours=40), target_at=T0 + timedelta(hours=60)), ev)
    assert adj["overdue"] is False and adj["targetAdjusted"] is True and adj["slaHours"] == 50  # 60 h to the target minus 10 h hold


def test_checks_before_sending_and_video_types():
    assert de.missing(_row(analysis=" "), [], []) == ["Tambahkan minimal satu item", "Isi analisis kerusakan", "Lampirkan minimal satu foto"]
    assert de.missing(_row(kind="PROCUREMENT"), [{"x": 1}], [{"kind": "VIDEO"}]) == ["Lampirkan minimal satu foto area di outlet"]
    assert de._video_type(b"\x00\x00\x00\x18ftypisom" + b"\x00" * 8, "a.mp4") == ("video/mp4", "mp4")
    assert de._video_type(b"\x00\x00\x00\x14ftypqt  " + b"\x00" * 8, "a.mov") == ("video/quicktime", "mov")
    try:
        de._video_type(b"MZ\x90\x00not a video", "a.exe")
        raise AssertionError("an executable is no video")
    except de.DeError:
        pass


def test_endpoints_follow_their_pages(anon, store, page_access, monkeypatch):
    monkeypatch.setattr(de, "form_state", lambda user: {"requests": [], "branches": []})
    monkeypatch.setattr(de, "monitor", lambda *a, **k: {"data": []})
    login(anon, "kasir", "Kasir1234")  # role user: fills the form, no monitoring
    assert anon.get("/api/de/form").status_code == 200
    assert anon.get("/api/de/monitor").status_code == 403
    assert anon.get("/api/de/technicians").status_code == 403
    page_access.overrides[str(store.user["id"])] = {"form.damaged-equipment": False, "mon.damaged-equipment": True}
    assert anon.get("/api/de/form").status_code == 403
    assert anon.get("/api/de/monitor").status_code == 200


def test_share_link_is_public_and_expires(anon, monkeypatch):
    monkeypatch.setattr(de, "by_token", lambda token: None)
    assert anon.get("/api/public/de/unknown").status_code == 404   # no sign-in needed, an unknown / expired token is 404
    assert anon.get("/api/public/de/unknown/pdf").status_code == 404


def test_pdf_document(monkeypatch, tmp_path):
    from app import de_pdf
    r = de.view(_row(), full=False)
    r.update(items=[{"group": "Peralatan Bar & Dapur", "category": "Mesin Kopi & Grinder", "name": "Grinder", "qty": 1,
                     "location": "Bar", "urgency": "CRITICAL", "note": "Burr tidak berputar"}],
             media=[], events=[{"kind": "SUBMIT", "at": T0.isoformat(), "from": None, "to": "PENDING", "actor": "PIC Satu", "note": None, "details": {}}],
             assignments=[], tracker=de.tracker(_row(), []))
    monkeypatch.setattr(de, "full", lambda rid: r)
    monkeypatch.setattr(de, "signature_of", lambda rid: None)
    data, name = de_pdf.build(r["id"], "https://portal.kopicalf.co.id/share/de/tok")
    assert name == "DE-2610-0001.pdf" and data.startswith(b"%PDF") and len(data) > 1500


def test_work_link_rules(monkeypatch):
    now = de._now()
    base = {"submitted_at": None, "revoked_at": None, "expires_at": now + timedelta(minutes=30)}
    assert de._link_state(base) == "OPEN"
    assert de._link_state({**base, "expires_at": now - timedelta(minutes=1)}) == "EXPIRED"
    assert de._link_state({**base, "revoked_at": now}) == "CLOSED"
    assert de._link_state({**base, "submitted_at": now}) == "SENT"
    monkeypatch.setattr(de, "get_row", lambda rid: _row(status="ACCEPTED"))
    try:
        de.create_work_link({"id": "x", "username": "t"}, "r", None)
        raise AssertionError("only while in progress")
    except de.DeError:
        pass
    link = {"id": 1, "revoked_at": None, "expires_at": now + timedelta(minutes=30), "submitted_at": None, "status": "IN_PROGRESS"}
    monkeypatch.setattr(de.db, "fetchrow", lambda q, p=None: {**link, "n": 0} if "count" in q else link)
    try:
        de.work_submit("tok", None)
        raise AssertionError("at least one photo")
    except de.DeError as e:
        assert "minimal 1 foto" in str(e)
    monkeypatch.setattr(de.db, "fetchrow", lambda q, p=None: {**link, "expires_at": now - timedelta(seconds=1)})
    try:
        de.work_view("tok")
        raise AssertionError("expired")
    except LookupError:
        pass


def test_pdf_events_read_well():
    from app import de_pdf
    assert de_pdf._event_text({"kind": "WORK", "details": {"link": "sent", "photos": 3}}) == "Foto pengerjaan dikirim tukang · 3 foto"
    assert de_pdf._event_text({"kind": "COST", "details": {"invoice": "a.pdf"}}) == "Invoice dilampirkan · a.pdf"
    assert de_pdf._hours(0.5) == "30 menit"


def test_invoice_types_and_wages(monkeypatch):
    assert de._invoice_type(b"%PDF-1.7 rest") == ("application/pdf", "pdf")
    assert de._invoice_type(b"\xff\xd8\xff\xe0 jpeg") == ("image/jpeg", "jpg")
    for bad in (b"MZ\x90\x00", b"<html>"):
        try:
            de._invoice_type(bad)
            raise AssertionError(bad)
        except de.DeError:
            pass
    monkeypatch.setattr(de, "get_row", lambda rid: _row(status="REJECTED"))
    try:
        de.add_invoice({"id": "x", "username": "t"}, "r", b"%PDF-1", "a.pdf", None)
        raise AssertionError("no invoice on a rejected request")
    except de.DeError:
        pass
    shared = de.without_invoices({"media": [{"kind": "PHOTO"}, {"kind": "INVOICE"}],
                                  "events": [{"kind": "COST", "details": {"invoice": "a.pdf"}}, {"kind": "COST", "details": {}}]})
    assert shared["media"] == [{"kind": "PHOTO"}] and shared["events"] == [{"kind": "COST", "details": {}}]
    for body in ({"name": "A", "kind": "INTERNAL", "wageType": "YEARLY"}, {"name": "A", "kind": "INTERNAL", "wageAmount": -1}):
        try:
            de.save_technician(None, body)
            raise AssertionError(body)
        except de.DeError:
            pass


def test_handover_and_vendor_rules(monkeypatch):
    monkeypatch.setattr(de, "recompute", lambda rid: None)
    rows = {"r": _row(status="HANDOVER", mode="VENDOR", handover_at=None, actual_cost=None, est_cost=500000)}
    monkeypatch.setattr(de, "get_row", lambda rid: rows["r"])
    monkeypatch.setattr(de.db, "fetchrow", lambda q, p=None: {"n": 1})
    u = {"id": "x", "username": "t"}
    for row, why in ((rows["r"], "Serah terima dari PIC belum dikirim"),
                     ({**rows["r"], "handover_at": T0}, "biaya aktual dan lampirkan invoice")):
        rows["r"] = row
        try:
            de.change_status(u, "r", "DONE", None)
            raise AssertionError(why)
        except de.DeError as e:
            assert why in str(e)
    rows["r"] = _row(status="REVIEW", mode="VENDOR", est_cost=None)
    try:
        de.change_status(u, "r", "ACCEPTED", None)
        raise AssertionError("vendor estimate")
    except de.DeError as e:
        assert "estimasi biaya vendor" in str(e)
    rows["r"] = _row(status="IN_PROGRESS", mode="INTERNAL")
    monkeypatch.setattr(de.db, "fetchrow", lambda q, p=None: {"n": 1 if "de_assignment" in q else 0})
    try:
        de.change_status(u, "r", "HANDOVER", None)
        raise AssertionError("internal needs the technician's photos")
    except de.DeError as e:
        assert "Foto hasil pekerjaan dari tukang" in str(e)
    # the PIC's handover: photos, both signatures for a vendor
    monkeypatch.setattr(de, "own_sent", lambda user, rid: _row(status="HANDOVER", mode="VENDOR", handover_at=None))
    monkeypatch.setattr(de.db, "fetchrow", lambda q, p=None: {"n": 0})
    try:
        de.submit_handover(u, "r", None, None, None, "data:x", "PIC", None, None, None)
        raise AssertionError("photo")
    except de.DeError as e:
        assert "minimal 1 foto" in str(e)
    monkeypatch.setattr(de.db, "fetchrow", lambda q, p=None: {"n": 2})
    monkeypatch.setattr(de, "parse_photo", lambda v: ("image/png", b"x"))
    try:
        de.submit_handover(u, "r", None, None, None, "data:x", "PIC", None, None, None)
        raise AssertionError("vendor signature")
    except de.DeError as e:
        assert "tanda tangan dan nama petugas vendor" in str(e)


def test_target_needs_a_reason(monkeypatch):
    monkeypatch.setattr(de, "get_row", lambda rid: _row(status="IN_PROGRESS"))
    try:
        de.set_target({"id": "x", "username": "t"}, "r", T0 + timedelta(days=2), " ")
        raise AssertionError("reason")
    except de.DeError as e:
        assert "Alasan" in str(e)


def test_labor_units():
    assert de._units("HOURLY", 2.2) == 2.5 and de._units("DAILY", 25) == 2 and de._units("WEEKLY", 200) == 2
    assert de._units("MONTHLY", 10) == 1 and de._units("PER_JOB", 0) == 1 and de._units("DAILY", 0) == 0
