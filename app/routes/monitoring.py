"""Forms and Monitoring (skeleton): outlets / PICs fill in four forms, the monitoring pages follow them.

    GET  /api/forms/{form}                 page form.<form>: the branches the user fills the form for + its entries
    POST /api/forms/{form}                 page form.<form>: submit (not built yet: 501)
    GET  /api/monitoring/{form}            page mon.<form>: entries of every branch the user sees

{form} = pic-check-in | outlet | product | tools (PIC Check In, Monitoring Outlet, Monitoring Product, Tools & Heavy
Tools). Only the access rules are final here (app/access.py pages, like the menu; role "user" sees its own branches,
app/scope.py); no role opens these pages until a super admin ticks them in User Accounts → Roles & pages.
"""
from fastapi import APIRouter, Depends, HTTPException, Request

from app.access import has_page, deny
from app.routes.auth import current_user
from app.scope import allowed_branches

FORMS = ("pic-check-in", "outlet", "product", "tools")
router = APIRouter(tags=["monitoring"])
SKELETON = {"ready": False, "message": "This form is being built: only the access rules are in place."}


def _page(prefix: str, form: str, request: Request, user: dict) -> None:
    if form not in FORMS:
        raise HTTPException(status_code=404, detail="Form tidak dikenal")
    if not has_page(user, f"{prefix}.{form}"):
        deny(request, user)


@router.get("/api/forms/{form}")
def form_entries(form: str, request: Request, user: dict = Depends(current_user)) -> dict:
    _page("form", form, request, user)
    scope = allowed_branches()
    return {**SKELETON, "form": form, "branches": None if scope is None else sorted(scope), "data": []}


@router.post("/api/forms/{form}")
def form_submit(form: str, request: Request, user: dict = Depends(current_user)) -> dict:
    _page("form", form, request, user)
    raise HTTPException(status_code=501, detail="Form belum tersedia")


@router.get("/api/monitoring/{form}")
def monitoring(form: str, request: Request, user: dict = Depends(current_user)) -> dict:
    _page("mon", form, request, user)
    return {**SKELETON, "form": form, "data": []}
