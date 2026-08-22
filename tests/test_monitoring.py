"""Operational monitoring tests: provider metrics accumulation + P95,
job search filters, project metrics."""

from __future__ import annotations

from typing import Any

from app.providers.metrics import ProviderCallRecord
from app.repositories.jobs import JobRepo
from app.services.metrics import (
    MetricsAccumulator,
    ProviderMetricsObserver,
    flush_accumulator,
    project_metrics,
    query_provider_metrics,
)
from tests.fakes import FakePocketBase, default_unique_fields


def make_project(pb: FakePocketBase, slug: str = "p1") -> dict[str, Any]:
    return pb.collection("projects").create(
        {"name": "پ", "slug": slug, "language": "fa", "status": "active", "timezone": "Asia/Tehran"}
    )


def seed_job(pb: FakePocketBase, project_id: str, **overrides: Any) -> dict[str, Any]:
    data = {
        "project": project_id,
        "type": "write_article",
        "payload": {},
        "idempotencyKey": f"k:{project_id}:{len(pb.collection('jobs').get_full_list())}",
        "status": "pending",
        "attempts": 0,
        "maxAttempts": 3,
        "progress": 0,
        "cancelRequested": False,
        "errorCode": "",
    }
    data.update(overrides)
    return pb.collection("jobs").create(data)


# ---------------------------------------------------------------------------
# Accumulator
# ---------------------------------------------------------------------------
def test_accumulator_aggregates_and_drains():
    acc = MetricsAccumulator()
    obs = ProviderMetricsObserver(acc)
    obs.on_call(
        ProviderCallRecord(
            provider="cohere",
            model="embed-v4.0",
            operation="embedding.search_document",
            latency_ms=100,
            success=True,
            project_id="p1",
        )
    )
    obs.on_call(
        ProviderCallRecord(
            provider="cohere",
            model="embed-v4.0",
            operation="embedding.search_document",
            latency_ms=800,
            success=True,
            project_id="p1",
        )
    )
    obs.on_call(
        ProviderCallRecord(
            provider="cohere",
            model="embed-v4.0",
            operation="embedding.search_document",
            latency_ms=3000,
            success=False,
            project_id="p1",
            retries=2,
            error_category="transient",
        )
    )
    obs.on_call(
        ProviderCallRecord(
            provider="gemini",
            model="gemini-2.0-flash",
            operation="llm.generate",
            latency_ms=500,
            success=True,
            project_id="p1",
            prompt_tokens=10,
            completion_tokens=20,
        )
    )

    rows = acc.drain()
    assert len(rows) == 2
    embed = next(r for r in rows if r["operation"] == "embedding.search_document")
    assert embed["requestCount"] == 3
    assert embed["successCount"] == 2
    assert embed["failureCount"] == 1
    assert embed["retryCount"] == 2
    assert embed["latencySum"] == 3900
    assert embed["latencyBuckets"].get("1") == 1  # 0.8s → bucket 1
    assert embed["latencyBuckets"].get("5") == 1  # 3s → bucket 5
    assert acc.drain() == []  # drained


def test_flush_writes_and_accumulates_across_flushes():
    pb = FakePocketBase(default_unique_fields())
    project = make_project(pb)
    acc = MetricsAccumulator()
    acc.record(
        ProviderCallRecord(
            provider="x",
            model="m",
            operation="op",
            latency_ms=100,
            success=True,
            project_id=project["id"],
        )
    )
    assert flush_accumulator(pb, acc) == 1

    rows = query_provider_metrics(pb, project_id=project["id"])
    assert len(rows) == 1
    assert rows[0]["requestCount"] == 1
    assert rows[0]["successRate"] == 1.0
    assert rows[0]["avgLatencyMs"] == 100.0

    # second flush accumulates into the same row
    acc.record(
        ProviderCallRecord(
            provider="x",
            model="m",
            operation="op",
            latency_ms=300,
            success=True,
            project_id=project["id"],
        )
    )
    assert flush_accumulator(pb, acc) == 1
    rows = query_provider_metrics(pb, project_id=project["id"])
    assert rows[0]["requestCount"] == 2
    assert rows[0]["avgLatencyMs"] == 200.0


def test_p95_from_histogram_buckets():
    pb = FakePocketBase(default_unique_fields())
    project = make_project(pb)
    acc = MetricsAccumulator()
    # 100 calls: 90 at 100ms (bucket 0.25), 10 at 2000ms (bucket 2) → P95 = 2000ms
    for _ in range(90):
        acc.record(
            ProviderCallRecord(
                provider="x",
                model="m",
                operation="op",
                latency_ms=100,
                success=True,
                project_id=project["id"],
            )
        )
    for _ in range(10):
        acc.record(
            ProviderCallRecord(
                provider="x",
                model="m",
                operation="op",
                latency_ms=2000,
                success=True,
                project_id=project["id"],
            )
        )
    flush_accumulator(pb, acc)
    rows = query_provider_metrics(pb, project_id=project["id"])
    assert rows[0]["p95LatencyMs"] == 2000


