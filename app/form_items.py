"""Form Items editor of Product & Outlet (Operations → Branch Groups → Form Items, 2026-11-13; migration 106).

The items of the forms are written in app/visit_forms.py (the workbook "Form Visit"). Operations changes them here
without a release, and every screen follows (the forms of Operations and the portal, WW-WR, the monitors), since they
all read the definitions through visit_forms.definition():

    override   per code item (form_item_override): category (Kritikal / Mayor / Minor / another), note and photo
               required / optional / none, active, the label and the description (photo points, standard) ID / EN —
               a NULL column keeps the code's value
    custom     new items of a section (form_item_custom), key "<section>.c<n>", rated UNDER / MEET / EXCEED like the
               others; never deleted, only made inactive (the visits sent with them keep their answers)
    weights    the weight of a category in the score (form_category_weight): score = Σ weight of MEET / EXCEED ÷
               Σ weight of the rated items (Kritikal 3, Mayor 2, Minor 1, any other or none 1 by default)
An inactive item is neither asked nor scored (visit_forms.applies); a visit already sent keeps the score it was sent
with (stored at submit).
"""
import copy
import re
import time
from typing import Any, Optional

from app import database as db

P = "integration_portal"
T_OVER, T_CUSTOM, T_WEIGHT = f"{P}.form_item_override", f"{P}.form_item_custom", f"{P}.form_category_weight"
T_ORDER = f"{P}.form_item_order"  # migration 109: the order of the items of a section
FORMS = ("product", "outlet")
LEVELS = ("required", "optional", "none")
DEFAULT_WEIGHTS = {"Kritikal": 3.0, "Mayor": 2.0, "Minor": 1.0}
CATEGORY_EN = {"Kritikal": "Critical", "Mayor": "Major", "Minor": "Minor"}
FIELDS = ("category", "note", "photo", "active", "label", "label_en", "detail", "detail_en", "standard", "standard_en", "required")
_TTL = 20
_cache: dict = {"at": 0.0, "over": {}, "custom": [], "weights": dict(DEFAULT_WEIGHTS), "order": {}}


class FormItemError(ValueError):
    """Refused with a message for the user (HTTP 422)."""


def clear() -> None:
    _cache["at"] = 0.0


def _load() -> dict:
    if time.monotonic() - _cache["at"] > _TTL:
        try:
            over = {(r["form"], r["item_key"]): r for r in db.fetch(f"SELECT * FROM {T_OVER}")}
            custom = db.fetch(f"SELECT * FROM {T_CUSTOM} ORDER BY form, section_key, sort, created_at")
            weights = {**DEFAULT_WEIGHTS, **{r["category"]: float(r["weight"]) for r in db.fetch(f"SELECT category, weight FROM {T_WEIGHT}")}}
        except Exception:  # before migration 106: the code alone
            over, custom, weights = {}, [], dict(DEFAULT_WEIGHTS)
        try:
            order = {(r["form"], r["section_key"]): list(r["keys"] or []) for r in db.fetch(f"SELECT form, section_key, keys FROM {T_ORDER}")}
        except Exception:  # before migration 109: the order of the code
            order = {}
        _cache.update(at=time.monotonic(), over=over, custom=custom, weights=weights, order=order)
    return _cache


def weights() -> dict[str, float]:
    return _load()["weights"]


def weight_of(item: dict) -> float:
    """The weight of an item in the score (its category; 1 without one)."""
    c = item.get("category")
    return float(weights().get(c, 1.0)) if c else 1.0


def _apply_fields(it: dict, row: dict) -> None:
    if row.get("category") is not None:
        cat = row["category"].strip()
        if cat:
            it["category"], it["categoryEn"] = cat, CATEGORY_EN.get(cat, cat)
        else:
            it.pop("category", None)
            it.pop("categoryEn", None)
    for col, key in (("note", "note"), ("photo", "photo")):
        if row.get(col) is not None:
            it[key] = row[col]
    if row.get("required") is not None:
        it["required"] = bool(row["required"])
    if row.get("active") is not None and not row["active"]:
        it["active"] = False
    for col, key in (("label", "label"), ("label_en", "labelEn"), ("detail", "detail"), ("detail_en", "detailEn"),
                     ("standard", "standard"), ("standard_en", "standardEn")):
        if row.get(col) is not None:
            it[key] = row[col] or None
            if not it[key]:
                it.pop(key, None)


