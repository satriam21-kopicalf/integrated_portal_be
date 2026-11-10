"""Sales Calculator → Breakdown (2026-11-10, management): the raw materials of every menu sold, by category → sub
category → menu → additional, for every outlet together or one by one, from the POS transactions and the recipe cards.

  portions   agg_menu_daily (kind menu) per outlet × month × menu; the sugar choices of agg_menu_option_daily split
             them into No / Less / Normal / Extra Sugar (the portions without a sugar choice are Normal)
  materials  one portion of the menu's card (menu_costing.explode: premix through its ingredients, bundles through their
             cards) × the portions; the sugar of a level = the grams set for that card (cc_option_card_rule, migration
             086), else the % rule of the choice; the other choices with a rule (Extra Shot, Cup Frosted …) add their
             own rows (a cup swap takes the default cup off: a negative quantity)
  value      the Pricelist of the period's last day, else the ESB HPP (as the calculator)

A menu without a card shows as one row without material, so a gap reads as a gap in the totals."""
import io
from datetime import date
from typing import Any, Optional

from app import database as db
from app import menu_costing as mc
from app import menu_options as mo
from app.scope import COMPANY_NAMES, MAIN_COMPANY

P = "integration_portal"
LEVELS = ("No Sugar", "Less Sugar", "Normal Sugar", "Extra Sugar")
NO_CARD = "(tanpa recipe card)"


def sugar_level(name: str) -> Optional[str]:
    """The sugar level of a choice (No / Less / Normal / Extra Sugar), None when it is not a sugar choice. Half Sugar
    counts as Less; Split Sugar (served apart) as Normal; the CE / CA variants as their level."""
    k = mo.key_of(name)
    if "sugar" not in k:
        return None
    if k.startswith("no "):
        return "No Sugar"
    if k.startswith(("less", "half")):
        return "Less Sugar"
    if k.startswith("extra"):
        return "Extra Sugar"
    return "Normal Sugar"


def _sold(d_from: date, d_to: date, branches: Optional[list[str]]) -> list[dict]:
    return db.fetch(
        f"""WITH br AS ({mc.BRANCH_COMPANY})
            SELECT a.branch_code AS branch, to_char(a.sales_date, 'YYYY-MM') AS month,
                   coalesce(br.company, %(main)s) || ':' || a.menu_id AS menu_id, max(a.menu_name) AS menu,
                   max(a.category) AS category, max(a.category_detail) AS sub, sum(a.qty) AS qty, sum(a.subtotal) AS sales
              FROM {P}.agg_menu_daily a LEFT JOIN br ON br.branch_code = a.branch_code
             WHERE a.sales_date BETWEEN %(a)s AND %(b)s AND a.kind = 'menu' AND a.menu_id <> '?'
               AND (%(br)s::text[] IS NULL OR a.branch_code = ANY(%(br)s))
             GROUP BY 1, 2, 3""", {"a": d_from, "b": d_to, "br": branches, "main": MAIN_COMPANY})


def _choices(d_from: date, d_to: date, branches: Optional[list[str]]) -> list[dict]:
    return db.fetch(
        f"""WITH br AS ({mc.BRANCH_COMPANY})
            SELECT a.branch_code AS branch, to_char(a.sales_date, 'YYYY-MM') AS month,
                   coalesce(br.company, %(main)s) || ':' || a.parent_id AS parent, a.option_name AS name, sum(a.qty) AS qty
              FROM {P}.agg_menu_option_daily a LEFT JOIN br ON br.branch_code = a.branch_code
             WHERE a.sales_date BETWEEN %(a)s AND %(b)s AND (%(br)s::text[] IS NULL OR a.branch_code = ANY(%(br)s))
             GROUP BY 1, 2, 3, 4""", {"a": d_from, "b": d_to, "br": branches, "main": MAIN_COMPANY})


def _f(v: Any) -> float:
    return float(v) if v is not None else 0.0


