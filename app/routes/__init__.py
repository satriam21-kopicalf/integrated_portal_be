"""Routes package."""
from app.routes.auth import router as auth_router
from app.routes.avatars import router as avatars_router
from app.routes.branches import router as branches_router
from app.routes.cost_control import router as cost_control_router
from app.routes.exports import router as exports_router
from app.routes.live import router as live_router
from app.routes.overview import router as overview_router
from app.routes.realtime import router as realtime_router
from app.routes.transactions import router as transactions_router
from app.routes.transactions import summary_router
from app.routes.users import router as users_router

__all__ = ["cost_control_router", "transactions_router", "summary_router", "branches_router", "exports_router", "overview_router", "live_router", "realtime_router", "auth_router", "users_router", "avatars_router"]
