"""Rating & Review: ratings and reviews of the outlets on Google Maps, ShopeeFood, GoFood and GrabFood.

    GET  /api/rating/google-maps                     page rating.google-maps: the outlets in scope with their place on
                                                     Google Maps (app/gmaps.py, Places API)
    GET  /api/rating/google-maps/search?q&branch     super admin / Cost Control: places for a text, near the outlet
    PUT  /api/rating/google-maps/places/{branch}     super admin / Cost Control: {"placeId"} (null = not on Google Maps)
    POST /api/rating/google-maps/sync                super admin / Cost Control: match and refresh now (background)
    GET  /api/rating/{source}                        page rating.<source>: ShopeeFood, GoFood, GrabFood (being built)

The pages are off for every role until a super admin ticks them in User Accounts → Roles & pages; role "user" sees its
own branches.
"""
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from app import activity, gmaps
from app.access import deny, has_page
from app.routes.auth import current_user
from app.scope import allowed_branches

SOURCES = ("google-maps", "shopeefood", "gofood", "grabfood")
GMAPS = "rating.google-maps"
EDITORS = ("superadmin", "cost_control")
router = APIRouter(tags=["rating"])


class PlaceIn(BaseModel):
    placeId: Optional[str] = Field(None, min_length=5, max_length=300, pattern=r"^[A-Za-z0-9_-]+$")


def _page(request: Request, user: dict) -> None:
    if not has_page(user, GMAPS):
        deny(request, user)


def _editor(request: Request, user: dict) -> None:
    _page(request, user)
    if user.get("role") not in EDITORS:
        deny(request, user)


def _places_error(e: gmaps.PlacesError) -> HTTPException:
    return HTTPException(status_code=502 if "Google Places API" in str(e) else 409, detail=str(e))


@router.get("/api/rating/google-maps")
def google_maps(request: Request, user: dict = Depends(current_user)) -> dict:
    _page(request, user)
    scope = allowed_branches()
    return {"ready": gmaps.enabled(), "source": "google-maps", "syncing": gmaps.running(),
            "canEdit": user.get("role") in EDITORS, "data": gmaps.overview(scope)}


@router.get("/api/rating/google-maps/search")
def google_maps_search(request: Request, q: str = Query(min_length=2, max_length=120), branch: Optional[str] = None,
                       user: dict = Depends(current_user)) -> dict:
    _editor(request, user)
    outlet = next((o for o in gmaps.outlets() if o["code"] == branch), None) if branch else None
    try:
        found = gmaps.search(q, (outlet or {}).get("latitude"), (outlet or {}).get("longitude"), limit=8)
    except gmaps.PlacesError as e:
        raise _places_error(e) from None
    return {"data": found}


@router.put("/api/rating/google-maps/places/{branch}")
def google_maps_place(branch: str, body: PlaceIn, request: Request, user: dict = Depends(current_user)) -> dict:
    _editor(request, user)
    try:
        result = gmaps.set_place(branch, body.placeId, user)
    except gmaps.PlacesError as e:
        raise _places_error(e) from None
    activity.record("system.gmaps_place", user=user, request=request, page="/rating/google-maps",
                    summary=f"Google Maps {branch}: {'bukan di Google Maps' if body.placeId is None else body.placeId}",
                    details={"branch": branch, "placeId": body.placeId, **result})
    return result


@router.post("/api/rating/google-maps/sync", status_code=202)
def google_maps_sync(request: Request, user: dict = Depends(current_user)) -> dict:
    _editor(request, user)
    if not gmaps.enabled():
        raise HTTPException(status_code=409, detail="Google Places API key belum diatur di server")
    if gmaps.running():
        return {"started": False, "syncing": True}
    gmaps.sync_in_background()
    activity.record("system.gmaps_sync", user=user, request=request, page="/rating/google-maps",
                    summary="Sinkronisasi Google Maps dijalankan")
    return {"started": True, "syncing": True}


@router.get("/api/rating/{source}")
def rating(source: str, request: Request, user: dict = Depends(current_user)) -> dict:
    if source not in SOURCES:
        raise HTTPException(status_code=404, detail="Sumber rating tidak dikenal")
    if not has_page(user, f"rating.{source}"):
        deny(request, user)
    scope = allowed_branches()
    return {"ready": False, "source": source, "branches": None if scope is None else sorted(scope), "data": []}
