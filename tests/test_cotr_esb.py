"""The ESB company COTR in the pool stock (migration 090): sales, transfers and cupping / waste journals per HUB."""
from datetime import date

from app import cotr_esb as E

MENUS = {c: {"code": c, "name": n} for c, n in [("PRM", "Calf Premium Cup"), ("CAN", "Premium Can 250"), ("CHO", "Chocolate Can 250"),
                                                 ("PEC", "Smooth Pecan 250"), ("MTC", "Matcha Latte")]}


def test_esb_documents_become_the_figures_of_the_hubs(monkeypatch):
    d = date(2026, 11, 10)
    monkeypatch.setattr(E, "_menu_map", lambda: MENUS)
    monkeypatch.setattr(E, "branches", lambda: {"COTR HUB CIPETE": {"pool": "HUB-CPT"}, "COTR HUB TEBET": {"pool": "HUB-TBT"},
                                                "STORAGE PALMERAH": {"pool": "STK-PLM"}})
    rows = [
        {"module": "simple_sales", "doc_date": d, "branch_name": "COTR HUB CIPETE", "counterparty_name": "Customer", "product_name": "Es Kopi Calf Premium - COTR (145ml)", "qty": 4465, "purpose": None},
        {"module": "simple_transfer", "doc_date": d, "branch_name": "STORAGE PALMERAH", "counterparty_name": "COTR HUB CIPETE", "product_name": "ICED CHOCOLATE - COTR (230ml)", "qty": 100, "purpose": None},
        {"module": "simple_transfer", "doc_date": d, "branch_name": "COTR HUB TEBET", "counterparty_name": "COTR HUB CIPETE", "product_name": "PREMIUM CAN - COTR (230ml)", "qty": 20, "purpose": None},
        {"module": "item_journal", "doc_date": d, "branch_name": "COTR HUB CIPETE", "counterparty_name": None, "product_name": "Matcha Latte - COTR (145ml)", "qty": -3, "purpose": "CUPPING"},
        {"module": "item_journal", "doc_date": d, "branch_name": "COTR HUB CIPETE", "counterparty_name": None, "product_name": "SMOOTH VANILLA PECAN - COTR (230ml)", "qty": -2, "purpose": "WASTING"},
        {"module": "item_journal", "doc_date": d, "branch_name": "COTR HUB CIPETE", "counterparty_name": None, "product_name": "PREMIX PREMIUM NEW", "qty": -500, "purpose": "CUPPING"},
        {"module": "simple_transfer", "doc_date": d, "branch_name": "STORAGE PALMERAH", "counterparty_name": "COTR HUB CIPETE", "product_name": "TRASH BAG 90X120CM (NEW)", "qty": 5, "purpose": None},
    ]
    monkeypatch.setattr(E.db, "fetch", lambda q, p=None: rows)
    out = E.figures(d, d)
    cpt = out["HUB-CPT"][d]
    assert cpt["PRM"] == {"sold": 4465} and cpt["CHO"] == {"arrived": 100} and cpt["CAN"] == {"transfer": 20}
    assert cpt["MTC"] == {"cupping": 3} and cpt["PEC"] == {"waste": 2} and out["HUB-TBT"][d]["CAN"] == {"transfer": -20}
