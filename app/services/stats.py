"""Dashboard/aggregation stats (read-only, paginated queries only)."""

from __future__ import annotations

import threading
import time
from functools import partial
from typing import Any

from app.repositories.articles import ArticleRepo
from app.repositories.indexing import IndexRunRepo
from app.repositories.integrations import IntegrationRepo
from app.repositories.jobs import JobEventRepo, JobRepo
from app.repositories.topics import TopicRepo
from app.repositories.worker_heartbeats import WorkerHeartbeatRepo
from app.utils import fanout

# Dashboard aggregates are derived, read-only data: a short TTL cache collapses
# repeated poll queries (multiple users × 8s polling). Mutable article state is
# never cached — this only caches aggregate counts for display.
#
# TTL must exceed the poll interval (`_stats.html` polls every 8s): at 5s every
# single poll missed the cache and re-ran the full 17-query aggregate, which is
# exactly the load this cache exists to absorb. 15s keeps counts at most ~15s
# stale (they were already up to 5s stale) while letting consecutive polls share
# one computation.
_STATS_CACHE: dict[tuple, tuple[float, dict[str, Any]]] = {}
_STATS_TTL = 15.0

# Queue depth (per-status job counts) is shown on the dashboard AND on /workers,
# which re-fetches the whole page every 5s. Both want the SAME exact numbers, so
# they share one cached computation instead of re-issuing 4-5 count queries each.
# Counts stay exact (one real count per status); only their freshness is bounded,
# exactly like the aggregate cache above.
_JOB_COUNTS_STATUSES = ("pending", "retrying", "running", "failed", "completed")
_JOB_COUNTS_CACHE: dict[tuple, tuple[float, dict[str, int]]] = {}
_JOB_COUNTS_TTL = 15.0

_STATS_LOCK = threading.Lock()
_JOB_COUNTS_LOCK = threading.Lock()


def clear_stats_cache() -> None:
    """Invalidate the aggregate caches (tests, schema changes)."""
    with _STATS_LOCK:
        _STATS_CACHE.clear()
    with _JOB_COUNTS_LOCK:
        _JOB_COUNTS_CACHE.clear()


def job_status_counts(pb: Any, project_scope: list[str] | None = None) -> dict[str, int]:
    """Exact count of jobs per status, TTL-cached and shared across pages.

    PocketBase has no GROUP BY (and batch requests are disabled on the server),
    so each status needs its own query — but they are independent, so they fan
    out in one wave and the result is reused by every page showing queue depth.
    """
    key: tuple = ("*",) if project_scope is None else tuple(sorted(project_scope))
    now = time.monotonic()
    with _JOB_COUNTS_LOCK:
        cached = _JOB_COUNTS_CACHE.get(key)
        if cached and now - cached[0] < _JOB_COUNTS_TTL:
            return cached[1]

    jobs = JobRepo(pb)
    scope_filter = _scope_filter(project_scope, "project")
    result = fanout(
        {
            status: partial(jobs.count, filter=f'status="{status}"{_and(scope_filter)}')
            for status in _JOB_COUNTS_STATUSES
        }
    )
    with _JOB_COUNTS_LOCK:
        _JOB_COUNTS_CACHE[key] = (now, result)
    return result


def global_stats(pb: Any, project_scope: list[str] | None = None) -> dict[str, Any]:
    key: tuple = ("*",) if project_scope is None else tuple(sorted(project_scope))
    now = time.monotonic()
    with _STATS_LOCK:
        cached = _STATS_CACHE.get(key)
        if cached and now - cached[0] < _STATS_TTL:
            return cached[1]
    result = _compute_stats(pb, project_scope)
    with _STATS_LOCK:
        _STATS_CACHE[key] = (now, result)
    return result


