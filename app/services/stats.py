"""Dashboard/aggregation stats (read-only, paginated queries only)."""

from __future__ import annotations

import threading
from typing import Any

from app.repositories.articles import ArticleRepo
from app.repositories.indexing import IndexRunRepo
from app.repositories.integrations import IntegrationRepo
from app.repositories.jobs import JobEventRepo, JobRepo
from app.repositories.projects import ProjectRepo
from app.repositories.topics import TopicRepo
from app.repositories.worker_heartbeats import WorkerHeartbeatRepo

# Dashboard aggregates are derived, read-only data: a short TTL cache collapses
# repeated poll queries (multiple users × 8s polling). Mutable article state is
# never cached — this only caches aggregate counts for display.
_STATS_CACHE: dict[tuple, tuple[float, dict[str, Any]]] = {}
_STATS_TTL = 5.0

_STATS_LOCK = threading.Lock()


def clear_stats_cache() -> None:
    """Invalidate the aggregate cache (tests, schema changes)."""
    with _STATS_LOCK:
        _STATS_CACHE.clear()


def global_stats(pb: Any, project_scope: list[str] | None = None) -> dict[str, Any]:
    import time as _time

    key: tuple = ("*",) if project_scope is None else tuple(sorted(project_scope))
    now = _time.monotonic()
    with _STATS_LOCK:
        cached = _STATS_CACHE.get(key)
        if cached and now - cached[0] < _STATS_TTL:
            return cached[1]
    result = _compute_stats(pb, project_scope)
    with _STATS_LOCK:
        _STATS_CACHE[key] = (now, result)
    return result


def _compute_stats(pb: Any, project_scope: list[str] | None = None) -> dict[str, Any]:
    jobs = JobRepo(pb)
    projects = ProjectRepo(pb)
    scope_filter = _scope_filter(project_scope, "project")
    proj_filter = _scope_filter(project_scope, "id")

    result = {
        "projects": projects.count(filter=proj_filter) if proj_filter else projects.count(),
        "jobs_pending": jobs.count(filter=f'status="pending"{_and(scope_filter)}'),
        "jobs_retrying": jobs.count(filter=f'status="retrying"{_and(scope_filter)}'),
        "jobs_running": jobs.count(filter=f'status="running"{_and(scope_filter)}'),
        "jobs_failed": jobs.count(filter=f'status="failed"{_and(scope_filter)}'),
        "jobs_completed": jobs.count(filter=f'status="completed"{_and(scope_filter)}'),
        "average_duration_s": _average_duration(pb, project_scope),
        "failure_rate": _failure_rate(pb, project_scope),
        "provider_latency_ms": _provider_latency(pb, project_scope),
        "topics_planned": _count_topics(pb, project_scope, "planned"),
        "articles_writing": _count_articles(pb, project_scope, ("outline_ready", "generating")),
        "articles_review": _count_articles(pb, project_scope, ("review", "approved")),
        "articles_published": _count_articles(pb, project_scope, ("published",)),
        "indexing_ok": 0,
        "indexing_failed": 0,
        "provider_healthy": 0,
        "provider_unhealthy": 0,
        "provider_unknown": 0,
        "workers_active": 0,
    }
    indexing_ok, indexing_failed = _indexing_health(pb, project_scope)
    provider_healthy, provider_unhealthy, provider_unknown = _provider_health(pb, project_scope)
    result["indexing_ok"], result["indexing_failed"] = indexing_ok, indexing_failed
    result["provider_healthy"], result["provider_unhealthy"], result["provider_unknown"] = (
        provider_healthy,
        provider_unhealthy,
        provider_unknown,
    )
    result["workers_active"] = _active_workers(pb)
    return result


def _active_workers(pb: Any) -> int:
    """Workers whose heartbeat is fresher than ~45s (missed ≤2 beats)."""
    import datetime as dt

    try:
        rows = WorkerHeartbeatRepo(pb).list_recent(limit=200)
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
    return JobRepo(pb).list_records(filter=f, sort="-created", page=1, per_page=limit)


def recent_events(
    pb: Any, project_scope: list[str] | None = None, limit: int = 10
) -> list[dict[str, Any]]:
    f = _scope_filter(project_scope, "project")
    return JobEventRepo(pb).list_records(filter=f, sort="-created", page=1, per_page=limit)


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
        filter=f'status="completed"{_and(f)}', sort="-created", page=1, per_page=100
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


def _failure_rate(pb: Any, scope: list[str] | None) -> float:
    jobs = JobRepo(pb)
    f = _scope_filter(scope, "project")
    failed = jobs.count(filter=f'status="failed"{_and(f)}')
    completed = jobs.count(filter=f'status="completed"{_and(f)}')
    total = failed + completed
    return round(failed / total, 3) if total else 0.0


def _provider_latency(pb: Any, scope: list[str] | None) -> float:
    """Average provider call latency from recent provider_call events (bounded)."""
    f = _scope_filter(scope, "project")
    events = JobEventRepo(pb).list_records(
        filter=f'eventType="provider_call"{_and(f)}', sort="-created", page=1, per_page=100
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


def _indexing_health(pb: Any, scope: list[str] | None) -> tuple[int, int]:
    """Per project: latest index run → ok (succeeded) or failed (failed)."""
    projects = ProjectRepo(pb).list_records(
        filter=_scope_filter(scope, "id"), sort="name", per_page=200
    )
    runs = IndexRunRepo(pb)
    ok = failed = 0
    for project in projects:
        latest = runs.first(filter=f'project="{project["id"]}"', sort="-created")
        if not latest:
            continue
        if latest.get("status") == "succeeded":
            ok += 1
        elif latest.get("status") == "failed":
            failed += 1
    return ok, failed


def _provider_health(pb: Any, scope: list[str] | None) -> tuple[int, int, int]:
    integrations = IntegrationRepo(pb).list_records(
        filter=_scope_filter(scope, "project"), sort="project", per_page=500
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
