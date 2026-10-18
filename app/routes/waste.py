"""Waste (skeleton): outlets enter the waste of their branch, Cost Control monitors and reviews it.

    GET  /api/waste/entries          page "waste": the branches the user enters waste for + its entries
    GET  /api/waste/monitor          page cc.waste-monitor: progress of every branch
    GET  /api/waste/reviews          page cc.waste-review: entries waiting for review
    POST /api/waste/reviews/{id}     page cc.waste-review: approve / send back (not built yet: 501)

Only the access rules are final here (app/access.py pages, same as the menu; role "user" sees its own branches only,
app/scope.py). The data model and the screens come next; until then the lists are empty.
"""
from fastapi import APIRouter, Depends, HTTPException

from app.access import require_page
from app.scope import allowed_branches

router = APIRouter(prefix="/api/waste", tags=["waste"])
SKELETON = {"ready": False, "message": "Waste is being built: only the access rules are in place."}


@router.get("/entries", dependencies=[Depends(require_page("waste"))])
def entries() -> dict:
    scope = allowed_branches()
    return {**SKELETON, "branches": None if scope is None else sorted(scope), "data": []}


@router.get("/monitor", dependencies=[Depends(require_page("cc.waste-monitor"))])
def monitor() -> dict:
    return {**SKELETON, "data": []}


@router.get("/reviews", dependencies=[Depends(require_page("cc.waste-review"))])
def reviews() -> dict:
    return {**SKELETON, "data": []}


@router.post("/reviews/{entry_id}", dependencies=[Depends(require_page("cc.waste-review"))])
def review(entry_id: str) -> dict:
    raise HTTPException(status_code=501, detail="Review waste belum tersedia")
