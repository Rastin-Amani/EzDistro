"""retry_failed_job handler — human retry as a first-class job.

Payload: {"targetJobId": "..."}. Resets the target failed job to `retrying`
(immediately claimable) and completes. Idempotent: retrying/pending targets
are left untouched.
"""

from __future__ import annotations

from typing import Any

from app.jobs.context import JobContext
from app.jobs.handlers import register_job
from app.repositories.jobs import JobRepo


@register_job("retry_failed_job")
async def handle_retry_failed_job(ctx: JobContext) -> dict[str, Any]:
    target_id = ctx.payload().get("targetJobId") or ""
    if not target_id:
        raise ValueError("retry_failed_job payload is missing targetJobId")

    jobs = JobRepo(ctx.pb)
    target = jobs.get(target_id)
    if not target:
        raise ValueError(f"target job not found: {target_id}")

    if target.get("status") != "failed":
        ctx.info(
            "target job is not failed — nothing to retry",
            {"target": target_id, "status": target.get("status")},
        )
        return {"targetJobId": target_id, "status": target.get("status"), "retried": False}

    jobs.reset_for_retry(target_id)
    ctx.info("target job queued for retry", {"target": target_id})
    return {"targetJobId": target_id, "status": "retrying", "retried": True}