def rows(d_from: date, d_to: date, branches: Optional[list[str]], per_branch: bool = True, per_month: bool = True) -> dict:
    """{rows: [...], prices, names}: one row per (month?, outlet?, menu, additional, material) with the portions and the
    quantity, as the Excel Data sheet; `per_branch` / `per_month` False fold the outlets / months together."""
    cards = {k: c for k, c in mc.resolve({r["code"]: r for r in mc.recipes()}).items() if c["isActive"]}
    mapped = {m["id"]: code for code, c in cards.items() for m in c["menus"]}
    wip = mc.wips()
    rules = mo.rules()
    grams = mo.card_rules()
    one_cache: dict[str, dict] = {}

    def one(code: str) -> dict:
        if code not in one_cache:
            out: dict[str, dict] = {}
            mc.explode(cards[code], 1, wip, out)
            one_cache[code] = out
        return one_cache[code]

    def sugar_of(code: str, level: str) -> Optional[float]:
        """Grams of sugar of one portion at this level (None: the card has no sugar)."""
        normal = sum(v["qty"] for v in one(code).values() if mo.SUGAR.search(v["name"] or ""))
        if not normal:
            return None
        if level == "Normal Sugar":
            return normal
        g = grams.get((level.lower(), code))
        if g is not None:
            return g
        r = rules.get(level.lower())
        pct = next((ln["value"] for ln in (r or {}).get("lines", []) if ln.get("target") == "sugar" and ln.get("mode") == "pct"), None)
        return normal * (1 + (pct or 0) / 100) if r and r["effect"] == "adjust" else normal

    sold = _sold(d_from, d_to, branches)
    choices: dict[tuple, list[dict]] = {}
    for c in _choices(d_from, d_to, branches):
        choices.setdefault((c["branch"], c["month"], c["parent"]), []).append(c)

    out: dict[tuple, dict] = {}

    def add(base: dict, variant: str, portions: float, mat: Optional[dict], qty: float) -> None:
        key = (base["month"] if per_month else "", base["branch"] if per_branch else "", base["menu_id"], variant,
               (mat or {}).get("code") or (mat or {}).get("name") or "")
        r = out.get(key)
        if r is None:
            r = out[key] = {"month": key[0], "branch": key[1], "company": base["menu_id"].split(":")[0], "category": base["category"] or "",
                            "sub": base["sub"] or "", "menuId": base["menu_id"], "menu": base["menu"], "card": base["card"],
                            "variant": variant, "portions": 0.0, "code": (mat or {}).get("code"), "material": (mat or {}).get("name") or NO_CARD,
                            "unit": (mat or {}).get("unit") or "", "qty": 0.0, "_seen": set()}
        # the portions of a variant counted once per sold line, not once per material
        tag = (base["branch"], base["month"])
        if tag not in r["_seen"]:
            r["_seen"].add(tag)
            r["portions"] += portions
        r["qty"] += qty

    for s in sold:
        qty = _f(s["qty"])
        code = mapped.get(s["menu_id"])
        base = {**s, "card": cards[code]["name"] if code else ""}
        if not code:
            add(base, "Normal Sugar", qty, None, 0.0)
            continue
        portion = one(code)
        mine = choices.get((s["branch"], s["month"], s["menu_id"]), [])
        levels = {lv: 0.0 for lv in LEVELS}
        for c in mine:
            lv = sugar_level(c["name"])
            if lv:
                levels[lv] += _f(c["qty"])
        levels["Normal Sugar"] += max(qty - sum(levels.values()), 0.0)  # no sugar choice = the card (Normal)
        for lv, n in levels.items():
            if n <= 0:
                continue
            target = sugar_of(code, lv)
            normal = sum(v["qty"] for v in portion.values() if mo.SUGAR.search(v["name"] or ""))
            for v in portion.values():
                q = v["qty"]
                if target is not None and normal and mo.SUGAR.search(v["name"] or ""):
                    q = target * (v["qty"] / normal)  # each sugar item by its share
                add(base, lv, n, v, q * n)
        # the other choices with a rule add their own materials (a cup swap: the default cup off, the other on)
        for c in mine:
            if sugar_level(c["name"]):
                continue
            r = rules.get(mo.key_of(c["name"]))
            if not r or r["effect"] != "adjust":
                continue
            n = _f(c["qty"])
            for ln in r["lines"]:
                if ln["mode"] == "pct" and ln["target"] == "item":
                    for v in portion.values():
                        if v.get("code") == ln["code"]:
                            add(base, c["name"], n, v, v["qty"] * ln["value"] / 100 * n)
                elif ln["mode"] == "qty" and ln.get("code"):
                    add(base, c["name"], n, {"code": ln["code"], "name": ln.get("name") or ln["code"], "unit": ""}, ln["value"] * n)
    data = list(out.values())
    for r in data:
        r.pop("_seen", None)
    # the unit of a material added by a rule: the item master's base unit
    blank = sorted({r["code"] for r in data if r["code"] and not r["unit"]})
    if blank:
        units = {x["code"]: x["base_uom"] for x in db.fetch("SELECT code, upper(coalesce(base_uom, '')) AS base_uom FROM kc_master.item WHERE code = ANY(%s)", (blank,))}
        for r in data:
            if r["code"] in units and not r["unit"]:
                r["unit"] = units[r["code"]]
    codes = sorted({r["code"] for r in data if r["code"]})
    price = mc.prices_on(d_to, codes or None)
    missing = [c for c in codes if c not in price]
    if missing:
        hpp = mc.esb_hpp(d_to)
        price.update({c: hpp[c][0] for c in missing if c in hpp})
    for r in data:
        p = price.get(r["code"]) if r["code"] else None
        r["price"] = p
        r["value"] = round(r["qty"] * p, 2) if p is not None else None
        r["qty"] = round(r["qty"], 4)
        r["portions"] = round(r["portions"], 3)
    names = {}
    if per_branch:
        names = {x["branch_code"]: x["branch_name"] for x in db.fetch(
            """SELECT DISTINCT ON (branch_code) branch_code, branch_name FROM integration_esb.master_branches
                WHERE branch_code = ANY(%s) ORDER BY branch_code""", (sorted({r["branch"] for r in data}),))}
    return {"rows": data, "names": names}


