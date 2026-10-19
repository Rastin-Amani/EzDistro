"""Dashboard — global overview with live polling."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from app.api.deps import project_scope
from app.repositories.projects import ProjectRepo
from app.services import stats
from app.templates import templates

router = APIRouter()


@router.get("/dashboard", response_class=HTMLResponse)
def dashboard(request: Request, welcome: bool = False):
    scope = project_scope(request)
    projects = (
        ProjectRepo(request.state.pb).list_active()
        if scope is None
        else ProjectRepo(request.state.pb).list_records(
            filter="(" + " || ".join(f'id="{s}"' for s in scope) + ")"
            if scope
            else 'id="__no_access__"',
            sort="name",
        )
    )
    return templates.TemplateResponse(
        request,
        "pages/dashboard.html",
        {
            "title": ("Dashboard"),
            "welcome": welcome,
            "project_overviews": stats.project_overviews(request.state.pb, projects),
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
