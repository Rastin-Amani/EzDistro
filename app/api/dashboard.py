"""Dashboard — global overview with live polling."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from app.api.deps import project_scope
from app.services import stats
from app.templates import templates

router = APIRouter()


@router.get("/dashboard", response_class=HTMLResponse)
def dashboard(request: Request, welcome: bool = False):
    scope = project_scope(request)
    return templates.TemplateResponse(
        request,
        "pages/dashboard.html",
        {
            "title": ("Dashboard"),
            "welcome": welcome,
            "stats": stats.global_stats(request.state.pb, scope),
            "recent_jobs": stats.recent_jobs(request.state.pb, scope, limit=8),
            "recent_events": stats.recent_events(request.state.pb, scope, limit=8),
        },
    )


@router.get("/dashboard/partial/stats", response_class=HTMLResponse)
def dashboard_stats_partial(request: Request):
    scope = project_scope(request)
    return templates.TemplateResponse(
        request,
        "pages/dashboard/_stats.html",
        {"stats": stats.global_stats(request.state.pb, scope)},
    )
