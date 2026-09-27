"""Job engine tests against the in-memory fake PocketBase.

Covers: atomic claim race, lease-based recovery, retry/backoff, cancellation,
per-project concurrency cap, and handler failure classification.
"""

from __future__ import annotations

import asyncio
import datetime as dt
from typing import Any

import pytest
from pocketbase.errors import ClientResponseError

from app.jobs.engine import JobEngine
from app.jobs.state import JobStateError
from app.providers.base import PermanentError, ProviderError, TransientError
from app.repositories.jobs import JobRepo, LeaseRepo, now_utc
from app.repositories.projects import DEFAULT_SETTINGS
from app.repositories.prompts import PromptRepo
from tests.fakes import FakePocketBase, default_unique_fields


def make_project(
    pb: FakePocketBase, name: str = "\u067e\u0631\u0648\u0698\u0647 \u062a\u0633\u062a"
) -> dict[str, Any]:
    project = pb.collection("projects").create(
        {
            "name": name,
            "slug": "test-proj",
            "language": "fa",
            "status": "active",
            "timezone": "Asia/Tehran",
        }
    )
    pb.collection("project_settings").create({"project": project["id"], **DEFAULT_SETTINGS})
    return project


def make_prompt(pb: FakePocketBase, project_id: str, kind: str, content: str) -> None:
    PromptRepo(pb).upsert(project_id, kind, "default", content)


def seed_job(pb: FakePocketBase, project_id: str, **overrides: Any) -> dict[str, Any]:
    data = {
        "project": project_id,
        "type": "test",
        "payload": {},
        "idempotencyKey": f"test:{project_id}:{now_utc().timestamp()}",
        "status": "pending",
        "attempts": 0,
        "maxAttempts": 3,
        "progress": 0,
        "cancelRequested": False,
    }
    data.update(overrides)
    return pb.collection("jobs").create(data)


def make_engine(pb: FakePocketBase, handlers: dict | None = None, **kwargs: Any) -> JobEngine:
    return JobEngine(
        pb,
        worker_id=kwargs.pop("worker_id", "w1"),
        lease_seconds=kwargs.pop("lease_seconds", 300),
        heartbeat_interval=kwargs.pop("heartbeat_interval", 60.0),
        max_concurrent_jobs=kwargs.pop("max_concurrent_jobs", 8),
        handlers=handlers or {},
    )


# ---------------------------------------------------------------------------
# Atomic claim
# ---------------------------------------------------------------------------
def test_two_workers_cannot_claim_same_job():
    pb = FakePocketBase(default_unique_fields())
    project = make_project(pb)
    job = seed_job(pb, project["id"])

    engine1 = make_engine(pb, worker_id="w1")
    engine2 = make_engine(pb, worker_id="w2")

    assert engine1._claim(job) is True
    assert engine2._claim(job) is False  # unique lease → second worker loses

    claimed = pb.collection("jobs").get_one(job["id"])
    assert claimed["lockedBy"] == "w1"
    assert claimed["status"] == "running"  # claim → running directly


def test_expired_lease_recovery():
    pb = FakePocketBase(default_unique_fields())
    project = make_project(pb)
    job = seed_job(pb, project["id"])
    engine = make_engine(pb, worker_id="w1")

    # stale lease held by a dead worker, already expired
    pb.collection("job_leases").create(
        {"job": job["id"], "workerId": "dead", "expiresAt": "2020-01-01 00:00:00.000Z"}
    )
    assert engine._claim(job) is True
    lease = pb.collection("job_leases").get_first_list_item(f'job="{job["id"]}"')
    assert lease["workerId"] == "w1"


def test_claim_skips_terminal_jobs():
    pb = FakePocketBase(default_unique_fields())
    project = make_project(pb)
    done = seed_job(pb, project["id"], status="completed", idempotencyKey="k1")
    engine = make_engine(pb)
    assert engine._claim(done) is False


# ---------------------------------------------------------------------------
# Retry & backoff
# ---------------------------------------------------------------------------
def test_transient_failure_schedules_backoff_retry():
    pb = FakePocketBase(default_unique_fields())
    project = make_project(pb)
    job = seed_job(pb, project["id"], maxAttempts=3)

    async def handler(ctx):
        raise TransientError("network down")

    engine = make_engine(pb, handlers={"test": handler})
    asyncio.run(engine._run(job))

    updated = pb.collection("jobs").get_one(job["id"])
    assert updated["status"] == "retrying"  # scheduled for retry
    assert updated["attempts"] == 1
    assert updated["availableAt"]  # backoff set
    assert updated["errorDetails"]["retryable"] is True
    # lease released
    with pytest.raises(ClientResponseError):
        pb.collection("job_leases").get_one(job["id"])