def overlay(form: str, definition: dict) -> dict:
    """A copy of the code definition of a form with the overrides, the custom items and the inactive marks."""
    c = _load()
    if not c["over"] and not any(r["form"] == form for r in c["custom"]) and not any(f == form for f, _ in c.get("order", {})):
        return definition
    out = copy.deepcopy(definition)
    for g in out["groups"]:
        for s in g["sections"]:
            for it in s["items"]:
                row = c["over"].get((form, it["key"]))
                if row:
                    _apply_fields(it, row)
            for r in (r for r in c["custom"] if r["form"] == form and r["section_key"] == s["key"]):
                no = max((x.get("no") or 0 for x in s["items"]), default=0) + 1
                it = {"key": r["key"], "no": no, "label": r["label"], "labelEn": r.get("label_en") or r["label"], "kind": "rating",
                      "required": bool(r.get("required", True)), "note": r.get("note") or "required", "photo": r.get("photo") or "required",
                      "custom": True}
                _apply_fields(it, {k: r.get(k) for k in ("category", "active", "detail", "detail_en", "standard", "standard_en")})
                s["items"].append(it)
            keys = c.get("order", {}).get((form, s["key"]))
            if keys:  # the order set by drag and drop; items not in it (added later) keep their place after them
                pos = {k: i for i, k in enumerate(keys)}
                was = {x["key"]: i for i, x in enumerate(s["items"])}
                s["items"].sort(key=lambda x: (pos.get(x["key"], len(keys)), was[x["key"]]))
                for i, x in enumerate(s["items"], start=1):
                    x["no"] = i
    return out


# ---------------------------------------------------------------- the editor

def editor(definitions: dict) -> dict:
    """Per form its groups / sections / items with what Operations may change (the effective values, the code's and
    whether an item is custom), the categories in use and their weights."""
    c = _load()
    forms, cats = [], set(DEFAULT_WEIGHTS)
    for form in FORMS:
        base = definitions[form]
        eff = overlay(form, base)
        base_items = {it["key"]: it for g in base["groups"] for s in g["sections"] for it in s["items"]}
        sections = []
        for g in eff["groups"]:
            for s in g["sections"]:
                if s.get("retired"):
                    continue
                items = []
                for it in s["items"]:
                    if it["kind"] != "rating":
                        continue
                    if it.get("category"):
                        cats.add(it["category"])
                    b = base_items.get(it["key"])
                    items.append({"key": it["key"], "no": it["no"], "label": it["label"], "labelEn": it.get("labelEn", it["label"]),
                                  "category": it.get("category"), "note": it.get("note", "required"), "photo": it.get("photo", "required"),
                                  "required": bool(it.get("required", True)), "active": it.get("active", True) is not False,
                                  "detail": it.get("detail"), "detailEn": it.get("detailEn"), "standard": it.get("standard"),
                                  "standardEn": it.get("standardEn"), "custom": bool(it.get("custom")),
                                  "changed": (form, it["key"]) in c["over"],
                                  "code": None if b is None else {"label": b["label"], "category": b.get("category"), "note": b.get("note", "required"),
                                                                    "photo": b.get("photo", "required"), "required": bool(b.get("required", True))}})
                sections.append({"key": s["key"], "title": s["title"], "titleEn": s.get("titleEn", s["title"]),
                                 "group": g.get("titleEn", g["title"]), "groupTitle": g["title"], "items": items})
        forms.append({"form": form, "sections": sections})
    w = weights()
    return {"forms": forms, "weights": [{"category": k, "categoryEn": CATEGORY_EN.get(k, k), "weight": w.get(k, 1.0)}
                                        for k in sorted(cats, key=lambda x: (-w.get(x, 1.0), x))], "levels": list(LEVELS)}


def _clean(data: dict) -> dict:
    out: dict[str, Any] = {}
    for k in ("note", "photo"):
        if k in data and data[k] is not None:
            if data[k] not in LEVELS:
                raise FormItemError("Catatan / foto: required, optional atau none")
            out[k] = data[k]
    if "category" in data and data["category"] is not None:
        out["category"] = str(data["category"]).strip()[:40]
    for k in ("label", "label_en", "detail", "detail_en", "standard", "standard_en"):
        if k in data and data[k] is not None:
            out[k] = str(data[k]).strip()[:1000]
    if out.get("label") == "":
        raise FormItemError("Nama butir wajib diisi")
    for k in ("active", "required"):
        if k in data and data[k] is not None:
            out[k] = bool(data[k])
    return out


def _section_keys(definitions: dict, form: str) -> set[str]:
    return {s["key"] for g in definitions[form]["groups"] for s in g["sections"] if not s.get("retired")}