def summary(d_from: date, d_to: date, branches: Optional[list[str]]) -> dict:
    """The screen: every menu (category → sub category → menu) with its portions per sugar level, its materials and value,
    and the materials over every menu; the outlets and months folded together."""
    got = rows(d_from, d_to, branches, per_branch=False, per_month=False)["rows"]
    menus: dict[str, dict] = {}
    mats: dict[str, dict] = {}
    for r in got:
        m = menus.setdefault(r["menuId"], {"id": r["menuId"], "category": r["category"], "sub": r["sub"], "menu": r["menu"], "card": r["card"],
                                           "levels": {}, "choices": {}, "materials": {}, "value": 0.0, "unpriced": 0})
        if r["variant"] in LEVELS:
            m["levels"][r["variant"]] = r["portions"]
        else:
            m["choices"][r["variant"]] = r["portions"]
        key = r["code"] or r["material"]
        x = m["materials"].setdefault(key, {"code": r["code"], "name": r["material"], "unit": r["unit"], "qty": 0.0, "value": 0.0, "priced": True})
        x["qty"] += r["qty"]
        if r["value"] is None:
            x["priced"] = False
            if r["code"]:
                m["unpriced"] += 1
        else:
            x["value"] += r["value"]
            m["value"] += r["value"]
        t = mats.setdefault(key, {"code": r["code"], "name": r["material"], "unit": r["unit"], "qty": 0.0, "value": 0.0, "price": r["price"], "menus": set()})
        t["qty"] += r["qty"]
        t["value"] += r["value"] or 0.0
        t["menus"].add(r["menuId"])
    out_menus = []
    for m in menus.values():
        m["portions"] = round(sum(m["levels"].values()), 3)
        m["materials"] = sorted(({**x, "qty": round(x["qty"], 3), "value": round(x["value"], 2)} for x in m["materials"].values()), key=lambda x: -x["value"])
        m["value"] = round(m["value"], 2)
        out_menus.append(m)
    out_menus.sort(key=lambda m: (m["category"], m["sub"], -m["portions"]))
    materials = sorted(({**t, "qty": round(t["qty"], 3), "value": round(t["value"], 2), "menus": len(t["menus"])} for t in mats.values()
                        if t["code"] or t["name"] != NO_CARD), key=lambda t: -t["value"])
    return {"period": {"from": d_from.isoformat(), "to": d_to.isoformat()}, "menus": out_menus, "materials": materials,
            "totals": {"portions": round(sum(m["portions"] for m in out_menus), 3), "value": round(sum(m["value"] for m in out_menus), 2),
                       "noCard": sum(1 for m in out_menus if not m["card"])}}


