"""The notification bell of the portal (app/alerts.py, migration 098): every account reads its own alerts;
the Cost Control ones only who opens their pages (alerts._mine)."""
from typing import Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from app import alerts
from app.routes.auth import current_user

router = APIRouter(prefix="/api/alerts", tags=["alerts"])


@router.get("")
def list_alerts(user: dict = Depends(current_user)) -> dict:
    return alerts.for_user(user)


class ReadIn(BaseModel):
    ids: Optional[list[int]] = Field(None, max_length=500)
    all: bool = False


@router.post("/read")
def read(body: ReadIn, user: dict = Depends(current_user)) -> dict:
    return {"marked": alerts.mark_read(user, body.ids, body.all)}
