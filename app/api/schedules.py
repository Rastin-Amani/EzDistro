"""Schedules — operational monitor for every project's automatic runs.

Read-only: it answers "what runs next, for which project, and what will that run
actually do". Turning a schedule on/off stays on the project's Settings tab.

Cost: four independent reads in one fanout wave (schedules, projects, worker
scheduler heartbeat, recent scheduled jobs), plus one bounded readiness lookup
per project that has an enabled schedule — a second wave, never nested.
"""

from __future__ import annotations

import datetime as dt
from functools import partial
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from app.api.deps import project_scope
from app.api.errors import page_guard
from app.repositories.articles import ArticleRepo
from app.repositories.indexing import DocumentRepo
from app.repositories.jobs import JobRepo, now_utc
from app.repositories.projects import ProjectRepo, ProjectSettingsRepo
from app.repositories.schedules import ScheduleRepo
from app.repositories.topics import TopicRepo
from app.templates import templates
from app.utils import fanout

router = APIRouter()

KIND_LABELS = {
    "index": "Indexing",
    "write": "Article writing",
    "publish": "Publishing",
}

# Job types the three schedule kinds create — the activity feed's filter.
SCHEDULE_JOB_TYPES = ("index_project", "write_article", "publish_article")

# Past its next run by more than this → the worker is not draining the schedule
# (worker down, or the project was deactivated after the row was written).
LATE_AFTER = dt.timedelta(minutes=20)
_MAX_ACTIVITY = 12
_MAX_DT = dt.datetime.max.replace(tzinfo=dt.UTC)
_STATE_RANK = {"late": 0, "due": 1, "scheduled": 2, "paused": 3, "inactive": 4, "unscheduled": 5}


def _interval_label(minutes: int) -> str:
    if minutes < 60:
        return f"every {minutes} min"
    hours = minutes // 60
    if hours < 24:
        return f"every {hours} h"
    days = hours // 24
    return "every day" if days == 1 else f"every {days} days"


def _duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds} s"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes} min"
    hours = minutes // 60
    if hours < 24:
        return f"{hours} h {minutes % 60} min" if minutes % 60 else f"{hours} h"
    days = hours // 24
    return f"{days} d {hours % 24} h" if hours % 24 else f"{days} d"


def _readiness(pb: Any, project_id: str, kinds: set[str]) -> dict[str, dict[str, Any]]:
    """What the next fire of each enabled kind will actually do, from live data."""
    out: dict[str, dict[str, Any]] = {}
    if "write" in kinds:
        topic = TopicRepo(pb).next_unwritten(project_id)
        out["write"] = (
            {"state": "ready", "subject": str(topic.get("title") or topic.get("keyword") or "")}
            if topic
            else {"state": "idle"}
        )
    if "index" in kinds:
        indexed = DocumentRepo(pb).count_indexed(project_id)
        out["index"] = {"state": "ready" if indexed else "idle", "count": indexed}
    if "publish" in kinds:
        auto = ProjectSettingsRepo(pb).get_for_project(project_id).get("autoPublish") or {}
        min_score = int(auto.get("min_score") or 0)
        if not auto.get("enabled"):
            # An enabled schedule with auto-publish off fires and skips — the
            # owner needs to see that, not a green "scheduled" row.
            out["publish"] = {"state": "off", "min_score": min_score}
        else:
            article = ArticleRepo(pb).next_to_publish(project_id, min_score)
            out["publish"] = {
                "state": "ready" if article else "idle",
                "subject": str((article or {}).get("title") or ""),
                "min_score": min_score,
            }
    return out


def _scheduler_heartbeat(pb: Any) -> dict[str, Any] | None:
    """Last schedule-poll outcome written by the worker (diagnostics)."""
    try:
        from app.repositories.base import record_to_dict

        record = pb.collection("app_settings").get_first_list_item(
            'key="scheduler_heartbeat"', {"perPage": 1}
        )
        return record_to_dict(record).get("value") or None
    except Exception:
        return None


