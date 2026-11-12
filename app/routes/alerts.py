"""The notification bell of the portal (app/alerts.py, migration 098): for whoever opens Good Receipt or COGS & Usage."""
from typing import Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from app import alerts
from app.access import require_page

router = APIRouter(prefix="/api/alerts", tags=["alerts"])


@router.get("")
def list_alerts(user: dict = Depends(require_page(*alerts.PAGES))) -> dict:
    return alerts.for_user(user)


class ReadIn(BaseModel):
    ids: Optional[list[int]] = Field(None, max_length=500)
    all: bool = False


@router.post("/read")
def read(body: ReadIn, user: dict = Depends(require_page(*alerts.PAGES))) -> dict:
    return {"marked": alerts.mark_read(user, body.ids, body.all)}
