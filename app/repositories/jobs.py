"""Jobs, leases and job-events repositories — the heart of the async job engine.

Atomic claiming relies on the `job_leases` collection's UNIQUE constraint on `job`:
creating a lease is a compare-and-swap — only one worker can win per job.

State model (docs/SCHEMA.md §7): pending / retrying / running / completed /
failed / cancelled. `availableAt` gates retries (retrying) and scheduled jobs;
`cancelRequested` drives cooperative cancellation.
"""

from __future__ import annotations

import contextlib
import datetime as dt
from typing import Any

from pocketbase.errors import ClientResponseError

from app.repositories.base import BaseRepo

JOB_STATUSES = ("pending", "running", "completed", "failed", "cancelled", "retrying")
ACTIVE_STATUSES = ("pending", "retrying", "running")

UTC = dt.UTC


def pb_dt(d: dt.datetime) -> str:
    """Format a datetime the way PocketBase stores/comparses date fields."""
    return d.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3] + "Z"


def now_utc() -> dt.datetime:
    return dt.datetime.now(UTC)


class JobRepo(BaseRepo):
    collection = "jobs"

    # -- creation (idempotent) ------------------------------------------------------
    def create(
        self,
        *,
        project: str,
        type: str,
        payload: dict[str, Any],
        idempotency_key: str,
        max_attempts: int = 3,
        parent: str = "",
        entity_type: str = "",
        entity_id: str = "",
        priority: int = 0,
        available_at: dt.datetime | None = None,
    ) -> dict[str, Any]:
        existing = self.first(filter=f'idempotencyKey="{idempotency_key}"')
        if existing:
            return existing
        record = super().create(
            {
                "project": project,
                "type": type,
                "entityType": entity_type,
                "entityId": entity_id,
                "status": "pending",
                "priority": priority,
                "payload": payload,
                "progress": 0,
                "stage": "",
                "currentItem": 0,
                "totalItems": 0,
                "attempts": 0,
                "maxAttempts": max_attempts,
                "availableAt": pb_dt(available_at) if available_at else pb_dt(now_utc()),
                "cancelRequested": False,
                "parent": parent or "",
                "idempotencyKey": idempotency_key,
            }
        )
        self._event(record["id"], "job.created", "job created", {"type": type, "project": project})
        return record

    # -- polling --------------------------------------------------------------------
    def pending_candidates(self, limit: int = 50) -> list[dict[str, Any]]:
        """Jobs ready to run: pending/retrying & due, or running with an expired lease."""
        now = pb_dt(now_utc())
        stale = self.list_records(
            filter=f'status="running" && leaseExpiresAt < "{now}"',
            sort="created",
            page=1,
            per_page=limit,
        )
        fresh = self.list_records(
            filter=f'(status="pending" || status="retrying") && availableAt <= "{now}"',
            sort="-priority,created",
            page=1,
            per_page=limit,
        )
        return fresh + stale

    def pending_count_for_project(self, project_id: str) -> int:
        return self.count(
            filter=f'project="{project_id}" && (status="pending" || status="retrying" || status="running")'
        )

    # -- claiming (lease → status=running directly) ----------------------------------
    def mark_running(
        self, job_id: str, worker_id: str, lease_expires: dt.datetime
    ) -> dict[str, Any]:
        now = now_utc()
        return self.update(
            job_id,
            {
                "status": "running",
                "lockedBy": worker_id,
                "lockedAt": pb_dt(now),
                "leaseExpiresAt": pb_dt(lease_expires),
                "heartbeatAt": pb_dt(now),
                "startedAt": pb_dt(now),
                "availableAt": "",
            },
        )

    def heartbeat(self, job_id: str, lease_expires: dt.datetime) -> None:
        self.update(
            job_id,
            {"heartbeatAt": pb_dt(now_utc()), "leaseExpiresAt": pb_dt(lease_expires)},
        )

    # -- progress -------------------------------------------------------------------
    def set_progress(
        self,
        job_id: str,
        progress: int,
        *,
        stage: str = "",
        message: str = "",
        current_item: int = 0,
        total_items: int = 0,
    ) -> None:
        payload: dict[str, Any] = {"progress": max(0, min(100, int(progress)))}
        if stage:
            payload["stage"] = stage
        if message:
            payload["progressMessage"] = message
        if current_item:
            payload["currentItem"] = current_item
        if total_items:
            payload["totalItems"] = total_items
        self.update(job_id, payload)

    # -- finalization ---------------------------------------------------------------
    def complete(self, job_id: str, result: dict[str, Any] | None = None) -> None:
        payload: dict[str, Any] = {
            "status": "completed",
            "progress": 100,
            "progressMessage": "",
            "lockedBy": "",
            "finishedAt": pb_dt(now_utc()),
        }
        if result is not None:
            payload["result"] = result
        self.update(job_id, payload)
        self._event(job_id, "job.completed", "job completed", {"result": result or {}})

    def fail(
        self,
        job_id: str,
        error_code: str,
        error_message: str,
        error_details: dict[str, Any] | None = None,
    ) -> None:
        self.update(
            job_id,
            {
                "status": "failed",
                "errorCode": error_code,
                "errorMessage": error_message,
                "errorDetails": error_details or {},
                "lockedBy": "",
                "finishedAt": pb_dt(now_utc()),
            },
        )
        self._event(job_id, "job.failed", f"job failed: {error_message}", error_details or {})

    def schedule_retry(
        self,
        job_id: str,
        next_available_at: dt.datetime,
        attempt: int,
        max_attempts: int,
        delay_seconds: int,
    ) -> None:
        self.update(
            job_id,
            {
                "status": "retrying",
                "availableAt": pb_dt(next_available_at),
                "attempts": attempt,
                "lockedBy": "",
            },
        )
        self._event(
            job_id,
            "job.retry_scheduled",
            f"retry scheduled (attempt {attempt}/{max_attempts}) in {delay_seconds}s",
            {
                "attempt": attempt,
                "max_attempts": max_attempts,
                "delay_seconds": delay_seconds,
                "available_at": pb_dt(next_available_at),
            },
        )

    def cancel(self, job_id: str) -> dict[str, Any]:
        record = self.update(
            job_id,
            {
                "status": "cancelled",
                "cancelRequested": True,
                "lockedBy": "",
                "finishedAt": pb_dt(now_utc()),
            },
        )
        self._event(job_id, "job.cancelled", "job cancelled", {})
        return record

    def request_cancel(self, job_id: str) -> dict[str, Any]:
        return self.update(job_id, {"cancelRequested": True})

    def reset_for_retry(self, job_id: str) -> dict[str, Any]:
        """Human retry: failed → retrying, immediately claimable."""
        record = self.update(
            job_id,
            {
                "status": "retrying",
                "availableAt": pb_dt(now_utc()),
                "errorCode": "",
                "errorMessage": "",
                "errorDetails": {},
                "cancelRequested": False,
                "lockedBy": "",
                "finishedAt": "",
            },
        )
        self._event(job_id, "job.retry_scheduled", "manual retry requested", {"manual": True})
        return record

    # -- queries --------------------------------------------------------------------
    def list_for_project(
        self, project_id: str, *, status: str | None = None, page: int = 1, per_page: int = 25
    ) -> list[dict[str, Any]]:
        f = f'project="{project_id}"'
        if status:
            f += f' && status="{status}"'
        return self.list_records(filter=f, sort="-created", page=page, per_page=per_page)

    def failed_jobs(
        self, page: int = 1, per_page: int = 25, project_id: str = ""
    ) -> list[dict[str, Any]]:
        f = 'status="failed"'
        if project_id:
            f = f'project="{project_id}" && ' + f
        return self.list_records(filter=f, sort="-updated", page=page, per_page=per_page)

    def search(
        self,
        *,
        project_id: str = "",
        job_type: str = "",
        status: str = "",
        worker: str = "",
        failure_type: str = "",
        date_from: str = "",
        date_to: str = "",
        page: int = 1,
        per_page: int = 25,
    ) -> tuple[list[dict[str, Any]], int]:
        """Filtered job search (admin monitor). Returns (rows, total)."""
        parts = []
        if project_id:
            parts.append(f'project="{project_id}"')
        if job_type:
            parts.append(f'type="{job_type}"')
        if status:
            parts.append(f'status="{status}"')
        if worker:
            parts.append(f'lockedBy="{worker}"')
        if failure_type:
            parts.append(f'errorCode="{failure_type}"')
        if date_from:
            parts.append(f'created >= "{date_from} 00:00:00.000Z"')
        if date_to:
            parts.append(f'created <= "{date_to} 23:59:59.999Z"')
        f = " && ".join(parts)
        total = self.count(filter=f)
        rows = self.list_records(filter=f, sort="-created", page=page, per_page=per_page)
        return rows, total

    def error_codes(self, per_page: int = 100) -> list[str]:
        """Distinct failure types present on failed jobs (bounded sample)."""
        failed = self.list_records(
            filter='status="failed"', sort="-updated", page=1, per_page=per_page
        )
        return sorted({str(j.get("errorCode") or "Unknown") for j in failed})

    def is_duplicate(self, idempotency_key: str) -> bool:
        return self.first(filter=f'idempotencyKey="{idempotency_key}"') is not None

    def for_entity(
        self, entity_type: str, entity_id: str, *, status: str | None = None
    ) -> list[dict[str, Any]]:
        f = f'entityType="{entity_type}" && entityId="{entity_id}"'
        if status:
            f += f' && status="{status}"'
        return self.list_records(filter=f, sort="-created", page=1, per_page=10)

    # -- event helper (audit; never breaks the job pipeline) -------------------------
    def _event(self, job_id: str, event_type: str, message: str, metadata: dict[str, Any]) -> None:
        job = self.get(job_id)
        if not job:
            return
        with contextlib.suppress(Exception):
            self._pb.collection("job_events").create(
                {
                    "project": job.get("project", ""),
                    "job": job_id,
                    "eventType": event_type,
                    "message": message,
                    "metadata": metadata,
                }
            )