COLUMNS = [("month", "Bulan", 10), ("branch", "Kode Outlet", 12), ("branchName", "Outlet", 30), ("brand", "Brand", 14),
           ("category", "Kategori", 14), ("sub", "Sub Kategori", 24), ("menu", "Menu", 32), ("card", "Recipe Card", 30),
           ("variant", "Additional", 22), ("portions", "Porsi", 12), ("code", "Kode Bahan", 14), ("material", "Bahan Baku", 34),
           ("unit", "Satuan", 9), ("qty", "Kebutuhan", 14), ("price", "Harga Satuan (Rp)", 16), ("value", "Nilai (Rp)", 16)]
MAX_ROWS = 1_000_000  # Excel holds 1,048,576 rows a sheet


def workbook(d_from: date, d_to: date, branches: Optional[list[str]], per_branch: bool = True, user: Optional[dict] = None) -> bytes:
    """The Excel as bytes (small scopes and tests); the export job writes the same file with write_file()."""
    import os
    import tempfile
    fd, path = tempfile.mkstemp(suffix=".xlsx")
    os.close(fd)
    try:
        write_file(path, d_from, d_to, branches, per_branch, user)
        with open(path, "rb") as f:
            return f.read()
    finally:
        os.remove(path)


def write_file(path: str, d_from: date, d_to: date, branches: Optional[list[str]], per_branch: bool = True,
               user: Optional[dict] = None, file_name: Optional[str] = None) -> int:
    """The Excel of the breakdown in the design of the Sales Transactions export (app/exports.py: the title rows, a bold
    frozen header, column widths, a Ringkasan sheet): Data (one row per month × outlet × menu × additional × material,
    ready for a Pivot Table from its header row), Per Bahan, Per Menu and Catatan."""
    from datetime import datetime
    from app.xlsx_stream import StreamingXlsxWriter
    got = rows(d_from, d_to, branches, per_branch=per_branch, per_month=True)
    data, names = got["rows"], got["names"]
    if len(data) > MAX_ROWS:
        raise mc.CostingError(f"{len(data):,} baris melebihi batas Excel: pilih outlet atau periode yang lebih pendek".replace(",", "."))
    cols = [c for c in COLUMNS if per_branch or c[0] not in ("branch", "branchName")]
    file_name = file_name or f"Breakdown Bahan Baku {d_from:%Y%m%d}-{d_to:%Y%m%d}"
    branch_label = "Semua Outlet" if not branches else ", ".join(sorted(branches)[:20]) + (" …" if len(branches) > 20 else "")
    preamble = [["Breakdown Bahan Baku per Menu"], ["PT Yuda Prawira Group"], [],
                ["Generated", datetime.now().strftime("%d-%m-%Y %H:%M:%S")],
                ["Period", f"{d_from:%d-%m-%Y} - {d_to:%d-%m-%Y}"],
                ["Branch", branch_label],
                ["Detail", "Per bulan, per outlet" if per_branch else "Per bulan, semua outlet digabung"],
                ["Generated Username", (user or {}).get("full_name") or (user or {}).get("username") or "Integrated Portal"],
                ["Report File Name", file_name], []]
    # per material and per menu, for the summary sheets
    mats: dict[str, dict] = {}
    menus: dict[tuple, dict] = {}
    for r in data:
        k = r["code"] or r["material"]
        m = mats.setdefault(k, {"code": r["code"], "material": r["material"], "unit": r["unit"], "qty": 0.0, "value": 0.0, "price": r["price"]})
        m["qty"] += r["qty"]
        m["value"] += r["value"] or 0.0
        mk = (r["category"], r["sub"], r["menu"], r["variant"])
        x = menus.setdefault(mk, {"portions": {}, "value": 0.0})
        x["portions"][(r["month"], r["branch"])] = r["portions"]
        x["value"] += r["value"] or 0.0
    total_value = sum(m["value"] for m in mats.values())
    if True:
        with StreamingXlsxWriter(path) as xlsx:
            sheet = xlsx.add_sheet("Data", widths=[w for _, _, w in cols], header=[h for _, h, _ in cols], preamble=preamble)
            sheet.write_rows([[{**r, "branchName": names.get(r["branch"], r["branch"]), "brand": COMPANY_NAMES.get(r["company"], r["company"])}.get(k)
                               for k, _, _ in cols]
                              for r in sorted(data, key=lambda r: (r["month"], r["branch"], r["category"], r["sub"], r["menu"], r["variant"], r["material"]))])
            xlsx.add_static_sheet("Per Bahan", [["Kode Bahan", "Bahan Baku", "Satuan", "Kebutuhan", "Harga Satuan (Rp)", "Nilai (Rp)"],
                                                *([m["code"], m["material"], m["unit"], round(m["qty"], 3), m["price"], round(m["value"], 2)]
                                                  for m in sorted(mats.values(), key=lambda m: -m["value"]))],
                                  widths=[14, 36, 9, 16, 18, 18], bold_rows=(0,))
            xlsx.add_static_sheet("Per Menu", [["Kategori", "Sub Kategori", "Menu", "Additional", "Porsi", "Nilai Bahan (Rp)"],
                                               *([cat, sub, menu, variant, round(sum(x["portions"].values()), 3), round(x["value"], 2)]
                                                 for (cat, sub, menu, variant), x in sorted(menus.items()))],
                                  widths=[14, 26, 34, 24, 14, 18], bold_rows=(0,))
            summary = [["Ringkasan"], [], ["Periode", f"{d_from:%d-%m-%Y} - {d_to:%d-%m-%Y}"], ["Outlet", branch_label],
                       ["Baris data", len(data)], ["Bahan baku", len([m for m in mats.values() if m["code"]])],
                       ["Nilai bahan (Rp)", round(total_value, 2)],
                       ["Menu tanpa recipe card", len({r["menuId"] for r in data if r["material"] == NO_CARD})], [],
                       ["Catatan"],
                       ["Porsi = porsi terjual menu itu dengan additional tersebut; porsi tanpa pilihan gula dihitung Normal Sugar."],
                       ["Kebutuhan = porsi x bahan per porsi di recipe card; gula mengikuti gramasi per menu (Variants & Modifiers), selain itu aturan %."],
                       ["Additional lain (Extra Shot, Cup Frosted, ...) menambah barisnya sendiri; penggantian cup memakai jumlah negatif untuk cup bawaan."],
                       ["Harga = Pricelist pada akhir periode, selain itu HPP ESB; Nilai kosong = bahan belum berharga."],
                       [f"Bahan Baku '{NO_CARD}' = menu yang belum punya recipe card."],
                       [f"Pivot: pilih sheet Data mulai baris judul kolom (baris {len(preamble) + 1}), lalu Insert > PivotTable."]]
            xlsx.add_static_sheet("Ringkasan", summary, widths=[26, 60], bold_rows=(0, 9), first=True)
    return len(data)