def test_metrics_filtered_by_project():
    pb = FakePocketBase(default_unique_fields())
    p1, p2 = make_project(pb, "a"), make_project(pb, "b")
    acc = MetricsAccumulator()
    acc.record(
        ProviderCallRecord(
            provider="x",
            model="m",
            operation="op",
            latency_ms=100,
            success=True,
            project_id=p1["id"],
        )
    )
    acc.record(
        ProviderCallRecord(
            provider="x",
            model="m",
            operation="op",
            latency_ms=100,
            success=True,
            project_id=p2["id"],
        )
    )
    flush_accumulator(pb, acc)
    assert len(query_provider_metrics(pb, project_id=p1["id"])) == 1
    assert len(query_provider_metrics(pb, project_id=p2["id"])) == 1
    assert len(query_provider_metrics(pb)) == 1  # same provider/model/op → single aggregate


# ---------------------------------------------------------------------------
# Job search filters
# ---------------------------------------------------------------------------
def test_job_search_filters():
    pb = FakePocketBase(default_unique_fields())
    project = make_project(pb)
    seed_job(pb, project["id"], type="index_project", status="completed", lockedBy="worker-a")
    seed_job(
        pb,
        project["id"],
        type="write_article",
        status="failed",
        lockedBy="worker-b",
        errorCode="PermanentError",
    )
    seed_job(pb, project["id"], type="generate_section", status="running", lockedBy="worker-a")

    rows, total = JobRepo(pb).search(project_id=project["id"])
    assert total == 3

    rows, total = JobRepo(pb).search(project_id=project["id"], job_type="write_article")
    assert total == 1
    assert rows[0]["errorCode"] == "PermanentError"

    rows, total = JobRepo(pb).search(project_id=project["id"], status="failed")
    assert total == 1

    rows, total = JobRepo(pb).search(project_id=project["id"], worker="worker-a")
    assert total == 2

    rows, total = JobRepo(pb).search(project_id=project["id"], failure_type="PermanentError")
    assert total == 1

    rows, total = JobRepo(pb).search(
        project_id=project["id"], job_type="index_project", status="completed"
    )
    assert total == 1

    # date filters (created today)
    rows, total = JobRepo(pb).search(project_id=project["id"], date_from="2020-01-01")
    assert total == 3
    rows, total = JobRepo(pb).search(project_id=project["id"], date_from="2099-01-01")
    assert total == 0


def test_error_codes_collection():
    pb = FakePocketBase(default_unique_fields())
    project = make_project(pb)
    seed_job(pb, project["id"], status="failed", errorCode="PermanentError")
    seed_job(pb, project["id"], status="failed", errorCode="TransientError")
    seed_job(pb, project["id"], status="failed")
    codes = JobRepo(pb).error_codes()
    assert "PermanentError" in codes
    assert "TransientError" in codes


# ---------------------------------------------------------------------------
# Project metrics
# ---------------------------------------------------------------------------
def test_project_metrics_counts():
    pb = FakePocketBase(default_unique_fields())
    project = make_project(pb)
    topic = pb.collection("topics").create(
        {"project": project["id"], "title": "ت", "status": "published"}
    )
    pb.collection("documents").create(
        {
            "project": project["id"],
            "sourceType": "wordpress",
            "sourceId": "1",
            "indexStatus": "indexed",
            "contentHash": "a",
        }
    )
    pb.collection("documents").create(
        {
            "project": project["id"],
            "sourceType": "wordpress",
            "sourceId": "2",
            "indexStatus": "deleted",
            "contentHash": "b",
        }
    )
    article = pb.collection("articles").create(
        {
            "project": project["id"],
            "topicId": topic["id"],
            "title": "الف",
            "status": "published",
            "generatedAt": "2026-01-01 10:00:00.000Z",
        }
    )
    pb.collection("article_sections").create(
        {"article": article["id"], "position": 0, "heading": "ب", "status": "done"}
    )
    seed_job(pb, project["id"], status="failed")

    metrics = project_metrics(pb, project["id"])
    assert metrics["indexed_documents"] == 1
    assert metrics["stale_documents"] == 1
    assert metrics["articles_generated"] == 1
    assert metrics["sections_generated"] == 1
    assert metrics["articles_published"] == 1
    assert metrics["failed_jobs"] == 1