class LeaseRepo(BaseRepo):
    collection = "job_leases"

    def acquire(self, job_id: str, worker_id: str, expires_at: dt.datetime) -> bool:
        """Atomically claim a job. Returns True only if THIS worker won the race."""
        try:
            self.create({"job": job_id, "workerId": worker_id, "expiresAt": pb_dt(expires_at)})
            return True
        except ClientResponseError as err:
            if err.status == 400:
                return False
            raise

    def current(self, job_id: str) -> dict[str, Any] | None:
        return self.first(filter=f'job="{job_id}"')

    def delete_expired(self) -> None:
        expired = pb_dt(now_utc())
        stale = self.list_all(filter=f'expiresAt < "{expired}"')
        for lease in stale:
            with contextlib.suppress(Exception):
                self.delete(lease["id"])

    def release(self, job_id: str) -> None:
        lease = self.current(job_id)
        if lease:
            with contextlib.suppress(Exception):
                self.delete(lease["id"])

    def extend(self, job_id: str, expires_at: dt.datetime) -> None:
        lease = self.current(job_id)
        if lease:
            self.update(lease["id"], {"expiresAt": pb_dt(expires_at)})


class JobEventRepo(BaseRepo):
    collection = "job_events"

    def add(
        self,
        *,
        project: str,
        job: str = "",
        event_type: str = "info",
        message: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> None:
        with contextlib.suppress(Exception):
            # Event logging must never break the job pipeline.
            self.create(
                {
                    "project": project,
                    "job": job or "",
                    "eventType": event_type,
                    "message": message,
                    "metadata": metadata or {},
                }
            )

    def list_for_job(self, job_id: str, page: int = 1, per_page: int = 50) -> list[dict[str, Any]]:
        return self.list_records(
            filter=f'job="{job_id}"', sort="created", page=page, per_page=per_page
        )

    def list_for_project(
        self, project_id: str, *, level: str | None = None, page: int = 1, per_page: int = 50
    ) -> list[dict[str, Any]]:
        f = f'project="{project_id}"'
        if level:
            f += f' && eventType="{level}"'
        return self.list_records(filter=f, sort="-created", page=page, per_page=per_page)

    def recent(self, page: int = 1, per_page: int = 50) -> list[dict[str, Any]]:
        return self.list_records(filter="", sort="-created", page=page, per_page=per_page)
