"""Rating & Review: ratings and reviews of the outlets on Google Maps, ShopeeFood, GoFood and GrabFood.

    GET  /api/rating/google-maps                     page rating.google-maps: the outlets in scope with their place on
                                                     Google Maps (app/gmaps.py, Places API)
    GET  /api/rating/google-maps/search?q&branch     super admin / Cost Control: places for a text, near the outlet
    PUT  /api/rating/google-maps/places/{branch}     super admin / Cost Control: {"placeId"} (null = not on Google Maps)
    POST /api/rating/google-maps/sync                super admin / Cost Control: match and refresh now (background)
    GET  /api/rating/grabfood                        page rating.grabfood: every Grab outlet in scope (status, rating, reviews)
    GET  /api/rating/grabfood/reviews                every review in scope, filtered and paged
    GET  /api/rating/grabfood/reviews/{review_id}    one review (preview, photos)
    GET  /api/rating/grabfood/menu                   menu availability: out of stock / switched off items and add-ons
    PUT  /api/rating/grabfood/session                super admin only: refresh the Grab merchant session
    GET  /api/rating/grabfood/branches               super admin: ESB branches to map a Grab store to
    PUT  /api/rating/grabfood/stores/{id}/branch     super admin: map a Grab store to its ESB branch
    GET  /api/rating/{source}                        page rating.<source>: ShopeeFood, GoFood (being built)

The pages are off for every role until a super admin ticks them in User Accounts → Roles & pages; role "user" sees its
own branches.
"""
from datetime import date
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from app import activity, gmaps, grab
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


class SessionIn(BaseModel):
    cookie: str = Field(min_length=20, max_length=20000)
    csrf: Optional[str] = Field(None, max_length=200)


GRAB = "rating.grabfood"
GRAB_SESSION_ROLES = ("superadmin",)  # only a super admin sees and refreshes the merchant session (2026-10-27)


def _grab_page(request: Request, user: dict) -> None:
    if not has_page(user, GRAB):
        deny(request, user)


@router.get("/api/rating/grabfood")
def grabfood(request: Request, user: dict = Depends(current_user)) -> dict:
    """Page rating.grabfood: every outlet in scope with its open/close status, official rating and reviews."""
    _grab_page(request, user)
    admin = user.get("role") in GRAB_SESSION_ROLES
    return {"ready": True, "source": "grabfood", "canEdit": admin,
            "session": grab.session_info() if admin else None,
            "data": grab.overview(allowed_branches())}


@router.get("/api/rating/grabfood/reviews")
def grabfood_reviews(request: Request, merchant: Optional[str] = Query(None, max_length=40),
                     branch: Optional[str] = Query(None, max_length=40), rating: Optional[str] = Query(None, pattern=r"^[1-5](,[1-5])*$"),
                     photo: Optional[bool] = None, replied: Optional[bool] = None, q: Optional[str] = Query(None, max_length=120),
                     date_from: Optional[date] = Query(None, alias="from"), date_to: Optional[date] = Query(None, alias="to"),
                     page: int = Query(1, ge=1, le=10000), size: int = Query(20, ge=1, le=100),
                     user: dict = Depends(current_user)) -> dict:
    """Every GrabFood review in scope, newest first; filters: outlet, stars (e.g. 1,2), with photo, replied, text, period."""
    _grab_page(request, user)
    return grab.reviews(allowed_branches(), merchant=merchant, branch=branch,
                        ratings=[int(x) for x in rating.split(",")] if rating else None, photo=photo, replied=replied, q=q,
                        date_from=date_from, date_to=date_to, page=page, size=size)


@router.get("/api/rating/grabfood/reviews/{review_id}")
def grabfood_review(review_id: str, request: Request, user: dict = Depends(current_user)) -> dict:
    """One review for the preview (text, items, aspects, photos)."""
    _grab_page(request, user)
    r = grab.review(review_id, allowed_branches())
    if r is None:
        raise HTTPException(status_code=404, detail="Ulasan tidak ditemukan")
    return r


@router.get("/api/rating/grabfood/menu")
def grabfood_menu(request: Request, merchant: Optional[str] = Query(None, max_length=40),
                  status: str = Query("unavailable", pattern=r"^(unavailable|all|AVAILABLE|OUT_OF_STOCK|OFF)$"),
                  kind: Optional[str] = Query(None, pattern=r"^(item|modifier)$"), q: Optional[str] = Query(None, max_length=120),
                  user: dict = Depends(current_user)) -> dict:
    """GrabFood menu availability of the outlets in scope: out of stock / switched off items and add-ons (or all)."""
    _grab_page(request, user)
    return grab.menu(allowed_branches(), merchant=merchant, status=status, kind=kind, q=q)


@router.get("/api/rating/grabfood/{branch}/reviews")
def grabfood_branch_reviews(branch: str, request: Request, user: dict = Depends(current_user)) -> dict:
    """Older clients: the latest reviews of one outlet."""
    _grab_page(request, user)
    return grab.reviews(allowed_branches(), branch=branch, size=100)


class BranchIn(BaseModel):
    branchCode: Optional[str] = Field(None, max_length=40, pattern=r"^[A-Za-z0-9_-]+$")


@router.get("/api/rating/grabfood/branches")
def grabfood_branches(request: Request, user: dict = Depends(current_user)) -> dict:
    """Super admin: ESB branches to map a Grab store to."""
    _grab_page(request, user)
    if user.get("role") not in GRAB_SESSION_ROLES:
        deny(request, user)
    return {"data": grab.branch_options()}


@router.put("/api/rating/grabfood/stores/{merchant_id}/branch")
def grabfood_store_branch(merchant_id: str, body: BranchIn, request: Request, user: dict = Depends(current_user)) -> dict:
    """Super admin: map a Grab store to its ESB branch (null = not mapped); the branch scope of every user follows it."""
    _grab_page(request, user)
    if user.get("role") not in GRAB_SESSION_ROLES:
        deny(request, user)
    try:
        result = grab.set_branch(merchant_id, body.branchCode)
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e)) from None
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e)) from None
    activity.record("system.grab_branch", user=user, request=request, page="/rating/grabfood",
                    summary=f"Outlet Grab {merchant_id} -> {body.branchCode or 'tidak dipetakan'}", details=result)
    return result


@router.put("/api/rating/grabfood/session")
def grabfood_session(body: SessionIn, request: Request, user: dict = Depends(current_user)) -> dict:
    """Super admin only: refresh the merchant session cookie (pasted from the Grab portal)."""
    _grab_page(request, user)
    if user.get("role") not in GRAB_SESSION_ROLES:
        deny(request, user)
    try:
        result = grab.set_session(body.cookie, body.csrf, user)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from None
    activity.record("system.grab_session", user=user, request=request, page="/rating/grabfood",
                    summary="Sesi Grab Merchant diperbarui")
    return result


@router.get("/api/rating/{source}")
def rating(source: str, request: Request, user: dict = Depends(current_user)) -> dict:
    if source not in SOURCES:
        raise HTTPException(status_code=404, detail="Sumber rating tidak dikenal")
    if not has_page(user, f"rating.{source}"):
        deny(request, user)
    scope = allowed_branches()
    return {"ready": False, "source": source, "branches": None if scope is None else sorted(scope), "data": []}