def save_item(definitions: dict, form: str, key: str, data: dict, actor: Optional[str]) -> dict:
    """Change one item: a code item gets an override row (a field equal to the code's is stored as NULL), a custom item
    its own row. reset=True puts a code item back to the code."""
    if form not in FORMS:
        raise FormItemError("Form tidak dikenal")
    code_items = {it["key"]: it for g in definitions[form]["groups"] for s in g["sections"] for it in s["items"]}
    fields = _clean(data)
    with db.transaction() as conn:
        if key in code_items:
            if data.get("reset"):
                conn.execute(f"DELETE FROM {T_OVER} WHERE form = %s AND item_key = %s", (form, key))
            else:
                b = code_items[key]
                base = {"category": b.get("category") or "", "note": b.get("note", "required"), "photo": b.get("photo", "required"),
                        "active": True, "label": b["label"], "label_en": b.get("labelEn", b["label"]), "detail": b.get("detail") or "",
                        "detail_en": b.get("detailEn") or "", "standard": b.get("standard") or "", "standard_en": b.get("standardEn") or "",
                        "required": bool(b.get("required", True))}
                # the other fields keep what was set before; a field equal to the code's goes back to the code (NULL)
                old = conn.execute(f"SELECT * FROM {T_OVER} WHERE form = %s AND item_key = %s", (form, key)).fetchone()
                old = dict(old) if old is not None and not isinstance(old, dict) and hasattr(old, "keys") else (old or {})
                row = {c: old.get(c) for c in FIELDS}
                row.update({k: (None if v == base.get(k) else v) for k, v in fields.items()})
                cols = list(FIELDS)
                conn.execute(
                    f"""INSERT INTO {T_OVER} (form, item_key, {", ".join(cols)}, updated_by) VALUES (%s, %s, {", ".join(["%s"] * len(cols))}, %s)
                        ON CONFLICT (form, item_key) DO UPDATE SET {", ".join(f"{c} = EXCLUDED.{c}" for c in cols)}, updated_by = EXCLUDED.updated_by,
                        updated_at = now()""",
                    (form, key, *[row.get(c) for c in cols], actor))
        else:
            got = conn.execute(f"SELECT key FROM {T_CUSTOM} WHERE form = %s AND key = %s", (form, key)).fetchone()
            if not got:
                raise FormItemError(f"Butir {key} tidak ada di form {form}")
            if fields:
                conn.execute(f"""UPDATE {T_CUSTOM} SET {", ".join(f"{k} = %s" for k in fields)}, updated_by = %s, updated_at = now()
                                 WHERE form = %s AND key = %s""", (*fields.values(), actor, form, key))
    clear()
    return {"form": form, "key": key}


def add_item(definitions: dict, form: str, section: str, data: dict, actor: Optional[str]) -> dict:
    """A new item at the end of a section (key "<section>.c<n>")."""
    if form not in FORMS:
        raise FormItemError("Form tidak dikenal")
    if section not in _section_keys(definitions, form):
        raise FormItemError("Bagian form tidak dikenal")
    fields = _clean(data)
    if not fields.get("label"):
        raise FormItemError("Nama butir wajib diisi")
    group = next(g["key"] for g in definitions[form]["groups"] for s in g["sections"] if s["key"] == section)
    with db.transaction() as conn:
        rows = conn.execute(f"SELECT key FROM {T_CUSTOM} WHERE form = %s AND section_key = %s", (form, section)).fetchall()
        used = [int(m.group(1)) for r in rows if (m := re.search(r"\.c(\d+)$", r["key"] if isinstance(r, dict) else r[0]))]
        key = f"{section}.c{max(used, default=0) + 1}"
        cols = ["label", "label_en", "category", "note", "photo", "required", "detail", "detail_en", "standard", "standard_en"]
        vals = {"label_en": fields.get("label"), "note": "required", "photo": "required", "required": True, **fields}
        conn.execute(f"""INSERT INTO {T_CUSTOM} (form, key, group_key, section_key, sort, active, {", ".join(cols)}, created_by)
                         VALUES (%s, %s, %s, %s, %s, true, {", ".join(["%s"] * len(cols))}, %s)""",
                     (form, key, group, section, len(rows) + 1, *[vals.get(c) for c in cols], actor))
    clear()
    return {"form": form, "key": key}


def save_order(definitions: dict, form: str, section: str, keys: list[str], actor: Optional[str]) -> dict:
    """The order of the items of a section (every item key of the section, custom ones included)."""
    if form not in FORMS:
        raise FormItemError("Form tidak dikenal")
    if section not in _section_keys(definitions, form):
        raise FormItemError("Bagian form tidak dikenal")
    known = {it["key"] for g in overlay(form, definitions[form])["groups"] for s in g["sections"] if s["key"] == section for it in s["items"]}
    clean = [k for k in dict.fromkeys(keys) if k in known]
    if not clean:
        raise FormItemError("Urutan butir kosong")
    with db.transaction() as conn:
        conn.execute(f"""INSERT INTO {T_ORDER} (form, section_key, keys, updated_by) VALUES (%s, %s, %s, %s)
                         ON CONFLICT (form, section_key) DO UPDATE SET keys = EXCLUDED.keys, updated_by = EXCLUDED.updated_by, updated_at = now()""",
                     (form, section, clean, actor))
    clear()
    return {"form": form, "section": section, "keys": clean}


def save_weights(rows: list[dict], actor: Optional[str]) -> dict:
    with db.transaction() as conn:
        for r in rows:
            cat = str(r.get("category") or "").strip()[:40]
            w = float(r.get("weight"))
            if not cat or not 0 <= w <= 100:
                raise FormItemError("Bobot 0–100 untuk setiap kategori")
            conn.execute(f"""INSERT INTO {T_WEIGHT} (category, weight, updated_by) VALUES (%s, %s, %s)
                             ON CONFLICT (category) DO UPDATE SET weight = EXCLUDED.weight, updated_by = EXCLUDED.updated_by, updated_at = now()""",
                         (cat, w, actor))
    clear()
    return {"saved": len(rows)}
