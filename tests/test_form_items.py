"""Form Items editor (app/form_items.py, migration 106): changed items, new items, inactive items, the weighted score."""
import time

from app import form_items as fi
from app import visit_forms as vf


def _cache(over=None, custom=None, weights=None):
    fi._cache.update(at=time.monotonic(), over=over or {}, custom=custom or [], weights={**fi.DEFAULT_WEIGHTS, **(weights or {})})


def _first(form="product"):
    g = vf.DEFINITIONS[form]["groups"][0]
    s = g["sections"][0]
    return g, s, s["items"][0], s["items"][1]


def test_an_override_a_custom_item_and_an_inactive_one_reach_the_definition():
    g, s, a, b = _first()
    _cache(over={("product", a["key"]): {"category": "Kritikal", "note": "optional", "photo": "none", "active": None, "label": "Stok premix",
                                         "label_en": None, "detail": "Foto rak", "detail_en": None, "standard": None, "standard_en": None, "required": None},
                 ("product", b["key"]): {"active": False}},
           custom=[{"form": "product", "key": f"{s['key']}.c1", "group_key": g["key"], "section_key": s["key"], "label": "Butir baru",
                    "label_en": "New item", "category": "Mayor", "note": "required", "photo": "optional", "required": True, "active": True}])
    d = vf.edited("product")
    sec = d["groups"][0]["sections"][0]
    it = {x["key"]: x for x in sec["items"]}
    assert it[a["key"]]["category"] == "Kritikal" and it[a["key"]]["categoryEn"] == "Critical" and it[a["key"]]["note"] == "optional"
    assert it[a["key"]]["photo"] == "none" and it[a["key"]]["label"] == "Stok premix" and it[a["key"]]["detail"] == "Foto rak"
    assert it[b["key"]]["active"] is False and not vf.applies(it[b["key"]], None)
    new = it[f"{s['key']}.c1"]
    assert new["custom"] and new["kind"] == "rating" and new["labelEn"] == "New item" and new["photo"] == "optional"
    assert new["no"] == max(x["no"] for x in s["items"]) + 1
    # the code definition is never changed; WW-WR is built from the edited forms
    assert "category" not in a or a.get("category") != "Kritikal" or a.get("label") != "Stok premix"
    assert any(x["key"] == f"product:{s['key']}.c1" for gg in vf.edited("wwwr")["groups"] for ss in gg["sections"] for x in ss["items"])
    _cache()


def test_the_score_is_weighted_by_category_and_skips_inactive_items(monkeypatch):
    items = [{"key": "a", "kind": "rating", "category": "Kritikal"}, {"key": "b", "kind": "rating", "category": "Minor"},
             {"key": "c", "kind": "rating"}, {"key": "d", "kind": "rating", "category": "Mayor", "active": False}]
    monkeypatch.setattr(vf, "all_items", lambda form, retired=False: items)
    _cache()
    answers = {"a": {"rating": "UNDER"}, "b": {"rating": "MEET"}, "c": {"rating": "EXCEED"}, "d": {"rating": "MEET"}}
    c = vf._counts("product", answers)
    assert (c["under"], c["meet"], c["exceed"], c["rated"], c["total"]) == (1, 1, 1, 3, 3)  # d inactive: not asked
    assert c["score"] == round((1 + 1) * 100 / (3 + 1 + 1), 1) and c["scoreBasis"] == "weighted"  # 40.0
    _cache(weights={"Kritikal": 1})
    assert vf._counts("product", answers)["score"] == round(2 * 100 / 3, 1)  # equal weights: as before
    _cache()


def test_saving_keeps_the_other_fields_and_a_value_equal_to_the_code_goes_back_to_it(monkeypatch):
    g, s, a, _ = _first()
    stored = {}

    class Conn:
        def execute(self, q, p=None):
            if q.startswith("SELECT * FROM"):
                return type("R", (), {"fetchone": lambda self_: stored.get("row")})()
            if "INSERT INTO" in q and "form_item_override" in q:
                cols = list(fi.FIELDS)
                stored["row"] = dict(zip(cols, p[2:2 + len(cols)]))
            return type("R", (), {"fetchone": lambda self_: None, "fetchall": lambda self_: []})()

    class Tx:
        def __enter__(self):
            return Conn()

        def __exit__(self, *x):
            return False
    monkeypatch.setattr(fi.db, "transaction", lambda: Tx())
    fi.save_item(vf.DEFINITIONS, "product", a["key"], {"category": "Kritikal"}, "u")
    fi.save_item(vf.DEFINITIONS, "product", a["key"], {"active": False}, "u")
    assert stored["row"]["category"] == "Kritikal" and stored["row"]["active"] is False  # the first change kept
    fi.save_item(vf.DEFINITIONS, "product", a["key"], {"label": a["label"]}, "u")
    assert stored["row"]["label"] is None  # equal to the code: back to it
    import pytest
    with pytest.raises(fi.FormItemError):
        fi.save_item(vf.DEFINITIONS, "product", "nope.1", {"active": False}, "u")
    with pytest.raises(fi.FormItemError):
        fi.add_item(vf.DEFINITIONS, "product", "nope", {"label": "x"}, "u")
    with pytest.raises(fi.FormItemError):
        fi.save_item(vf.DEFINITIONS, "product", a["key"], {"note": "maybe"}, "u")


def test_the_order_of_a_section_by_drag_and_drop():
    g, s, a, b = _first()
    keys = [x["key"] for x in s["items"]]
    _cache()
    fi._cache["order"] = {("product", s["key"]): [keys[1], keys[0]]}  # the second first; the others after, as they were
    sec = vf.edited("product")["groups"][0]["sections"][0]
    assert [x["key"] for x in sec["items"]] == [keys[1], keys[0], *keys[2:]]
    assert [x["no"] for x in sec["items"]] == list(range(1, len(keys) + 1))
    assert [x["key"] for x in vf.DEFINITIONS["product"]["groups"][0]["sections"][0]["items"]] == keys  # the code unchanged
    _cache()
