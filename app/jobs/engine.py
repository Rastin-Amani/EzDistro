"""Job engine — atomic lease claiming, execution, heartbeat, retries, finalization.

Correctness never depends on worker memory: every decision is re-read from
PocketBase. The worker may crash at ANY point:
- after lease creation → job is `running` with an expiring lease; another
  worker reclaims it once `leaseExpiresAt` passes (crash recovery)
- mid-execution → same recovery; handlers are idempotent
- during finalization → the state update is atomic per field batch
"""

from __future__ import annotations

import asyncio
import datetime as dt
import random
import time
import traceback
from typing import Any

import structlog

from app.jobs.context import JobCancelled, JobContext, ProviderStack
from app.jobs.state import validate_transition
from app.providers.base import ProviderError
from app.providers.registry import ProviderRegistry
from app.repositories.jobs import JobEventRepo, JobRepo, LeaseRepo, now_utc
from app.repositories.projects import ProjectSettingsRepo
from app.services.settings import ProjectConfig

logger = structlog.get_logger("worker.engine")

# `jobs.errorMessage` is a 5000-char text field (PocketBase's default cap), so a
# failure message must be clipped before it is written back.
ERROR_MESSAGE_MAX = 5000


class JobEngine:
    def __init__(
        self,
        pb: Any,
        *,
        worker_id: str,
        lease_seconds: int = 300,
        heartbeat_interval: float = 15.0,
        max_concurrent_jobs: int = 4,
        llm_concurrency: int = 4,
        embedding_concurrency: int = 4,
        publish_concurrency: int = 2,
        handlers: dict[str, Any] | None = None,
    ) -> None:
        self.pb = pb
        self.worker_id = worker_id
        self.lease_seconds = lease_seconds
        self.heartbeat_interval = heartbeat_interval
        self.jobs = JobRepo(pb)
        self.leases = LeaseRepo(pb)
        self.events = JobEventRepo(pb)
        self.registry = ProviderRegistry(pb)
        self._semaphore = asyncio.Semaphore(max(1, max_concurrent_jobs))
        # Lightweight provider metrics accumulator (flushed by the worker loop).
        from app.services.metrics import MetricsAccumulator, ProviderMetricsObserver

        self._metrics = MetricsAccumulator()
        self._metrics_observer = ProviderMetricsObserver(self._metrics)
        # Bounded provider concurrency (rate limits).
        self._llm_sem = asyncio.Semaphore(max(1, llm_concurrency))
        self._embedding_sem = asyncio.Semaphore(max(1, embedding_concurrency))
        self._publish_sem = asyncio.Semaphore(max(1, publish_concurrency))
        self._handlers = handlers or {}
        self._completed = 0
        self._failed = 0
        self._cancelled = 0
        # ProjectConfig is immutable once loaded and identical for every job of a
        # project, but ProjectConfig.load issues ~7 PocketBase queries. The engine
        # is bound to a single PocketBase client, so caching per project_id is
        # safe; a 60s TTL bounds staleness if project settings change mid-batch.
        # ponytail: per-engine cache; multi-worker uses each worker's own cache.
        self._config_cache: dict[str, tuple[ProjectConfig, float]] = {}
        self._config_ttl = 60.0

    def stats(self) -> dict[str, int]:
        return {
            "completed": self._completed,
            "failed": self._failed,
            "cancelled": self._cancelled,
        }

    # ---------------------------------------------------------------------------
    # Polling
    # ---------------------------------------------------------------------------
    async def poll_and_run(self) -> None:
        """One poll cycle: claim candidates, then execute them under the semaphore."""
        candidates = self.jobs.pending_candidates(limit=50)
        for job in candidates:
            if job.get("status") not in ("pending", "retrying", "running"):
                continue
            if not self._claim(job):
                continue
            asyncio.create_task(self._run_guarded(job))

    def _claim(self, job: dict[str, Any]) -> bool:
        """Atomic claim via the unique job_leases constraint. Returns True on win.

        The winner transitions the job straight to `running` with lock fields
        and a fresh lease; losers (including crash-recovery races) back off.
        """
        job_id = job["id"]
        expires = now_utc() + dt.timedelta(seconds=self.lease_seconds)
        if not self.leases.acquire(job_id, self.worker_id, expires):
            # Lost the race — but if the existing lease is already expired we can
            # clean it up and retry once (abandoned-job recovery).
            current = self.leases.current(job_id)
            if current and _lease_expired(current):
                self.leases.delete(current["id"])
                if not self.leases.acquire(job_id, self.worker_id, expires):
                    return False
            else:
                return False
        # Only claim jobs that are still claimable (state may have changed).
        fresh = self.jobs.get(job_id)
        if not fresh or fresh.get("status") not in ("pending", "retrying", "running"):
            self.leases.release(job_id)
            return False
        validate_transition(fresh.get("status", "pending"), "running")
        self.jobs.mark_running(job_id, self.worker_id, expires)
        self.events.add(
            project=fresh.get("project", ""),
            job=job_id,
            event_type="job.claimed",
            message=f"job claimed by worker {self.worker_id}",
            metadata={"worker_id": self.worker_id, "lease_expires_at": expires.isoformat()},
        )
        return True

    # ---------------------------------------------------------------------------
    # Execution
    # ---------------------------------------------------------------------------
    async def _run_guarded(self, job: dict[str, Any]) -> None:
        async with self._semaphore:
            try:
                await self._run(job)
            except Exception:
                logger.exception("job execution crashed", job_id=job.get("id"))

    def _config_for(self, project_id: str) -> ProjectConfig:
        """Return the project config, using a short-TTL per-engine cache.

        Eliminates the ~7 PocketBase queries ProjectConfig.load issues for every
        job of the same project (the dominant redundant read in the pipeline).
        """
        cached = self._config_cache.get(project_id)
        if cached is not None and (time.monotonic() - cached[1]) < self._config_ttl:
            return cached[0]
        config = ProjectConfig.load(self.pb, project_id)
        self._config_cache[project_id] = (config, time.monotonic())
        return config

    async def _run(self, job: dict[str, Any]) -> None:
        job_id = job["id"]
        project_id = job["project"]
        handler = self._handlers.get(job.get("type"))
        if handler is None:
            self.jobs.fail(
                job_id,
                "UnknownJobType",
                f"no handler for job type {job.get('type')!r}",
                {"retryable": False},
            )
            self._failed += 1
            self.leases.release(job_id)
            return

        self.events.add(
            project=project_id, job=job_id, event_type="job.started", message="job started"
        )

        try:
            config = self._config_for(project_id)
        except Exception as exc:
            # A transient PB outage while loading config is retryable — only
            # deterministic (non-network) errors are permanent.
            transient = isinstance(exc, (ConnectionError, TimeoutError, OSError))
            self._finalize_failure(
                job_id, "ConfigError", f"failed to load project config: {exc}", retryable=transient
            )
            self.leases.release(job_id)
            return

        stack = ProviderStack(
            registry=self.registry,
            config=config,
            observer=self._observer_for(project_id, job_id),
            limits={
                "llm": self._llm_sem,
                "embedding": self._embedding_sem,
                "publish": self._publish_sem,
            },
        )
        ctx = JobContext(
            pb=self.pb,
            job=self.jobs.get(job_id) or job,
            config=config,
            providers=stack,
            registry=self.registry,
            events=self.events,
            set_progress=self.jobs.set_progress,
            request_cancel=self._is_cancel_requested,
        )

        heartbeat_task = asyncio.create_task(self._heartbeat_loop(job_id))
        try:
            result = await handler(ctx)
            self.jobs.complete(job_id, result)
            self._completed += 1
            logger.info("job completed", job_id=job_id, type=ctx.job.get("type"))
        except JobCancelled:
            self.jobs.cancel(job_id)
            self._cancelled += 1
            logger.info("job cancelled", job_id=job_id)
        except ProviderError as exc:
            self._handle_provider_failure(ctx, exc)
        except Exception as exc:
            self._finalize_failure(
                job_id,
                type(exc).__name__,
                str(exc),
                retryable=True,
                details={"traceback": traceback.format_exc()[-2000:]},
            )
        finally:
            heartbeat_task.cancel()
            self.leases.release(job_id)
            await stack.aclose()

    # ---------------------------------------------------------------------------
    # Failure handling & retries (exponential backoff + jitter + Retry-After)
    # ---------------------------------------------------------------------------
    def _handle_provider_failure(self, ctx: JobContext, exc: ProviderError) -> None:
        error = exc.to_dict()
        ctx.provider_error(f"{exc.__class__.__name__}: {exc}", error)
        self._finalize_failure(
            ctx.job_id,
            error["type"],
            error["message"],
            retryable=error["retryable"],
            details=error.get("details"),
        )

    def _finalize_failure(
        self,
        job_id: str,
        error_code: str,
        error_message: str,
        *,
        retryable: bool,
        details: dict[str, Any] | None = None,
    ) -> None:
        job = self.jobs.get(job_id)
        if not job:
            return
        attempts = int(job.get("attempts") or 0) + 1
        max_attempts = int(job.get("maxAttempts") or 1)
        # errorMessage is a 5000-char text field on the jobs collection; a raw
        # exception string (a PocketBase 400 embeds the whole request URL and
        # filter) can blow past it and make writing the failure itself fail.
        if len(error_message) > ERROR_MESSAGE_MAX:
            error_message = error_message[: ERROR_MESSAGE_MAX - 1].rstrip() + "…"
        error_details: dict[str, Any] = {"retryable": retryable, "attempts": attempts}
        if details:
            error_details["details"] = details

        if retryable and attempts < max_attempts:
            policy = (
                ProjectSettingsRepo(self.pb).get_for_project(job["project"]).get("retryPolicy", {})
            )
            base = max(1, int(policy.get("backoff_base") or 30))
            cap = max(base, int(policy.get("backoff_max") or 3600))
            # exponential backoff with jitter (±50%)
            delay = min(base * (2 ** (attempts - 1)), cap)
            delay = max(1, int(delay * random.uniform(0.5, 1.5)))
            # respect the provider's Retry-After when available
            retry_after = int((details or {}).get("retry_after_seconds") or 0)
            if retry_after > 0:
                delay = max(delay, retry_after)
            next_retry = now_utc() + dt.timedelta(seconds=delay)
            self.jobs.update(
                job_id,
                {
                    "errorCode": error_code,
                    "errorMessage": error_message,
                    "errorDetails": error_details,
                },
            )
            self.jobs.schedule_retry(job_id, next_retry, attempts, max_attempts, delay)
            logger.warning(
                "job scheduled for retry",
                job_id=job_id,
                attempts=attempts,
                max_attempts=max_attempts,
                delay=delay,
                jittered=True,
            )
        else:
            self.jobs.update(job_id, {"attempts": attempts})
            self.jobs.fail(job_id, error_code, error_message, error_details)
            self._failed += 1
            logger.error("job failed permanently", job_id=job_id, error_code=error_code)

    # ---------------------------------------------------------------------------
    # Heartbeat & cancellation
    # ---------------------------------------------------------------------------
    async def _heartbeat_loop(self, job_id: str) -> None:
        try:
            while True:
                await asyncio.sleep(self.heartbeat_interval)
                expires = now_utc() + dt.timedelta(seconds=self.lease_seconds)
                try:
                    self.jobs.heartbeat(job_id, expires)
                    self.leases.extend(job_id, expires)
                except Exception:
                    logger.warning("heartbeat failed", job_id=job_id)
        except asyncio.CancelledError:
            pass

    def _is_cancel_requested(self, job_id: str) -> bool:
        job = self.jobs.get(job_id)
        return bool(job and job.get("cancelRequested"))

    def _observer_for(self, project_id: str, job_id: str) -> Any:
        """Call observer chain: metrics accumulator + job_events (opt-in) + debug logs.

        Records never contain prompt content — only size/tokens/latency/errors.
        """
        from app.config import settings
        from app.providers.metrics import (
            ChainedObserver,
            EventObserver,
            LoggingObserver,
            ProjectScopedObserver,
        )

        observers: list[Any] = [LoggingObserver(), self._metrics_observer]
        if settings.provider_events_enabled:
            observers.append(EventObserver(self.events, project_id, job_id))
        return ProjectScopedObserver(project_id, ChainedObserver(observers))

    def flush_metrics(self) -> int:
        """Flush accumulated provider metrics to PocketBase. Returns rows written."""
        from app.services.metrics import flush_accumulator

        return flush_accumulator(self.pb, self._metrics)


def _lease_expired(lease: dict[str, Any]) -> bool:
    expires = lease.get("expiresAt")
    if not expires:
        return True
    try:
        if isinstance(expires, dt.datetime):
            return expires < now_utc()
        value = str(expires).replace("Z", "+00:00")
        return dt.datetime.fromisoformat(value).astimezone(dt.UTC) < now_utc()
    except ValueError:
        return True
