"""Rating & Review (skeleton, 2026-10-19): ratings and reviews of the outlets on Google Maps, ShopeeFood, GoFood and
GrabFood.

    GET /api/rating/{source}    page rating.<source>: the branches the user sees (data comes later)

{source} = google-maps | shopeefood | gofood | grabfood. Only the access rules are in place (app/access.py pages, off
for every role until a super admin ticks them in User Accounts → Roles & pages; role "user" sees its own branches).
"""
from fastapi import APIRouter, Depends, HTTPException, Request

from app.access import deny, has_page
from app.routes.auth import current_user
from app.scope import allowed_branches

SOURCES = ("google-maps", "shopeefood", "gofood", "grabfood")
router = APIRouter(tags=["rating"])


@router.get("/api/rating/{source}")
def rating(source: str, request: Request, user: dict = Depends(current_user)) -> dict:
    if source not in SOURCES:
        raise HTTPException(status_code=404, detail="Sumber rating tidak dikenal")
    if not has_page(user, f"rating.{source}"):
        deny(request, user)
    scope = allowed_branches()
    return {"ready": False, "source": source, "branches": None if scope is None else sorted(scope), "data": []}
