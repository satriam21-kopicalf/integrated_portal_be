"""The COTR line and the COTR admin (migration 099, 2026-11-13)."""
from app import access
from app.routes import users


def _setup(monkeypatch, overrides=None):
    monkeypatch.setattr(access, "role_pages", lambda: {"user": ["overview", "sales", "stock-opname", "guide", "cotr.storage"],
                                                        "outlet": ["stock-opname"], "cost_control": ["overview"]})
    monkeypatch.setattr(access, "user_overrides", lambda uid: overrides or {})


def test_a_cotr_account_never_opens_the_dashboard_or_sales(monkeypatch):
    _setup(monkeypatch, {"cotr.check-in": True, "overview": True})
    pages = access.pages_for({"id": "u", "role": "user", "business_line": "cotr"})
    assert "overview" not in pages and "sales" not in pages and "stock-opname" not in pages
    assert {"cotr.storage", "cotr.check-in", "guide"} <= set(pages)
    # a Kopi Calf account keeps its pages
    assert "overview" in access.pages_for({"id": "u", "role": "user", "business_line": "kopicalf"})


def test_the_cotr_admin_opens_every_cotr_page_only(monkeypatch):
    _setup(monkeypatch)
    pages = set(access.pages_for({"id": "a", "role": "cotr_admin", "business_line": "cotr"}))
    assert pages == {p for p in access.PAGE_KEYS if access.cotr_page(p)}
    assert "overview" not in pages and "guide" in pages and access.account_admin({"role": "cotr_admin"})
    assert not access.account_admin({"role": "user"})


def test_a_cotr_admin_manages_the_cotr_line_only():
    admin = {"id": "a", "role": "cotr_admin"}
    assert users._out_of_scope(admin, {"business_line": "kopicalf", "role": "user"}) is not None
    assert users._out_of_scope(admin, {"business_line": "cotr", "role": "superadmin"}) is not None
    assert users._out_of_scope(admin, {"business_line": "cotr", "role": "driver"}) is None
    assert users._out_of_scope({"role": "superadmin"}, {"business_line": "kopicalf", "role": "superadmin"}) is None
    data = {"role": "cost_control"}
    assert users._cotr_fields(admin, users.UserFields(role="cost_control"), data).status_code == 403
    data = {"role": "driver"}
    assert users._cotr_fields(admin, users.UserFields(role="driver"), data) is None and data["businessLine"] == "cotr"
    assert users._cotr_fields(admin, users.UserFields(businessLine="kopicalf"), {}).status_code == 403
    assert users._cotr_fields(admin, users.UserFields(pageOverrides={"overview": True}), {}).status_code == 403
    assert users._cotr_fields(admin, users.UserFields(pageOverrides={"cotr.fleet": True}), {}) is None
