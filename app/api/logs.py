"""Logs — global job-event feed and per-project feed."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from app.api.deps import project_scope
from app.api.errors import page_guard
from app.repositories.jobs import JobEventRepo
from app.templates import templates

router = APIRouter()

# The badge filters offered by feed.html. Anything else in ?level= is ignored
# rather than interpolated straight into a PocketBase filter — a stray quote
# ("err\"||x=\"1") made PocketBase reject the query, which surfaced as a 500.
LEVELS = ("info", "warning", "error")


@router.get("/logs", response_class=HTMLResponse)
@page_guard(
    "Something went wrong loading the events page — please try again.",
    back_url="/dashboard",
    back_label="Back to dashboard",
)
def logs_page(request: Request, level: str = ""):
    pb = request.state.pb
    scope = project_scope(request)
    f = ""
    if level in LEVELS:
        f = f'eventType="{level}"'
    else:
        level = ""
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
        {"title": ("Events"), "events": events, "level_filter": level},
    )