def _compute_stats(pb: Any, project_scope: list[str] | None = None) -> dict[str, Any]:
    # Queue depth is shared+cached with /workers (see job_status_counts) — it is
    # NOT part of the fanout below, so this wave only carries the dashboard-only
    # aggregates. `projects` used to be counted here too but no template ever
    # rendered it, so it was a pure wasted round trip.
    counts = job_status_counts(pb, project_scope)

    computed = fanout(
        {
            "average_duration_s": lambda: _average_duration(pb, project_scope),
            "provider_latency_ms": lambda: _provider_latency(pb, project_scope),
            "topics_planned": lambda: _count_topics(pb, project_scope, "planned"),
            "articles_writing": lambda: _count_articles(
                pb, project_scope, ("outline_ready", "generating")
            ),
            "articles_review": lambda: _count_articles(pb, project_scope, ("review", "approved")),
            "articles_published": lambda: _count_articles(pb, project_scope, ("published",)),
            "indexing": lambda: _indexing_health(pb, project_scope),
            "provider_health": lambda: _provider_health(pb, project_scope),
            "workers_active": lambda: _active_workers(pb),
        }
    )

    indexing_ok, indexing_failed = computed.pop("indexing")
    provider_healthy, provider_unhealthy, provider_unknown = computed.pop("provider_health")
    failed = counts["failed"]
    completed = counts["completed"]
    total = failed + completed
    return {
        "jobs_pending": counts["pending"],
        "jobs_retrying": counts["retrying"],
        "jobs_running": counts["running"],
        "jobs_failed": failed,
        "jobs_completed": completed,
        **computed,
        "failure_rate": round(failed / total, 3) if total else 0.0,
        "indexing_ok": indexing_ok,
        "indexing_failed": indexing_failed,
        "provider_healthy": provider_healthy,
        "provider_unhealthy": provider_unhealthy,
        "provider_unknown": provider_unknown,
    }


def dashboard_extra(
    pb: Any, project_scope: list[str] | None = None, limit: int = 8
) -> dict[str, Any]:
    """Recent jobs + events for the dashboard, fetched concurrently (1 round trip)."""
    return fanout(
        {
            "recent_jobs": lambda: recent_jobs(pb, project_scope, limit),
            "recent_events": lambda: recent_events(pb, project_scope, limit),
        }
    )


