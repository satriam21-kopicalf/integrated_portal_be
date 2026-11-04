"""Which Monitoring Product / Outlet items each branch group is asked (2026-11-04, migration 069): Monitoring OPS →
Branch groups. The defaults are the only_for of app/visit_forms.py; a row of form_item_scope shows (shown = true) or
hides (false) one item at the outlets of one group. A hidden item is neither asked nor assessed there (applies()).
The forms, the monitors and WW-WR (its keys are "product:…" / "outlet:…") all read the definitions through definition(),
so a change reaches every screen. Cached a few seconds; saving clears the cache."""
import copy
import time
from typing import Optional

from app import database as db

T = "integration_portal.form_item_scope"
FORMS = ("product", "outlet")
NONE = "—"  # onlyFor of an item no group is asked
_TTL = 20
_cache: dict = {"at": 0.0, "rows": {}, "groups": []}


def _load() -> tuple[dict, list[str]]:
    if time.monotonic() - _cache["at"] > _TTL:
        try:
            rows = db.fetch(f"SELECT form, item_key, group_code, shown FROM {T}")
            groups = [r["code"] for r in db.fetch("SELECT code FROM integration_portal.branch_group ORDER BY sort, code")]
        except Exception:  # the table not there yet (before 069): the defaults of the code
            rows, groups = [], []
        by: dict = {}
        for r in rows:
            by.setdefault((r["form"], r["item_key"]), {})[r["group_code"]] = r["shown"]
        _cache.update(at=time.monotonic(), rows=by, groups=groups)
    return _cache["rows"], _cache["groups"]


def clear() -> None:
    _cache["at"] = 0.0


def effective_only_for(form: str, key: str, base: Optional[list[str]]) -> Optional[list[str]]:
    """The groups that are asked the item (None = every group)."""
    rows, groups = _load()
    over = rows.get((form, key))
    if not over:
        return base
    asked = set(base) if base else set(groups)
    for g, shown in over.items():
        (asked.add if shown else asked.discard)(g)
    if base is None and asked >= set(groups):
        return None
    return sorted(asked) or [NONE]


def apply(form: str, definition: dict) -> dict:
    """A copy of a form definition with the onlyFor of every item after the scope rows."""
    rows, _ = _load()
    if not rows:
        return definition
    out = copy.deepcopy(definition)
    for g in out["groups"]:
        for s in g["sections"]:
            for it in s["items"]:
                src, key = (it["key"].split(":", 1) if ":" in it["key"] else (form, it["key"]))
                if src not in FORMS:
                    continue
                only = effective_only_for(src, key, it.get("onlyFor"))
                if only:
                    it["onlyFor"] = only
                else:
                    it.pop("onlyFor", None)
    return out


def matrix(base_definitions: dict) -> dict:
    """For Monitoring OPS → Branch groups: per form its sections and items with, per group, whether it is asked
    (shown) and whether that is the default (the setting of the code) or a change."""
    rows, groups = _load()
    forms = []
    for form in FORMS:
        sections = []
        for g in base_definitions[form]["groups"]:
            for s in g["sections"]:
                if s.get("retired"):
                    continue
                items = []
                for it in s["items"]:
                    if it["kind"] != "rating":
                        continue
                    base = it.get("onlyFor")
                    over = rows.get((form, it["key"]), {})
                    cells = {}
                    for code in groups:
                        default = base is None or code in base
                        cells[code] = {"shown": over.get(code, default), "default": default, "changed": code in over and over[code] != default}
                    items.append({"key": it["key"], "no": it["no"], "label": it["label"], "labelEn": it.get("labelEn", it["label"]),
                                  "required": bool(it.get("required", True)), "groups": cells})
                if items:
                    sections.append({"key": s["key"], "title": s["title"], "titleEn": s.get("titleEn", s["title"]),
                                     "group": g.get("titleEn", g["title"]), "groupTitle": g["title"], "items": items})
        forms.append({"form": form, "sections": sections})
    return {"groups": groups, "forms": forms}


def save(changes: list[dict], actor: Optional[str], base_definitions: dict) -> dict:
    """changes: [{form, itemKey, group, shown}]. A choice equal to the default removes its row (back to the code)."""
    _, groups = _load()
    keys = {(f, it["key"]): it.get("onlyFor") for f in FORMS for g in base_definitions[f]["groups"] for s in g["sections"] for it in s["items"]}
    n = 0
    with db.transaction() as conn:
        for c in changes:
            form, key, group, shown = c["form"], c["itemKey"], c["group"], bool(c["shown"])
            if (form, key) not in keys:
                raise ValueError(f"Butir {key} tidak ada di form {form}")
            if group not in groups:
                raise ValueError(f"Grup {group} tidak dikenal")
            base = keys[(form, key)]
            default = base is None or group in base
            if shown == default:
                conn.execute(f"DELETE FROM {T} WHERE form = %s AND item_key = %s AND group_code = %s", (form, key, group))
            else:
                conn.execute(f"""INSERT INTO {T} (form, item_key, group_code, shown, updated_by) VALUES (%s, %s, %s, %s, %s)
                                 ON CONFLICT (form, item_key, group_code) DO UPDATE SET shown = EXCLUDED.shown, updated_at = now(),
                                 updated_by = EXCLUDED.updated_by""", (form, key, group, shown, actor))
            n += 1
    clear()
    return {"saved": n}
