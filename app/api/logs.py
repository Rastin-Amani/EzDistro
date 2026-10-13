"""Logs — global job-event feed and per-project feed."""

from __future__ import annotations

import time

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from structlog import get_logger

from app.api.deps import project_scope
from app.api.errors import page_guard
from app.repositories.jobs import JobEventRepo
from app.templates import templates

logger = get_logger(__name__)

router = APIRouter()

# The badge filters offered by feed.html. Anything else in ?level= is ignored
# rather than interpolated straight into a PocketBase filter — a stray quote
# ("err\"||x=\"1") made PocketBase reject the query, which surfaced as a 500.
# A level maps to every eventType that belongs to it: job failures carry
# `job.failed`/`job.provider_error`, not the bare `error`, so filtering on the
# bare value alone showed an empty list even while jobs were failing.
LEVEL_TYPES = {
    "info": ("info",),
    "warning": ("warning", "job.retry_scheduled"),
    "error": ("error", "job.failed", "job.provider_error", "provider_error"),
}


@router.get("/logs", response_class=HTMLResponse)
@page_guard(
    "Something went wrong loading the events page — please try again.",
    back_url="/logs",
    back_label="Try again",
)
def logs_page(request: Request, level: str = ""):
    pb = request.state.pb
    scope = project_scope(request)
    f = ""
    if level in LEVEL_TYPES:
        f = " || ".join(f'eventType="{t}"' for t in LEVEL_TYPES[level])
    else:
        level = ""
    if scope is None:
        pass  # admin — unrestricted
    elif scope:
        project_part = "(" + " || ".join(f'project="{s}"' for s in scope) + ")"
        f = " && ".join(x for x in (f, project_part) if x)
    else:
        f = " && ".join(x for x in (f, 'project="__no_access__"') if x)

    # A read-only feed should never take over the whole page because of one
    # transient PocketBase blip. One retry absorbs a hiccup; if it still fails
    # we render the feed with an inline, retryable error panel instead of the
    # generic dead-end error page.
    events: list[dict] = []
    load_error: str | None = None
    for attempt in (1, 2):
        try:
            events = JobEventRepo(pb).list_records(filter=f, sort="-created", page=1, per_page=50)
            load_error = None
            break
        except Exception as exc:  # noqa: BLE001 — surface a safe message, log the cause
            logger.warning("logs.read_failed", attempt=attempt, error=str(exc))
            load_error = str(exc)
            if attempt == 1:
                time.sleep(0.4)

    return templates.TemplateResponse(
        request,
        "pages/logs/feed.html",
        {"title": ("Events"), "events": events, "level_filter": level, "load_error": load_error},
    )
