"""Matching of person accounts to HRMS employees (app/hrms_sync.py)."""
from app.hrms_sync import _index, match, norm_name


def emp(i, name, status="kontrak", email=None, no=None):
    return {"id": i, "full_name": name, "status": status, "email": email, "employee_no": no,
            "job_title": "Store Supervisor", "department": "Operation"}


EMPS = [emp("e1", "Budi  Santoso"), emp("e2", "Andi", email="andi@calf.id"), emp("e3", "Andi"),
        emp("e4", "Rina", status="keluar"), emp("e5", "Rina"), emp("e6", "Dewi", status="keluar"),
        emp("e7", "Dewi", status="keluar"), emp("e8", "Tono", no="K-99")]
IDX = _index(EMPS)


def acc(name, email=None, no=None, linked=None):
    return {"full_name": name, "email": email, "employee_number": no, "hrms_employee_id": linked}


def test_norm_name():
    assert norm_name("  Budi   SANTOSO.") == "budi santoso"
    assert norm_name("Rénata") == "renata"


def test_name_match():
    m = match(acc("budi santoso"), IDX)
    assert (m["by"], m["employee"]["id"]) == ("name", "e1")


def test_name_prefers_working_employee():
    assert match(acc("Rina"), IDX)["employee"]["id"] == "e5"


def test_shared_name_falls_back_to_email():
    m = match(acc("Andi", email="ANDI@calf.id"), IDX)
    assert (m["by"], m["employee"]["id"]) == ("email", "e2")


def test_shared_name_without_email_is_ambiguous():
    m = match(acc("Dewi"), IDX)
    assert m["employee"] is None and m["by"] == "ambiguous" and len(m["candidates"]) == 2


def test_employee_number_and_none():
    assert match(acc("T. Tono", no="k-99"), IDX)["by"] == "employee_no"
    assert match(acc("Nobody"), IDX)["by"] == "none"


def test_saved_link_wins():
    m = match(acc("Budi Santoso", linked="e8"), IDX)
    assert (m["by"], m["employee"]["id"]) == ("linked", "e8")
