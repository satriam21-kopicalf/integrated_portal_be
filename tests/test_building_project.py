"""Building & Renovation projects (app/building_project.py): RAB totals, progress, the S-curve."""
from datetime import date

from app import building_project as bp


def test_rab_totals():
    sections = [{"id": 1}, {"id": 2}]
    lines = [{"section_id": 1, "kind": "MATERIAL", "qty": 2, "unit_price": 600000, "actual_amount": 1150000},
             {"section_id": 1, "kind": "LABOUR", "qty": 3, "unit_price": 150000, "actual_amount": None},
             {"section_id": 2, "kind": "MATERIAL", "qty": 1.5, "unit_price": 100, "actual_amount": None}]
    t = bp.rab_totals(sections, lines)
    assert t["total"] == 1650150.0 and t["actual"] == 1150000.0 and t["bought"] == 1
    assert t["sections"][1]["total"] == 1650000.0 and t["sections"][2]["total"] == 150.0
    assert t["kinds"]["LABOUR"] == 450000.0


def test_overall_is_weighted():
    assert bp.overall([{"weight": 30, "progress": 100}, {"weight": 70, "progress": 50}]) == 65.0
    assert bp.overall([]) == 0.0


def test_planned_share_of_days():
    st = [{"id": 1, "weight": 50, "plan_start": date(2026, 12, 1), "plan_end": date(2026, 12, 10)},
          {"id": 2, "weight": 50, "plan_start": date(2026, 12, 11), "plan_end": date(2026, 12, 20)}]
    assert bp.planned_at(st, date(2026, 11, 30)) == 0.0
    assert bp.planned_at(st, date(2026, 12, 5)) == 25.0
    assert bp.planned_at(st, date(2026, 12, 20)) == 100.0


def test_curve_actual_up_to_today():
    st = [{"id": 1, "weight": 50, "plan_start": date(2026, 12, 1), "plan_end": date(2026, 12, 10)},
          {"id": 2, "weight": 50, "plan_start": date(2026, 12, 11), "plan_end": date(2026, 12, 20)}]
    reps = [{"id": 1, "stage_id": 1, "progress": 40, "reported_on": date(2026, 12, 3)}, {"id": 2, "stage_id": 1, "progress": 100, "reported_on": date(2026, 12, 9)}]
    c = bp.curve(st, reps, date(2026, 12, 1), date(2026, 12, 20), date(2026, 12, 9))
    assert [p["day"] for p in c] == ["2026-12-01", "2026-12-08", "2026-12-15", "2026-12-20"]
    assert c[1]["actual"] == 20.0 and c[2]["actual"] is None and c[-1]["plan"] == 100.0


def test_fund_lines_from_the_rab():
    from decimal import Decimal
    lines = [{"id": 1, "kind": "MATERIAL", "qty": 4, "unit_price": 600000, "unit": "lbr", "description": "ACP Taco", "vendor": "Toko A"},
             {"id": 2, "kind": "LABOUR", "qty": 10, "unit_price": 150000, "unit": "hari", "description": "Upah", "vendor": None},
             {"id": 3, "kind": "OTHER", "qty": 1, "unit_price": 0, "unit": None, "description": "Nol", "vendor": None}]
    out = bp.fund_lines(lines, [1, 2, 3])
    assert [x["category"] for x in out] == ["MATERIAL", "SERVICE"] and out[0]["amount"] == Decimal("2400000.0")
    assert out[0]["description"].startswith("ACP Taco (4 lbr") and out[0]["description"].endswith("Toko A")
