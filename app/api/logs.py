"""Logs — global job-event feed and per-project feed."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from app.api.deps import project_scope
from app.i18n import _
from app.repositories.jobs import JobEventRepo
from app.templates import templates

router = APIRouter()


@router.get("/logs", response_class=HTMLResponse)
def logs_page(request: Request, level: str = ""):
    pb = request.state.pb
    scope = project_scope(request)
    f = ""
    if level:
        f = f'eventType="{level}"'
    if scope is None:
        pass  # admin — unrestricted
    elif scope:
        project_part = "(" + " || ".join(f'project="{s}"' for s in scope) + ")"
        f = " && ".join(x for x in (f, project_part) if x)
    else:
        f = " && ".join(x for x in (f, 'project="__no_access__"') if x)
    events = JobEventRepo(pb).list_records(filter=f, sort="-created", page=1, per_page=50)
    return templates.TemplateResponse(
        request,
        "pages/logs/feed.html",
        {"title": _("رویدادها"), "events": events, "level_filter": level},
    )