@router.get("/schedules", response_class=HTMLResponse)
@page_guard("Something went wrong loading the schedules page — please try again.")
def schedules_page(request: Request):
    pb = request.state.pb
    scope = project_scope(request)

    if scope is None:
        sched_filter = ""
        project_filter = ""
    elif scope:
        sched_filter = "(" + " || ".join(f'project="{s}"' for s in scope) + ")"
        project_filter = "(" + " || ".join(f'id="{s}"' for s in scope) + ")"
    else:
        # A user with no memberships sees nothing — never everything.
        sched_filter = 'project="__no_access__"'
        project_filter = 'id="__no_access__"'

    jobs_clause = "(" + " || ".join(f'type="{t}"' for t in SCHEDULE_JOB_TYPES) + ")"
    activity_filter = f"{jobs_clause} && {sched_filter}" if sched_filter else jobs_clause

    loaded = fanout(
        {
            # Schedules are few (≤3 per project); list_all is accurate without
            # ever loading an unbounded result set.
            "schedules": lambda: ScheduleRepo(pb).list_all(filter=sched_filter, sort="nextRunAt"),
            "projects": lambda: ProjectRepo(pb).list_all(filter=project_filter, sort="name"),
            "heartbeat": lambda: _scheduler_heartbeat(pb),
            "activity": lambda: JobRepo(pb).list_records(
                filter=activity_filter,
                sort="-created",
                per_page=_MAX_ACTIVITY,
                fields="id,type,status,created,project,payload,errorMessage",
            ),
        }
    )
    projects = {p["id"]: p for p in loaded["projects"]}
    schedules = [s for s in loaded["schedules"] if s.get("project") in projects]

    # One readiness lookup per project with at least one enabled, active schedule
    # (a paused or inactive-project row will not fire, so its forecast is moot).
    # ponytail: ~3 lookups per project, never per row — fine at this product's
    # scale; paginate the table if a deployment ever runs hundreds of projects.
    wants: dict[str, set[str]] = {}
    for sched in schedules:
        kind = str(sched.get("kind") or "")
        project = projects.get(sched.get("project") or "")
        if sched.get("enabled") and kind in KIND_LABELS and _project_active(project):
            wants.setdefault(str(sched["project"]), set()).add(kind)
    readiness = (
        fanout({pid: partial(_readiness, pb, pid, kinds) for pid, kinds in wants.items()})
        if wants
        else {}
    )

    now = now_utc()
    rows = [
        _decorate(sched, projects[sched["project"]], readiness.get(str(sched["project"]), {}), now)
        for sched in schedules
    ]
    rows.sort(key=lambda r: (r["rank"], r["next_at"] or _MAX_DT))
    counts = {
        "active": sum(1 for r in rows if r["state"] in ("scheduled", "due", "late")),
        "due": sum(1 for r in rows if r["state"] == "due"),
        "late": sum(1 for r in rows if r["state"] == "late"),
        "paused": sum(1 for r in rows if r["state"] == "paused"),
        "inactive": sum(1 for r in rows if r["state"] == "inactive"),
    }
    soonest = next((r for r in rows if r["state"] in ("due", "scheduled")), None)
    overdue = next((r for r in rows if r["state"] == "late"), None)

    resp = templates.TemplateResponse(
        request,
        "pages/schedules/index.html",
        {
            "title": ("Automation"),
            "rows": rows,
            "counts": counts,
            "soonest": soonest,
            "overdue": overdue,
            "heartbeat": loaded["heartbeat"],
            "activity": [
                {
                    **job,
                    "project_name": (projects.get(job.get("project") or "") or {}).get("name")
                    or "",
                    "scheduled": (job.get("payload") or {}).get("trigger") == "schedule",
                }
                for job in loaded["activity"]
            ],
            "total_schedules": len(rows),
        },
    )
    # The page polls itself; without this the browser replays the first response.
    resp.headers["Cache-Control"] = "no-store"
    return resp


def _project_active(project: dict[str, Any] | None) -> bool:
    return (project or {}).get("status", "active") == "active"


def _decorate(
    sched: dict[str, Any],
    project: dict[str, Any],
    readiness: dict[str, dict[str, Any]],
    now: dt.datetime,
) -> dict[str, Any]:
    next_at = _as_dt(sched.get("nextRunAt"))
    last_at = _as_dt(sched.get("lastRunAt"))
    kind = str(sched.get("kind") or "")

    if not sched.get("enabled"):
        state = "paused"
    elif not _project_active(project):
        state = "inactive"
    elif next_at is None:
        state = "unscheduled"
    elif next_at <= now:
        state = "late" if now - next_at > LATE_AFTER else "due"
    else:
        state = "scheduled"

    if next_at is None or state in ("paused", "inactive", "unscheduled"):
        next_label = "—"
    elif state == "due":
        # Set at creation/enable, so it sits a few seconds in the past until the
        # poller fires — "due now" reads better than "0 s late".
        next_label = "due now"
    elif state == "late":
        next_label = f"{_duration((now - next_at).total_seconds())} late"
    else:
        next_label = f"in {_duration((next_at - now).total_seconds())}"

    interval = max(1, int(sched.get("intervalMinutes") or 60))
    return {
        "id": sched.get("id") or "",
        "project_id": str(sched.get("project") or ""),
        "project_name": project.get("name") or "",
        "kind": kind,
        "kind_label": KIND_LABELS.get(kind, kind or "—"),
        "enabled": bool(sched.get("enabled")),
        "interval_label": _interval_label(interval),
        "state": state,
        "rank": _STATE_RANK.get(state, 9),
        "next_at": next_at,
        "next_label": next_label,
        "last_at": last_at,
        "readiness": readiness.get(kind),
    }


def _as_dt(value: Any) -> dt.datetime | None:
    if not value:
        return None
    if isinstance(value, dt.datetime):
        return value if value.tzinfo else value.replace(tzinfo=dt.UTC)
    try:
        return dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