def project_overviews(pb: Any, projects: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Small, user-facing summary for each visible website."""

    def one(project: dict[str, Any]) -> dict[str, Any]:
        pid = project.get("id") or ""
        topics = TopicRepo(pb)
        articles = ArticleRepo(pb)
        jobs = JobRepo(pb)
        return {
            **project,
            "planned": topics.count_by_status(pid, "planned"),
            "writing": articles.count(
                filter=f'project="{pid}" && (status="generating" || status="outline_ready")'
            ),
            "review": articles.count(
                filter=f'project="{pid}" && (status="review" || status="approved")'
            ),
            "published": articles.count(filter=f'project="{pid}" && status="published"'),
            "attention": jobs.count(filter=f'project="{pid}" && status="failed"'),
        }

    return list(fanout({str(p.get("id")): partial(one, p) for p in projects}).values())


def _active_workers(pb: Any) -> int:
    """Workers whose heartbeat is fresher than ~45s (missed ≤2 beats)."""
    import datetime as dt

    try:
        # Only the timestamp is needed — never pull whole heartbeat rows.
        rows = WorkerHeartbeatRepo(pb).list_records(
            sort="-lastHeartbeatAt", page=1, per_page=200, fields="lastHeartbeatAt"
        )
    except Exception:
        return 0
    cutoff = dt.datetime.now(dt.UTC) - dt.timedelta(seconds=45)
    active = 0
    for row in rows:
        last = row.get("lastHeartbeatAt")
        if not last:
            continue
        if isinstance(last, dt.datetime):
            parsed = last if last.tzinfo else last.replace(tzinfo=dt.UTC)
        else:
            try:
                parsed = dt.datetime.fromisoformat(str(last).replace("Z", "+00:00"))
            except ValueError:
                continue
        if parsed >= cutoff:
            active += 1
    return active


def recent_jobs(
    pb: Any, project_scope: list[str] | None = None, limit: int = 10
) -> list[dict[str, Any]]:
    f = _scope_filter(project_scope, "project")
    # Only the columns the dashboard renders — never the payload/result/error JSON.
    return JobRepo(pb).list_records(
        filter=f, sort="-created", page=1, per_page=limit, fields="type,status,progress,created"
    )


def recent_events(
    pb: Any, project_scope: list[str] | None = None, limit: int = 10
) -> list[dict[str, Any]]:
    f = _scope_filter(project_scope, "project")
    # `metadata` can be a large JSON blob and the dashboard never shows it.
    return JobEventRepo(pb).list_records(
        filter=f, sort="-created", page=1, per_page=limit, fields="eventType,message,created"
    )


def _scope_filter(scope: list[str] | None, field: str) -> str:
    """None = unrestricted; [] = match nothing (user has no projects).

    Always returns a valid *standalone* filter (no leading operator) so it can
    be passed straight to PocketBase. Compose with :func:`_and` when appending
    it to a base condition (e.g. ``status="pending"``).
    """
    if scope is None:
        return ""
    if not scope:
        return f'{field}="__no_access__"'
    quoted = " || ".join(f'{field}="{s}"' for s in scope)
    return f"({quoted})"


def _and(filter_part: str) -> str:
    """``" && <filter>"`` for composing with a base condition (``""`` if empty)."""
    return f" && {filter_part}" if filter_part else ""


def _average_duration(pb: Any, scope: list[str] | None) -> float:
    """Average wall-clock duration of recent completed jobs (bounded sample)."""

    f = _scope_filter(scope, "project")
    recent = JobRepo(pb).list_records(
        filter=f'status="completed"{_and(f)}',
        sort="-created",
        page=1,
        per_page=100,
        fields="startedAt,finishedAt",
    )
    durations = []
    for job in recent:
        started = job.get("startedAt")
        finished = job.get("finishedAt")
        if not started or not finished:
            continue
        s = _as_dt(started)
        e = _as_dt(finished)
        if s and e and e > s:
            durations.append((e - s).total_seconds())
    return round(sum(durations) / len(durations), 1) if durations else 0.0


def _indexing_health(pb: Any, scope: list[str] | None) -> tuple[int, int]:
    """Per project: latest index run → ok (succeeded) or failed (failed).

    One scan instead of one query per project (was an N+1). Runs are sorted
    newest-first so the first row seen per project is its latest. The scope
    filter already restricts runs to the visible projects, so no separate
    project lookup is needed. ponytail: bounded to the 1000 newest runs; if a
    project's latest run is older than that global window it is simply not
    counted — add per-project queries back if run volume ever makes that
    possible.
    """
    if scope is not None and not scope:
        return 0, 0
    runs = IndexRunRepo(pb).list_records(
        filter=_scope_filter(scope, "project"),
        sort="-created",
        page=1,
        per_page=1000,
        fields="project,status",
    )
    latest: dict[str, str] = {}
    for run in runs:
        pid = run.get("project")
        if isinstance(pid, str) and pid not in latest:
            latest[pid] = run.get("status") or ""
    ok = sum(1 for s in latest.values() if s == "succeeded")
    failed = sum(1 for s in latest.values() if s == "failed")
    return ok, failed


def _provider_latency(pb: Any, scope: list[str] | None) -> float:
    """Average provider call latency from recent provider_call events (bounded)."""
    f = _scope_filter(scope, "project")
    events = JobEventRepo(pb).list_records(
        filter=f'eventType="provider_call"{_and(f)}',
        sort="-created",
        page=1,
        per_page=100,
        fields="metadata",
    )
    latencies = [
        int((e.get("metadata") or {}).get("latency_ms") or 0)
        for e in events
        if (e.get("metadata") or {}).get("latency_ms")
    ]
    return round(sum(latencies) / len(latencies), 1) if latencies else 0.0


def _as_dt(value: Any) -> Any:
    import datetime as dt

    if isinstance(value, dt.datetime):
        return value
    try:
        return dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _count_topics(pb: Any, scope: list[str] | None, status: str) -> int:
    f = _scope_filter(scope, "project")
    return TopicRepo(pb).count(filter=f'status="{status}"{_and(f)}')


def _count_articles(pb: Any, scope: list[str] | None, statuses: tuple[str, ...]) -> int:
    f = _scope_filter(scope, "project")
    parts = " || ".join(f'status="{s}"' for s in statuses)
    return ArticleRepo(pb).count(filter=f"({parts}){_and(f)}")


def _provider_health(pb: Any, scope: list[str] | None) -> tuple[int, int, int]:
    integrations = IntegrationRepo(pb).list_records(
        filter=_scope_filter(scope, "project"),
        sort="project",
        per_page=500,
        fields="healthStatus",
    )
    healthy = unhealthy = unknown = 0
    for integration in integrations:
        status = integration.get("healthStatus") or "unknown"
        if status == "healthy":
            healthy += 1
        elif status == "unhealthy":
            unhealthy += 1
        else:
            unknown += 1
    return healthy, unhealthy, unknown
