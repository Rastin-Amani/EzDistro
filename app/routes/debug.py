import os
from fastapi import APIRouter, Request

router = APIRouter(prefix="/debug", tags=["debug"])


@router.get("/test")
def debug_test(request: Request):
    pb = request.state.pb
    user = request.state.user
    return {
        "user": user.id if user else None,
    }


if os.getenv("ENV", "dev").lower() == "production":
    router.routes.clear()
