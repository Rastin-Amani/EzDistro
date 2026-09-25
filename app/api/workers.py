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
from app.i18n import _
from app.repositories.jobs import JobEventRepo, JobRepo
from app.repositories.worker_heartbeats import WorkerHeartbeatRepo
from app.templates import templates

router = APIRouter()

ACTIVE_AFTER_S = 45  # heartbeat every ~15s; 45s → missed ≤2 beats
STALE_AFTER_S = 300  # 5 min without a beat → effectively offline


@router.get("/workers", response_class=HTMLResponse)
@page_guard(_("مشکلی در بارگذاری صفحه کارگرها پیش آمد — دوباره تلاش کنید."))
def workers_page(request: Request):
    pb = request.state.pb
    try:
        heartbeats = WorkerHeartbeatRepo(pb).list_recent(limit=50)
    except Exception:
        # collection missing (bootstrap not run yet) → clean empty state
        heartbeats = []

    now = dt.datetime.now(dt.UTC)
    workers = [_decorate(w, now) for w in heartbeats]
    active = sum(1 for w in workers if w["status"] == "active")

    jobs = JobRepo(pb)
    queue = {
        "pending": jobs.count(filter='status="pending"'),
        "retrying": jobs.count(filter='status="retrying"'),
        "running": jobs.count(filter='status="running"'),
        "failed": jobs.count(filter='status="failed"'),
    }

    recent_failed = jobs.list_records(filter='status="failed"', sort="-created", page=1, per_page=6)

    recent_errors = JobEventRepo(pb).list_records(
        filter='eventType="job.failed" || eventType="provider_error" || eventType="job.provider_error"',
        sort="-created",
        page=1,
        per_page=8,
    )

    scheduler_heartbeat = _scheduler_heartbeat(pb)

    return templates.TemplateResponse(
        request,
        "pages/workers/index.html",
        {
            "title": _("کارگرها"),
            "workers": workers,
            "active_count": active,
            "queue": queue,
            "recent_failed": recent_failed,
            "recent_errors": recent_errors,
            "scheduler_heartbeat": scheduler_heartbeat,
            "app_version": settings.app_version,
            "restart_commands": [
                {
                    "label": _("راه‌اندازی کارگر (توسعه)"),
                    "command": "make worker",
                    "hint": _("پس از هر تغییر کد، کارگر را متوقف و دوباره اجرا کنید."),
                },
                {
                    "label": _("راه‌اندازی کارگر (سرویس)"),
                    "command": "sudo systemctl restart ezdistro-worker",
                    "hint": _("اگر کارگر به‌صورت سرویس systemd اجرا می‌شود."),
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