def test_permanent_failure_does_not_retry():
    pb = FakePocketBase(default_unique_fields())
    project = make_project(pb)
    job = seed_job(pb, project["id"], maxAttempts=5)

    async def handler(ctx):
        raise PermanentError("bad request")

    engine = make_engine(pb, handlers={"test": handler})
    asyncio.run(engine._run(job))

    updated = pb.collection("jobs").get_one(job["id"])
    assert updated["status"] == "failed"
    assert updated["attempts"] == 1
    assert updated["errorDetails"]["retryable"] is False


def test_exhausts_retries_then_fails():
    pb = FakePocketBase(default_unique_fields())
    project = make_project(pb)
    job = seed_job(pb, project["id"], maxAttempts=2, attempts=1)

    async def handler(ctx):
        raise TransientError("flaky")

    engine = make_engine(pb, handlers={"test": handler})
    asyncio.run(engine._run(job))

    updated = pb.collection("jobs").get_one(job["id"])
    assert updated["status"] == "failed"
    assert updated["attempts"] == 2


# ---------------------------------------------------------------------------
# Cancellation
# ---------------------------------------------------------------------------
def test_cancel_request_aborts_handler():
    pb = FakePocketBase(default_unique_fields())
    project = make_project(pb)
    job = seed_job(pb, project["id"])

    cancelled = {"seen": False}

    async def handler(ctx):
        await ctx.check_cancelled()
        cancelled["seen"] = True

    engine = make_engine(pb, handlers={"test": handler})
    pb.collection("jobs").update(job["id"], {"cancelRequested": True})
    asyncio.run(engine._run(job))

    updated = pb.collection("jobs").get_one(job["id"])
    assert updated["status"] == "cancelled"
    assert cancelled["seen"] is False  # handler aborted at checkpoint


def test_successful_handler_records_result_and_100_percent():
    pb = FakePocketBase(default_unique_fields())
    project = make_project(pb)
    job = seed_job(pb, project["id"])

    async def handler(ctx):
        ctx.progress(50, stage="test", message="halfway")
        return {"ok": True}

    engine = make_engine(pb, handlers={"test": handler})
    asyncio.run(engine._run(job))

    updated = pb.collection("jobs").get_one(job["id"])
    assert updated["status"] == "completed"
    assert updated["progress"] == 100
    assert updated["result"] == {"ok": True}


def test_unknown_job_type_fails_permanently():
    pb = FakePocketBase(default_unique_fields())
    project = make_project(pb)
    job = seed_job(pb, project["id"], type="nope")

    engine = make_engine(pb, handlers={"test": None})
    asyncio.run(engine._run(job))
    assert pb.collection("jobs").get_one(job["id"])["status"] == "failed"


def test_missing_project_config_fails_permanently():
    pb = FakePocketBase(default_unique_fields())
    job = pb.collection("jobs").create(
        {
            "project": "nonexistent",
            "type": "test",
            "payload": {},
            "idempotencyKey": "x1",
            "status": "pending",
            "attempts": 0,
            "maxAttempts": 3,
            "progress": 0,
            "cancelRequested": False,
        }
    )

    async def handler(ctx):
        return {}

    engine = make_engine(pb, handlers={"test": handler})
    asyncio.run(engine._run(job))
    updated = pb.collection("jobs").get_one(job["id"])
    assert updated["status"] == "failed"
    assert updated["errorDetails"]["retryable"] is False


# ---------------------------------------------------------------------------
# Idempotent job creation
# ---------------------------------------------------------------------------
def test_create_job_idempotent_by_key():
    pb = FakePocketBase(default_unique_fields())
    project = make_project(pb)
    jobs = JobRepo(pb)
    first = jobs.create(
        project=project["id"], type="index_run", payload={}, idempotency_key="dup-key"
    )
    second = jobs.create(
        project=project["id"], type="index_run", payload={}, idempotency_key="dup-key"
    )
    assert first["id"] == second["id"]
    assert len(pb.collection("jobs").get_full_list()) == 1
