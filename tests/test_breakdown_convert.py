from app.menu_breakdown import convert


def test_largest_pack_from_base():
    assert convert(1500, "ML", ("ML", {"BTL": 750, "ML": 1})) == {"convQty": 2.0, "convUom": "BTL", "convFactor": 750, "baseUom": "ML"}


def test_recipe_unit_through_base():
    assert convert(2, "KG", ("GR", {"PCS": 500, "BOX": 5000}))["convQty"] == 0.4


def test_no_pack_or_incomparable():
    assert convert(10, "GR", None) == {}
    assert convert(10, "GR", ("GR", {"GR": 1})) == {}
    assert convert(10, "PCS", ("ML", {"BTL": 750})) == {}
