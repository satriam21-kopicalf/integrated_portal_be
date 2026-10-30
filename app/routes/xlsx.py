"""POST /api/xlsx: the table a page shows as an Excel file (2026-10-30: the CSV downloads became Excel, as every other
export). The page sends what it shows (the user's own data, already scoped by the endpoints that gave it); the file
has a title row, a bold frozen header and real numbers. Any signed-in user."""
import io
import re
from datetime import datetime
from typing import Any, Optional
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends
from fastapi.responses import Response
from pydantic import BaseModel, Field

from app.routes.auth import current_user
from app.xlsx_stream import StreamingXlsxWriter

router = APIRouter(tags=["xlsx"])
MAX_ROWS = 100_000


class TableIn(BaseModel):
    fileName: str = Field("export", max_length=120)
    sheet: str = Field("Data", max_length=31)
    title: Optional[str] = Field(None, max_length=300)
    header: list[str] = Field(..., min_length=1, max_length=200)
    rows: list[list[Any]] = Field(default_factory=list, max_length=MAX_ROWS)


def build(body: TableIn) -> bytes:
    buf = io.BytesIO()
    stamp = f"Diunduh {datetime.now(ZoneInfo('Asia/Jakarta')):%d/%m/%Y %H:%M} WIB"
    widths = [max(10, min(48, max([len(str(h))] + [len(str(r[i])) for r in body.rows[:300] if i < len(r) and r[i] is not None]) + 2))
              for i, h in enumerate(body.header)]
    with StreamingXlsxWriter(buf) as xlsx:
        sheet = xlsx.add_sheet(re.sub(r"[\[\]:*?/\\]", " ", body.sheet) or "Data", widths=widths, header=body.header,
                               preamble=[[body.title or body.sheet], [stamp], []])
        sheet.write_rows([[v if isinstance(v, (int, float)) or v is None else str(v) for v in r] for r in body.rows])
    return buf.getvalue()


@router.post("/api/xlsx")
def to_xlsx(body: TableIn, user: dict = Depends(current_user)) -> Response:
    name = re.sub(r"[^\w .()-]", "", body.fileName).strip() or "export"
    return Response(build(body), media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"content-disposition": f'attachment; filename="{name}.xlsx"'})
