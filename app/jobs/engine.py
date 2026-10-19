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
        # Claim bookkeeping, so heartbeat/release/execution need no extra reads:
        # `_lease_ids` remembers the lease row we created, `_running` the job
        # record mark_running() returned. Both are bounded by jobs in flight and
        # every consumer falls back to a real read if the entry is missing.
        self._lease_ids: dict[str, str] = {}
        self._running: dict[str, dict[str, Any]] = {}
        # Cancellation is polled from inside per-item loops (per document, per
        # keyword…), so a raw `jobs.get()` there was one PocketBase round trip
        # PER ITEM — hundreds per run. A short TTL keeps cancellation responsive
        # (it is already cooperative) while collapsing those to ~1 per window.
        self._cancel_cache: dict[str, tuple[bool, float]] = {}
        self._cancel_ttl = 3.0
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
    async def poll_and_run(self) -> int:
        """One poll cycle: claim candidates, then execute them under the semaphore.

        Returns the number of jobs claimed this cycle.
        """
        candidates = self.jobs.pending_candidates(limit=50)
        claimed = 0
        for job in candidates:
            if job.get("status") not in ("pending", "retrying", "running"):
                continue
            if not self._claim(job):
                continue
            claimed += 1
            asyncio.create_task(self._run_guarded(job))
        return claimed

    def _claim(self, job: dict[str, Any]) -> bool:
        """Atomic claim via the unique job_leases constraint. Returns True on win.

        The winner transitions the job straight to `running` with lock fields
        and a fresh lease; losers (including crash-recovery races) back off.
        """
        job_id = job["id"]
        expires = now_utc() + dt.timedelta(seconds=self.lease_seconds)
        lease_id = self.leases.acquire(job_id, self.worker_id, expires)
        if not lease_id:
            # Lost the race — but if the existing lease is already expired we can
            # clean it up and retry once (abandoned-job recovery).
            current = self.leases.current(job_id)
            if current and _lease_expired(current):
                self.leases.delete(current["id"])
                lease_id = self.leases.acquire(job_id, self.worker_id, expires)
                if not lease_id:
                    return False
            else:
                return False
        # Keep the lease row id: heartbeat + release then hit it directly instead
        # of re-reading the row (one request each instead of two).
        self._lease_ids[job_id] = lease_id
        # Only claim jobs that are still claimable (state may have changed).
        fresh = self.jobs.get(job_id)
        if not fresh or fresh.get("status") not in ("pending", "retrying", "running"):
            self.leases.release(job_id, lease_id)
            self._lease_ids.pop(job_id, None)
            return False
        validate_transition(fresh.get("status", "pending"), "running")
        running = self.jobs.mark_running(job_id, self.worker_id, expires)
        self.events.add(
            project=fresh.get("project", ""),
            job=job_id,
            event_type="job.claimed",
            message=f"job claimed by worker {self.worker_id}",
            metadata={"worker_id": self.worker_id, "lease_expires_at": expires.isoformat()},
        )
        # Hand the freshly-claimed record to _run so it need not re-read it.
        self._running[job_id] = running or fresh
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
            self._release_lease(job_id)
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
            self._release_lease(job_id)
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
            # The record _claim just wrote, so no re-read is needed (falls back).
            job=self._running.pop(job_id, None) or self.jobs.get(job_id) or job,
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
        except asyncio.CancelledError:
            # Worker stopping (deploy/restart) mid-job: back to the queue at once
            # instead of sitting `running` until the lease expires. The handler
            # is no longer running, so no later write can fight the requeue.
            self.jobs.requeue(job_id)
            raise
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
                settings=config.settings,
            )
        finally:
            heartbeat_task.cancel()
            self._release_lease(job_id)
            self._cancel_cache.pop(job_id, None)
            await stack.aclose()

    def _release_lease(self, job_id: str) -> None:
        """Drop the lease row and the per-job claim bookkeeping."""
        self.leases.release(job_id, self._lease_ids.pop(job_id, None))
        self._running.pop(job_id, None)

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
            settings=ctx.config.settings,
        )

    def _finalize_failure(
        self,
        job_id: str,
        error_code: str,
        error_message: str,
        *,
        retryable: bool,
        details: dict[str, Any] | None = None,
        settings: dict[str, Any] | None = None,
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
            # The caller already holds the project settings (loaded once per job);
            # only fall back to a query when it failed before config was loaded.
            policy = (
                settings
                if settings is not None
                else ProjectSettingsRepo(self.pb).get_for_project(job["project"])
            ).get("retryPolicy", {})
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
            # One write carrying both the error and the retry state.
            self.jobs.schedule_retry(
                job_id,
                next_retry,
                attempts,
                max_attempts,
                delay,
                error_code=error_code,
                error_message=error_message,
                error_details=error_details,
            )
            logger.warning(
                "job scheduled for retry",
                job_id=job_id,
                attempts=attempts,
                max_attempts=max_attempts,
                delay=delay,
                jittered=True,
            )
        else:
            self.jobs.fail(job_id, error_code, error_message, error_details, attempts=attempts)
            self._failed += 1
            self._settle_artifact(job)
            logger.error("job failed permanently", job_id=job_id, error_code=error_code)

    def _settle_artifact(self, job: dict[str, Any]) -> None:
        """Take the failed job's artifact out of its in-progress state.

        A job that dies permanently must never leave an article at "generating"
        or a topic at "writing": that is the stuck state with no way forward —
        the dashboard shows work in flight that no job will ever finish.
        `failed` is terminal, visible, and resumable from the UI.

        Diagnostics only: a failure here must never mask the job failure.
        """
        try:
            from app.repositories.articles import ArticleRepo
            from app.repositories.topics import TopicRepo

            articles = ArticleRepo(self.pb)
            topics = TopicRepo(self.pb)
            entity_type = str(job.get("entityType") or "")
            entity_id = str(job.get("entityId") or "")
            article = articles.get(entity_id) if entity_type == "article" and entity_id else None
            topic = topics.get(entity_id) if entity_type == "topic" and entity_id else None
            if article is None and topic is not None:
                linked = str(topic.get("articleId") or "")
                article = articles.get(linked) if linked else None
            if topic is None and article is not None:
                linked = str(article.get("topicId") or "")
                topic = topics.get(linked) if linked else None
            if article is not None and article.get("status") in ("generating", "publishing"):
                articles.set_status(article["id"], "failed")
            if topic is not None and topic.get("status") in ("queued", "writing", "outline_ready"):
                topics.set_status(topic["id"], "failed")
        except Exception:
            logger.exception("job.artifact_settle.failed", job_id=job.get("id"))

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
                    self.leases.extend(job_id, expires, self._lease_ids.get(job_id))
                except Exception:
                    logger.warning("heartbeat failed", job_id=job_id)
        except asyncio.CancelledError:
            pass

    def _is_cancel_requested(self, job_id: str) -> bool:
        """Cancel flag, memoized for a few seconds (see _cancel_cache)."""
        cached = self._cancel_cache.get(job_id)
        now = time.monotonic()
        if cached is not None and now - cached[1] < self._cancel_ttl:
            return cached[0]
        job = self.jobs.get(job_id)
        value = bool(job and job.get("cancelRequested"))
        self._cancel_cache[job_id] = (value, now)
        return value

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
