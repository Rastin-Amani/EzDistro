"""Jobs — admin monitor (filters), job detail with timeline, retry/cancel.

Routers only parse HTTP/HTMX; technical error details are only rendered for
admins (users see safe summaries).
"""

from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from app.api.deps import project_scope, require_hx, require_user
from app.api.errors import hx_error, page_guard
from app.jobs.handlers import JOB_TYPES
from app.repositories.jobs import JobEventRepo, JobRepo
from app.repositories.projects import ProjectRepo
from app.templates import templates
from app.utils import error_response, fanout, success_response, toast_response

router = APIRouter()

JOB_STATUSES = ["pending", "retrying", "running", "completed", "failed", "cancelled"]


def _job_accessible(request: Request, job: dict | None) -> bool:
    if not job:
        return False
    scope = project_scope(request)
    return scope is None or job.get("project") in scope


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


@router.get("/jobs", response_class=HTMLResponse)
@page_guard("Something went wrong loading the jobs page — please try again.")
def jobs_monitor(
    request: Request,
    project: str = "",
    job_type: str = "",
    status: str = "",
    worker: str = "",
    failure_type: str = "",
    date_from: str = "",
    date_to: str = "",
    page: int = 1,
):
    pb = request.state.pb
    scope = project_scope(request)
    # non-admins can only filter by their own projects
    if scope is not None and project not in scope:
        project = ""
    per_page = 25
    # The scope filter speaks the `jobs` collection's language (`project="id"`),
    # but this list is the `projects` collection itself — match on `id` instead.
    if scope is None:
        project_filter = ""
    elif scope:
        project_filter = "(" + " || ".join(f'id="{s}"' for s in scope) + ")"
    else:
        project_filter = 'id="__no_access__"'
    from app.services.metrics import query_provider_metrics

    loaded = fanout(
        {
            "search": lambda: JobRepo(pb).search(
                project_id=project or (scope[0] if scope and len(scope) == 1 else ""),
                job_type=job_type,
                status=status,
                worker=worker,
                failure_type=failure_type,
                date_from=date_from,
                date_to=date_to,
                page=max(1, page),
                per_page=per_page,
            ),
            "projects": lambda: ProjectRepo(pb).list_records(
                filter=project_filter, sort="name", per_page=200
            ),
            "error_codes": lambda: JobRepo(pb).error_codes(),
            "provider_metrics": lambda: query_provider_metrics(pb, project_id=project, days=7),
            "scheduler_heartbeat": lambda: _scheduler_heartbeat(pb),
        }
    )
    rows, total = loaded["search"]
    projects = loaded["projects"]
    error_codes = loaded["error_codes"]
    provider_metrics = loaded["provider_metrics"]
    scheduler_heartbeat = loaded["scheduler_heartbeat"]
    return templates.TemplateResponse(
        request,
        "pages/jobs/monitor.html",
        {
            "title": ("Jobs"),
            "provider_metrics": provider_metrics,
            "scheduler_heartbeat": scheduler_heartbeat,
            "jobs": rows,
            "total": total,
            "page": max(1, page),
            "pages": max(1, -(-total // per_page)),
            "projects": projects,
            "job_types": JOB_TYPES,
            "statuses": JOB_STATUSES,
            "error_codes": error_codes,
            "f": {
                "project": project,
                "job_type": job_type,
                "status": status,
                "worker": worker,
                "failure_type": failure_type,
                "date_from": date_from,
                "date_to": date_to,
            },
        },
    )


@router.get("/failed", response_class=HTMLResponse)
@page_guard("Something went wrong loading the failed jobs page — please try again.")
def failed_jobs(request: Request, page: int = 1):
    """Failed jobs (dead-letter view) — retry from here or open the detail."""
    pb = request.state.pb
    scope = project_scope(request)
    f = 'status="failed"'
    if scope is None:
        pass  # admin — unrestricted
    elif scope:
        f += " && (" + " || ".join(f'project="{s}"' for s in scope) + ")"
    else:
        f += ' && project="__no_access__"'
    rows, total = JobRepo(pb).search(status="failed", page=max(1, page), per_page=25)
    jobs = JobRepo(pb).list_records(filter=f, sort="-updated", page=max(1, page), per_page=25)
    return templates.TemplateResponse(
        request,
        "pages/jobs/failed.html",
        {
            "title": ("Failed jobs"),
            "jobs": jobs,
            "total": total,
            "page": max(1, page),
            "pages": max(1, -(-total // 25)),
        },
    )


@router.get("/jobs/{job_id}", response_class=HTMLResponse)
def job_detail(request: Request, job_id: str):
    pb = request.state.pb
    job = JobRepo(pb).get(job_id)
    if not job or not _job_accessible(request, job):
        return templates.TemplateResponse(
            request, "pages/jobs/not_found.html", {"title": ("Job not found")}
        )
    events = JobEventRepo(pb).list_for_job(job_id, per_page=100)
    project = ProjectRepo(pb).get(job.get("project") or "")
    user = require_user(request)
    is_admin = user.get("role") == "admin"
    return templates.TemplateResponse(
        request,
        "pages/jobs/detail.html",
        {
            "title": f"Job {job.get('type', '')}",
            "job": job,
            "events": events,
            "project": project,
            "is_admin": is_admin,
        },
    )


@router.get("/jobs/{job_id}/events", response_class=HTMLResponse)
def job_events_fragment(request: Request, job_id: str):
    """Live timeline fragment — polled while the job is active."""
    job = JobRepo(request.state.pb).get(job_id)
    if not job or not _job_accessible(request, job):
        return HTMLResponse("")
    events = JobEventRepo(request.state.pb).list_for_job(job_id, per_page=100)
    user = require_user(request)
    return templates.TemplateResponse(
        request,
        "pages/jobs/_timeline.html",
        {"job": job, "events": events, "is_admin": user.get("role") == "admin"},
    )


@router.post("/jobs/{job_id}/cancel")
@hx_error("Cancelling job failed")
def cancel_job(request: Request, job_id: str):
    require_hx(request)
    require_user(request)
    job = JobRepo(request.state.pb).get(job_id)
    if not _job_accessible(request, job):
        return error_response("Job not found")
    JobRepo(request.state.pb).request_cancel(job_id)
    return toast_response(
        ("Job cancellation requested"), type="warning", extra_events={"refreshJobs": True}
    )


@router.post("/jobs/{job_id}/retry")
@hx_error("Retry failed")
def retry_job(request: Request, job_id: str):
    """Human retry — enqueues a `retry_failed_job` job (auditable, idempotent)."""
    require_hx(request)
    require_user(request)
    job = JobRepo(request.state.pb).get(job_id)
    if not _job_accessible(request, job):
        return error_response("Job not found")
    if not job or job.get("status") != "failed":
        return toast_response(("This job is not in the failed state"), type="warning")
    existing = JobRepo(request.state.pb).first(
        filter=f'type="retry_failed_job" && payload.targetJobId="{job_id}" && (status="pending" || status="retrying" || status="running")'
    )
    if not existing:
        JobRepo(request.state.pb).create(
            project=str(job["project"]),
            type="retry_failed_job",
            payload={"targetJobId": job_id},
            idempotency_key=f"retry:job:{job_id}:{int(time.time())}",
            max_attempts=1,
            entity_type="job",
            entity_id=job_id,
        )
    return success_response(("Retry scheduled"), extra_events={"refreshJobs": True})
