"""Profile fields of a user account: validation shared by "My profile"
(PATCH /api/auth/me) and user management (/api/users).

API field -> column. Values are trimmed; empty strings become NULL.
"""
import re
from datetime import date
from typing import Optional

from app import database as db
from app.database import SCHEMA

PROFILE_FIELDS = {
    "fullName": "full_name",
    "phoneNumber": "phone_number",
    "jobTitle": "job_title",
    "department": "department",
    "employeeNumber": "employee_number",
    "gender": "gender",
    "birthDate": "birth_date",
    "address": "address",
    "city": "city",
    "workBranchCode": "work_branch_code",
}
GENDERS = ("male", "female")
# what "profile complete" means (the UI asks the user to fill these in)
REQUIRED_FOR_COMPLETE = ("full_name", "phone_number", "job_title", "department")


class ProfileError(ValueError):
    def __init__(self, message: str, field: str):
        super().__init__(message)
        self.field = field


def _text(value, limit: int, field: str, label: str) -> Optional[str]:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if len(text) > limit:
        raise ProfileError(f"{label} maksimal {limit} karakter", field)
    return text or None


def branch_exists(code: str) -> bool:
    return db.fetchrow(f"SELECT 1 AS x FROM {SCHEMA}.master_branches WHERE branch_code = %s LIMIT 1", (code,)) is not None


def clean_profile(data: dict) -> dict:
    """Validated profile values for the keys present in `data` (raises ProfileError)."""
    out: dict = {}
    if "fullName" in data:
        out["fullName"] = _text(data["fullName"], 120, "fullName", "Nama lengkap")
    if "phoneNumber" in data:
        phone = _text(data["phoneNumber"], 32, "phoneNumber", "Nomor telepon")
        if phone and not re.fullmatch(r"[0-9+()\-\s]{6,32}", phone):
            raise ProfileError("Nomor telepon tidak valid", "phoneNumber")
        out["phoneNumber"] = phone
    if "jobTitle" in data:
        out["jobTitle"] = _text(data["jobTitle"], 80, "jobTitle", "Jabatan")
    if "department" in data:
        out["department"] = _text(data["department"], 80, "department", "Departemen")
    if "employeeNumber" in data:
        number = _text(data["employeeNumber"], 32, "employeeNumber", "Nomor karyawan")
        if number and not re.fullmatch(r"[A-Za-z0-9./\-]+", number):
            raise ProfileError("Nomor karyawan hanya huruf, angka, titik, garis miring atau strip", "employeeNumber")
        out["employeeNumber"] = number
    if "gender" in data:
        gender = (data["gender"] or None)
        if gender is not None and gender not in GENDERS:
            raise ProfileError("Jenis kelamin tidak valid", "gender")
        out["gender"] = gender
    if "birthDate" in data:
        raw = data["birthDate"]
        if raw in (None, ""):
            out["birthDate"] = None
        else:
            try:
                born = date.fromisoformat(str(raw)[:10])
            except ValueError as exc:
                raise ProfileError("Tanggal lahir tidak valid", "birthDate") from exc
            if not date(1900, 1, 1) <= born <= date.today():
                raise ProfileError("Tanggal lahir tidak valid", "birthDate")
            out["birthDate"] = born
    if "address" in data:
        out["address"] = _text(data["address"], 300, "address", "Alamat")
    if "city" in data:
        out["city"] = _text(data["city"], 80, "city", "Kota")
    if "workBranchCode" in data:
        code = _text(data["workBranchCode"], 32, "workBranchCode", "Lokasi kerja")
        if code and not branch_exists(code):
            raise ProfileError("Lokasi kerja tidak ditemukan", "workBranchCode")
        out["workBranchCode"] = code
    return out


def is_complete(row: dict) -> bool:
    return all(row.get(c) for c in REQUIRED_FOR_COMPLETE)
