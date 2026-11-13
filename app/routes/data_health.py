"""Cost Control: Pusat Informasi (page cc.info), app/data_health.py.

    GET /api/cost-control/health?keys=esbSales,so   the cards of the data checks (all, or the keys) — any Cost Control
                                                    page reads it for its "Sources" line
"""
from typing import Optional

from fastapi import APIRouter, Depends

from app import data_health
from app.access import CC_PAGES, require_page

router = APIRouter(prefix="/api/cost-control", tags=["cost-control"])


@router.get("/health")
def health(keys: Optional[str] = None, _: dict = Depends(require_page(*CC_PAGES))) -> dict:
    return data_health.health([k for k in (keys or "").split(",") if k.strip()] or None)
