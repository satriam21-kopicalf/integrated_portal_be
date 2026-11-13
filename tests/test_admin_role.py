"""Role Admin (migration 105, 2026-11-13): menus set by the superadmin, manages role-User accounts only, gives only the
pages it has, never changes its own access."""
from app import access
from app.routes import users


def _setup(monkeypatch, overrides=None):
    monkeypatch.setattr(access, "role_pages", lambda: {"admin": ["overview", "sales", "form.product", "mon.overview", "guide"],
                                                        "user": list(access.USER_PAGES), "outlet": [], "cost_control": []})
    monkeypatch.setattr(access, "user_overrides", lambda uid: overrides or {})


def test_the_admin_opens_what_the_superadmin_gave_it(monkeypatch):
    _setup(monkeypatch, {"cc.ratio": True, "sales": False})
    pages = set(access.pages_for({"id": "a", "role": "admin"}))
    assert pages == {"overview", "form.product", "mon.overview", "guide", "cc.ratio"}
    assert access.account_admin({"role": "admin"}) and "admin" in access.EDITABLE_ROLES
    assert access.so_role({"id": "a", "role": "admin"}) == "cost_control"


def test_the_admin_manages_role_user_accounts_only(monkeypatch):
    _setup(monkeypatch)
    admin = {"id": "a", "role": "admin"}
    assert users._out_of_scope(admin, {"id": "u", "role": "user", "business_line": "kopicalf"}) is None
    for role in ("superadmin", "admin", "cost_control", "outlet"):
        assert users._out_of_scope(admin, {"id": "x", "role": role, "business_line": "kopicalf"}).status_code == 403
    assert users._out_of_scope(admin, {"id": "a", "role": "user", "business_line": "kopicalf"}).status_code == 403  # itself
    data = {"role": "cost_control"}
    assert users._admin_fields(admin, users.UserFields(role="cost_control"), data).status_code == 403
    data = {}
    assert users._admin_fields(admin, users.UserFields(), data) is None and data["role"] == "user"
    # only the pages it opens itself
    assert users._admin_fields(admin, users.UserFields(pageOverrides={"mon.overview": True, "form.wwwr": False}), {}) is None
    assert users._admin_fields(admin, users.UserFields(pageOverrides={"cc.ratio": True}), {}).status_code == 403
    assert users._admin_fields(admin, users.UserFields(businessLine="cotr"), {}).status_code == 403
    # a superadmin is not limited
    assert users._admin_fields({"id": "s", "role": "superadmin"}, users.UserFields(role="admin"), {"role": "admin"}) is None
