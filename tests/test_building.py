"""Building & Renovation catalog (app/building.py): prices typed in and how old a price is."""
from datetime import date
from decimal import Decimal

import pytest

from app import building as bd


@pytest.mark.parametrize("raw,want", [
    ("Rp 1.250.000", "1250000.00"), ("1,250,000.50", "1250000.50"), ("1.250.000,50", "1250000.50"), (600000, "600000.00"),
    ("125.000", "125000.00"), ("37,5", "37.50"), ("12.5", "12.50"),
])
def test_price_typed_in(raw, want):
    assert bd.price_of(raw) == Decimal(want)


def test_price_empty_or_wrong():
    assert bd.price_of(None) is None and bd.price_of("  ") is None
    with pytest.raises(bd.BuildingError):
        bd.price_of(-5)


def test_age():
    assert bd.age_days(date(2026, 9, 1), date(2026, 11, 27)) == 87
    assert bd.age_days(None) is None


def test_cost_line_keeps_its_jenis_transaksi_and_material():
    """The route model must carry transactionId (required by save_cost_item) and materialId (2026-11-27)."""
    from app.routes.damaged_equipment import CostItemIn
    d = CostItemIn(description="ACP Taco", transactionId=5, materialId=9).model_dump()
    assert d["transactionId"] == 5 and d["materialId"] == 9
