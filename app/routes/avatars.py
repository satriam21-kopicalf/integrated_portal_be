"""Profile pictures (any signed-in user may view them).

    GET /api/avatars/{user_id}?v=<version>

The URL carries the image version (user_account.avatar_updated_at), so the
response can be cached by the browser for a year. Uploads: PUT/DELETE
/api/auth/me/avatar (own) and /api/users/{id}/avatar (superadmin).
"""
from uuid import UUID

from fastapi import APIRouter
from fastapi.responses import JSONResponse, Response

from app import avatars

router = APIRouter(prefix="/api/avatars", tags=["avatars"])


@router.get("/{user_id}")
def get_avatar(user_id: UUID):
    row = avatars.load(str(user_id))
    if not row:
        return JSONResponse({"error": "No profile picture"}, status_code=404)
    return Response(bytes(row["data"]), media_type=row["content_type"],
                    headers={"Cache-Control": "private, max-age=31536000, immutable"})
