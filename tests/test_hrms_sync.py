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


def test_fill_out_of_scope_role_unlinks_and_refuses():
    from app.hrms_sync import fill
    data = {}
    assert fill(data, {"hrms_employee_id": "e1"}, "outlet") is None
    assert data == {"hrmsEmployeeId": None, "hrmsLinkedBy": None}
    assert fill({"hrmsEmployeeId": "e1"}, None, "superadmin")


def test_fill_linked_account_ignores_typed_values():
    from app.hrms_sync import fill
    data = {"jobTitle": "Typed", "department": "Typed", "fullName": "X", "city": "Bogor", "phoneNumber": "0812"}
    current = {"hrms_employee_id": "e1", "job_title": "SS", "department": "Op", "full_name": "Budi", "phone_number": None}
    assert fill(data, current, "user") is None
    assert data == {"city": "Bogor", "phoneNumber": "0812"}  # an empty synced field stays editable


def test_values_and_diff():
    from datetime import date

    from app.hrms_sync import diff, values
    e = {"full_name": " Budi ", "employee_no": "K-1", "gender": "l", "birth_date": date(1990, 1, 2), "phone": "",
         "address": None, "work_branch_code": "CTG18", "job_title": "Store Supervisor", "department": "Operation"}
    v = values(e)
    assert v["fullName"] == "Budi" and v["gender"] == "male" and v["phoneNumber"] is None
    acc_row = {"full_name": "Budi", "employee_number": "K-1", "gender": "male", "birth_date": date(1990, 1, 2),
               "phone_number": "0812", "work_branch_code": "CTG18", "job_title": "Area Supervisor",
               "department": "Operation"}
    assert diff(acc_row, e) == ["jobTitle"]  # an empty HRMS phone keeps the portal one


def test_fill_person_account_needs_its_hrms_employee():
    # migration 115: a person account is made from HRMS; it cannot be created or left without its employee
    from app.hrms_sync import fill
    assert fill({"jobTitle": "Barista"}, {}, "user")                                   # new person, no employee
    assert fill({"hrmsEmployeeId": " "}, {"hrms_employee_id": "e1"}, "user")           # unlink a person
    assert fill({"role": "user"}, {"id": "a", "role": "outlet"}, "user")                # outlet turned into a person
    assert fill({}, {"id": "a", "role": "user", "account_kind": "test"}, "user") is None  # stays test (kind kept)
    assert fill({"accountKind": "person"}, {"id": "a", "role": "user", "account_kind": "shared"}, "user")
    # an existing unlinked account can still be edited, deactivated or get a new password
    assert fill({"isActive": False}, {"id": "a", "role": "user", "account_kind": "person"}, "user") is None
    assert fill({"notes": "x"}, {"hrms_employee_id": "e1", "account_kind": "person"}, "user") is None


def test_fill_shared_and_test_accounts_are_never_linked():
    from app.hrms_sync import fill
    data = {"accountKind": "shared", "jobTitle": "Admin"}
    assert fill(data, {}, "cotr_admin") is None and data == {"accountKind": "shared", "jobTitle": "Admin"}
    data = {"accountKind": "test"}
    assert fill(data, {"hrms_employee_id": "e1"}, "user") is None
    assert data == {"accountKind": "test", "hrmsEmployeeId": None, "hrmsLinkedBy": None}
    assert fill({"accountKind": "test", "hrmsEmployeeId": "e1"}, {}, "user")


def test_profile_complete_without_job_title_when_linked():
    from app.profile import is_complete
    row = {"full_name": "A", "phone_number": "0812", "job_title": None, "department": None}
    assert not is_complete(row)
    assert is_complete({**row, "hrms_employee_id": "e1"})
