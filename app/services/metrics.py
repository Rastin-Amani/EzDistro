"""Lightweight operational metrics.

No monitoring stack: provider-call metrics are accumulated in memory per worker
and flushed as tiny aggregate rows (`provider_metrics`: one row per
project/provider/model/operation/day) — request count, success/failure counts,
retries, token usage, latency sum + a small latency histogram for P95.
Project metrics are computed from existing collections with bounded queries.
"""

from __future__ import annotations

import datetime as dt
import threading
from typing import Any

from app.providers.metrics import ProviderCallRecord

# latency histogram buckets (seconds upper bounds) — P95 derived from these
LATENCY_BUCKETS: list[tuple[float, str]] = [
    (0.25, "0.25"),
    (0.5, "0.5"),
    (1.0, "1"),
    (2.0, "2"),
    (5.0, "5"),
    (10.0, "10"),
    (30.0, "30"),
]
BUCKET_OVER = "over"


def _day_key() -> str:
    return dt.datetime.now(dt.UTC).strftime("%Y-%m-%d")


def _bucket_for(latency_ms: float) -> str:
    seconds = latency_ms / 1000.0
    for upper, label in LATENCY_BUCKETS:
        if seconds <= upper:
            return label
    return BUCKET_OVER


class MetricsAccumulator:
    """Thread-safe in-memory aggregation of provider call records."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._rows: dict[tuple[str, str, str, str, str], dict[str, Any]] = {}

    def record(self, record: ProviderCallRecord) -> None:
        key = (record.provider, record.model, record.operation, record.project_id or "", _day_key())
        with self._lock:
            row = self._rows.setdefault(
                key,
                {
                    "provider": record.provider,
                    "model": record.model,
                    "operation": record.operation,
                    "project": record.project_id or "",
                    "day": _day_key(),
                    "requestCount": 0,
                    "successCount": 0,
                    "failureCount": 0,
                    "retryCount": 0,
                    "promptTokens": 0,
                    "completionTokens": 0,
                    "latencySum": 0,
                    "latencyBuckets": {},
                },
            )
            row["requestCount"] += 1
            if record.success:
                row["successCount"] += 1
            else:
                row["failureCount"] += 1
            row["retryCount"] += record.retries
            if record.prompt_tokens:
                row["promptTokens"] += record.prompt_tokens
            if record.completion_tokens:
                row["completionTokens"] += record.completion_tokens
            row["latencySum"] += record.latency_ms
            bucket = _bucket_for(record.latency_ms)
            row["latencyBuckets"][bucket] = row["latencyBuckets"].get(bucket, 0) + 1

    def drain(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = list(self._rows.values())
            self._rows.clear()
        return rows


class ProviderMetricsObserver:
    """Feeds the accumulator (and optionally per-job events)."""

    def __init__(self, accumulator: MetricsAccumulator) -> None:
        self._accumulator = accumulator

    def on_call(self, record: ProviderCallRecord) -> None:
        self._accumulator.record(record)


def flush_accumulator(pb: Any, accumulator: MetricsAccumulator) -> int:
    """Write drained rows into provider_metrics (read-modify-write upsert)."""
    rows = accumulator.drain()
    if not rows:
        return 0
    written = 0
    for row in rows:
        project = row.pop("project", "")
        if not project:
            continue
        key_filter = (
            f'project="{project}" && provider="{row["provider"]}" && model="{row["model"] or ""}" '
            f'&& operation="{row["operation"]}" && day="{row["day"]}"'
        )
        existing = _first(pb, "provider_metrics", key_filter)
        if existing:
            for field in (
                "requestCount",
                "successCount",
                "failureCount",
                "retryCount",
                "promptTokens",
                "completionTokens",
                "latencySum",
            ):
                row[field] = int(existing.get(field) or 0) + int(row.get(field) or 0)
            buckets = dict(existing.get("latencyBuckets") or {})
            for bucket, count in (row.get("latencyBuckets") or {}).items():
                buckets[bucket] = int(buckets.get(bucket, 0)) + int(count)
            row["latencyBuckets"] = buckets
            _update(pb, "provider_metrics", existing["id"], row)
        else:
            _create(pb, "provider_metrics", {"project": project, **row})
        written += 1
    return written


def _first(pb: Any, collection: str, filter: str) -> dict[str, Any] | None:
    try:
        return _to_dict(pb.collection(collection).get_first_list_item(filter, {"perPage": 1}))
    except Exception:
        return None


def _to_dict(record: Any) -> dict[str, Any]:
    if isinstance(record, dict):
        return record
    return {k: v for k, v in vars(record).items() if not k.startswith("_")}


def _update(pb: Any, collection: str, record_id: str, data: dict[str, Any]) -> None:
    pb.collection(collection).update(record_id, data)


def _create(pb: Any, collection: str, data: dict[str, Any]) -> None:
    pb.collection(collection).create(data)


# ---------------------------------------------------------------------------
# Queries
# ---------------------------------------------------------------------------
def query_provider_metrics(pb: Any, project_id: str = "", days: int = 7) -> list[dict[str, Any]]:
    """Aggregated provider metrics (last `days` days), per provider/model/operation.

    Each row: requestCount, successRate, failureRate, avgLatencyMs, p95LatencyMs,
    retryCount, promptTokens, completionTokens.
    """
    since = (dt.datetime.now(dt.UTC) - dt.timedelta(days=days)).strftime("%Y-%m-%d")
    f = f'day >= "{since}"'
    if project_id:
        f = f'project="{project_id}" && {f}'
    rows = []
    try:
        records = pb.collection("provider_metrics").get_full_list(
            query_params={"filter": f, "sort": "provider,model,operation"}
        )
    except Exception:
        return []
    by_key: dict[tuple[str, str, str], dict[str, Any]] = {}
    for record in records:
        data = _to_dict(record)
        key = (data.get("provider") or "", data.get("model") or "", data.get("operation") or "")
        agg = by_key.setdefault(
            key,
            {
                "provider": key[0],
                "model": key[1],
                "operation": key[2],
                "requestCount": 0,
                "successCount": 0,
                "failureCount": 0,
                "retryCount": 0,
                "promptTokens": 0,
                "completionTokens": 0,
                "latencySum": 0,
                "latencyBuckets": {},
            },
        )
        for field in (
            "requestCount",
            "successCount",
            "failureCount",
            "retryCount",
            "promptTokens",
            "completionTokens",
            "latencySum",
        ):
            agg[field] += int(data.get(field) or 0)
        for bucket, count in (data.get("latencyBuckets") or {}).items():
            agg["latencyBuckets"][bucket] = int(agg["latencyBuckets"].get(bucket, 0)) + int(count)

    for agg in by_key.values():
        total = agg["requestCount"]
        agg["successRate"] = round(agg["successCount"] / total, 3) if total else 0.0
        agg["failureRate"] = round(agg["failureCount"] / total, 3) if total else 0.0
        agg["avgLatencyMs"] = round(agg["latencySum"] / total, 1) if total else 0.0
        agg["p95LatencyMs"] = _p95_from_buckets(agg["latencyBuckets"], total) if total else 0.0
        rows.append(agg)
    return rows


def _p95_from_buckets(buckets: dict[str, int], total: int) -> int:
    ordered = [(label, count) for label, count in buckets.items() if label != BUCKET_OVER]
    try:
        ordered.sort(key=lambda pair: float(pair[0]))
    except ValueError:
        ordered = []
    over = int(buckets.get(BUCKET_OVER, 0))
    target = total * 0.95
    seen = 0
    for label, count in ordered:
        seen += count
        if seen >= target:
            return int(float(label) * 1000)
    if over:
        return 60_000  # > 30s bucket: report 60s
    return 0


# ---------------------------------------------------------------------------
# Project metrics (bounded queries over existing collections)
# ---------------------------------------------------------------------------
def project_metrics(pb: Any, project_id: str) -> dict[str, Any]:
    from app.repositories.articles import ArticleRepo, SectionRepo
    from app.repositories.indexing import DocumentRepo
    from app.repositories.jobs import JobRepo

    docs = DocumentRepo(pb)
    articles = ArticleRepo(pb)
    jobs = JobRepo(pb)
    indexed = docs.count(filter=f'project="{project_id}" && indexStatus="indexed"')
    stale = docs.count(filter=f'project="{project_id}" && indexStatus="deleted"')
    # article_sections has no `project` field — scope through the article relation.
    sections_done = SectionRepo(pb).count(filter=f'article.project="{project_id}" && status="done"')
    return {
        "indexed_documents": indexed,
        "stale_documents": stale,
        "articles_generated": articles.count(filter=f'project="{project_id}" && generatedAt != ""'),
        "sections_generated": sections_done,
        "articles_published": articles.count(
            filter=f'project="{project_id}" && status="published"'
        ),
        "failed_jobs": jobs.count(filter=f'project="{project_id}" && status="failed"'),
    }
