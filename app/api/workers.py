"""Workers — operational dashboard: worker liveness, statuses, queue depth,
scheduler health and restart guidance.

Workers report liveness through `worker_heartbeats` (upserted every heartbeat
interval by the worker process). Status is derived from heartbeat age:
active (recent), stale (missed a few beats), offline (old or never seen).
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from app.api.errors import page_guard
from app.config import settings
from app.repositories.jobs import JobEventRepo, JobRepo
from app.repositories.worker_heartbeats import WorkerHeartbeatRepo
from app.templates import templates
from app.utils import fanout

router = APIRouter()

ACTIVE_AFTER_S = 45  # heartbeat every ~15s; 45s → missed ≤2 beats
STALE_AFTER_S = 300  # 5 min without a beat → effectively offline


@router.get("/workers", response_class=HTMLResponse)
@page_guard("Something went wrong loading the workers page — please try again.")
def workers_page(request: Request):
    pb = request.state.pb

    def _heartbeats() -> list[dict[str, Any]]:
        try:
            return WorkerHeartbeatRepo(pb).list_recent(limit=50)
        except Exception:
            # collection missing (bootstrap not run yet) → clean empty state
            return []

    jobs = JobRepo(pb)
    loaded = fanout(
        {
            "heartbeats": _heartbeats,
            "q_pending": lambda: jobs.count(filter='status="pending"'),
            "q_retrying": lambda: jobs.count(filter='status="retrying"'),
            "q_running": lambda: jobs.count(filter='status="running"'),
            "q_failed": lambda: jobs.count(filter='status="failed"'),
            "recent_failed": lambda: jobs.list_records(
                filter='status="failed"', sort="-created", page=1, per_page=6
            ),
            "recent_errors": lambda: JobEventRepo(pb).list_records(
                filter='eventType="job.failed" || eventType="provider_error" || eventType="job.provider_error"',
                sort="-created",
                page=1,
                per_page=8,
            ),
            "scheduler_heartbeat": lambda: _scheduler_heartbeat(pb),
        }
    )

    now = dt.datetime.now(dt.UTC)
    workers = [_decorate(w, now) for w in loaded["heartbeats"]]
    active = sum(1 for w in workers if w["status"] == "active")
    queue = {
        "pending": loaded["q_pending"],
        "retrying": loaded["q_retrying"],
        "running": loaded["q_running"],
        "failed": loaded["q_failed"],
    }
    recent_failed = loaded["recent_failed"]
    recent_errors = loaded["recent_errors"]
    scheduler_heartbeat = loaded["scheduler_heartbeat"]

    return templates.TemplateResponse(
        request,
        "pages/workers/index.html",
        {
            "title": ("Workers"),
            "workers": workers,
            "active_count": active,
            "queue": queue,
            "recent_failed": recent_failed,
            "recent_errors": recent_errors,
            "scheduler_heartbeat": scheduler_heartbeat,
            "app_version": settings.app_version,
            "restart_commands": [
                {
                    "label": ("Run the worker (development)"),
                    "command": "make worker",
                    "hint": ("After every code change, stop and re-run the worker."),
                },
                {
                    "label": ("Run the worker (service)"),
                    "command": "sudo systemctl restart ezdistro-worker",
                    "hint": ("If the worker runs as a systemd service."),
                },
            ],
        },
    )


def _decorate(worker: dict[str, Any], now: dt.datetime) -> dict[str, Any]:
    last = _as_dt(worker.get("lastHeartbeatAt"))
    age_s = (now - last).total_seconds() if last else None
    if age_s is None or age_s > STALE_AFTER_S:
        status = "offline"
    elif age_s > ACTIVE_AFTER_S:
        status = "stale"
    else:
        status = "active"
    out = dict(worker)
    out["status"] = status
    out["age_s"] = age_s
    out["needs_restart"] = (
        bool(worker.get("version")) and str(worker.get("version")) != settings.app_version
    )
    return out


def _scheduler_heartbeat(pb: Any) -> dict[str, Any] | None:
    try:
        from app.repositories.base import record_to_dict

        record = pb.collection("app_settings").get_first_list_item(
            'key="scheduler_heartbeat"', {"perPage": 1}
        )
        return record_to_dict(record).get("value") or None
    except Exception:
        return None


def _as_dt(value: Any) -> dt.datetime | None:
    if not value:
        return None
    if isinstance(value, dt.datetime):
        return value if value.tzinfo else value.replace(tzinfo=dt.UTC)
    try:
        return dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
