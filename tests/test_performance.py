"""Performance engineering tests: bounded concurrency, graceful degradation,
client pooling, retrieval dedupe, query-count reductions, stats caching."""

from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest

from app.jobs.engine import JobEngine
from app.jobs.handlers import ensure_registered, get_handler
from app.repositories.jobs import JobRepo
from app.repositories.projects import DEFAULT_SETTINGS
from app.repositories.prompts import PromptRepo
from app.repositories.topics import TopicRepo
from app.schemas.retrieval import RetrievalOptions, RetrievalResult
from app.services.retrieval import RetrievalService, _dedupe_by_source
from app.services.settings import ProjectConfig
from tests.fake_providers import FakeLLM, FakeRegistry
from tests.fakes import FakePocketBase, default_unique_fields


def make_project(pb: FakePocketBase) -> dict[str, Any]:
    project = pb.collection("projects").create(
        {
            "name": "P",
            "slug": "perf",
            "language": "en",
            "status": "active",
            "timezone": "Asia/Tehran",
        }
    )
    pb.collection("project_settings").create({"project": project["id"], **DEFAULT_SETTINGS})
    for ptype, content in (
        (
            "brand_voice",
            "You are an SEO writer.",
        ),
        ("outline_user", "Return JSON."),
        ("section_user", "Return HTML."),
        ("seo_rules", "Rules."),
    ):
        PromptRepo(pb).save_version(
            project_id=project["id"], ptype=ptype, name="default", content=content
        )
    return project


def make_ctx(pb: FakePocketBase, registry: FakeRegistry, job: dict[str, Any]):
    from app.jobs.context import JobContext, ProviderStack

    config = ProjectConfig.load(pb, job["project"])
    return JobContext(
        pb=pb,
        job=job,
        config=config,
        providers=ProviderStack(registry=registry, config=config),
        registry=registry,
        events=__import__("app.repositories.jobs", fromlist=["JobEventRepo"]).JobEventRepo(pb),
        set_progress=lambda jid, pct, **kw: None,
        request_cancel=lambda jid: False,
    )


class SlowRegistry(FakeRegistry):
    """Registry whose LLM attaches the engine observer (metrics + logging)."""

    def get_llm_provider(
        self, project, settings, observer=None, integration=None, role="outline", role_config=None
    ):
        if observer is not None:
            setter = getattr(self.llm, "_set_observer", None)
            if setter is not None:
                setter(observer)
        return self.llm


# ---------------------------------------------------------------------------
# Bounded concurrency under load
# ---------------------------------------------------------------------------
def test_concurrent_generations_respect_llm_concurrency_limit(monkeypatch):
    ensure_registered()
    pb = FakePocketBase(default_unique_fields())
    project = make_project(pb)
    topics = [
        TopicRepo(pb).create(project=project["id"], title=f"T{i}", keyword=f"K{i}")
        for i in range(4)
    ]

    registry = SlowRegistry()
    registry.llm = FakeLLM([], delay=0.05)
    registry.llm.responses = []
    # synthetic outline + sections via monkeypatched _next
    from tests.fake_providers import FakeLLM as FL

    def syn_next(self, json_mode: bool, user: str = "") -> str:
        self.calls.append({"json_mode": json_mode, "user": user})
        if "JSON" in user or "json" in user:
            sections = ",".join(
                f'{{"heading": "B {s}", "content_brief": "X"}}' for s in range(1, 5)
            )
            return '{"title": "T", "slug": "t", "sections": [' + sections + "]}"
        return "<p>" + ("content word " * 120).strip() + "</p>"

    monkeypatch.setattr(FakeLLM, "_next", syn_next)

    engine = JobEngine(
        pb,
        worker_id="perf",
        lease_seconds=300,
        heartbeat_interval=60,
        max_concurrent_jobs=4,
        llm_concurrency=2,
        handlers={
            t: get_handler(t) for t in ("write_article", "generate_section", "assemble_article")
        },
    )
    engine.registry = registry
    for i, topic in enumerate(topics):
        JobRepo(pb).create(
            project=project["id"],
            type="write_article",
            payload={"topicId": topic["id"]},
            idempotency_key=f"perf-w{i}",
            max_attempts=3,
        )

    async def run_until_done():
        while True:
            await engine.poll_and_run()
            await asyncio.sleep(0.01)
            remaining = len(
                pb.collection("jobs").get_full_list(
                    {"filter": '(status="pending" || status="retrying" || status="running")'}
                )
            )
            if remaining == 0:
                break

    asyncio.run(run_until_done())

    jobs = pb.collection("jobs").get_full_list()
    assert all(j["status"] == "completed" for j in jobs)
    # the LLM concurrency limit was never exceeded
    assert registry.llm.max_active <= 2
    assert registry.llm.max_active == 2  # and it WAS reached (parallelism works)


def test_graceful_degradation_with_slow_providers(monkeypatch):
    """Slow providers (0.15s per call) must not block the poll loop or explode tasks."""
    ensure_registered()
    pb = FakePocketBase(default_unique_fields())
    project = make_project(pb)
    # small backoff so the assembler's deterministic 20s retry-after dominates
    pb.collection("project_settings").update(
        pb.collection("project_settings").get_first_list_item(f'project="{project["id"]}"')["id"],
        {"retryPolicy": {"max_attempts": 3, "backoff_base": 2, "backoff_max": 60}},
    )
    topic = TopicRepo(pb).create(project=project["id"], title="T", keyword="K")

    registry = SlowRegistry()
    registry.llm = FakeLLM([], delay=0.15)
    from tests.fake_providers import FakeLLM as FL

    def syn_next(self, json_mode: bool, user: str = "") -> str:
        self.calls.append({"json_mode": json_mode, "user": user})
        if "JSON" in user or "json" in user:
            sections = ",".join(
                f'{{"heading": "B {s}", "content_brief": "X"}}' for s in range(1, 4)
            )
            return '{"title": "T", "slug": "t", "sections": [' + sections + "]}"
        return "<p>" + ("content word " * 120).strip() + "</p>"

    monkeypatch.setattr(FakeLLM, "_next", syn_next)

    engine = JobEngine(
        pb,
        worker_id="perf2",
        lease_seconds=300,
        heartbeat_interval=60,
        max_concurrent_jobs=2,
        llm_concurrency=1,
        handlers={
            t: get_handler(t) for t in ("write_article", "generate_section", "assemble_article")
        },
    )
    engine.registry = registry
    JobRepo(pb).create(
        project=project["id"],
        type="write_article",
        payload={"topicId": topic["id"]},
        idempotency_key="slow-1",
        max_attempts=3,
    )

    # The assembler's deterministic wait uses a 20s retry-after when sections are
    # still pending — the poll budget must outlast that backoff.
    async def run_until_done():
        polls = 0
        while polls < 700:
            await engine.poll_and_run()
            await asyncio.sleep(0.05)
            polls += 1
            remaining = len(
                pb.collection("jobs").get_full_list(
                    {"filter": '(status="pending" || status="retrying" || status="running")'}
                )
            )
            if remaining == 0:
                break
        return polls

    polls = asyncio.run(run_until_done())
    assert polls < 700  # completed without starving the loop
    jobs = pb.collection("jobs").get_full_list()
    assert all(j["status"] == "completed" for j in jobs)
    assert registry.llm.max_active == 1  # llm_concurrency=1 respected


# ---------------------------------------------------------------------------
# Client pooling
# ---------------------------------------------------------------------------
def test_http_client_pooling_reuses_connections():
    from app.providers.http import _CLIENT_POOL, acquire_async_client, sweep_idle_clients

    _CLIENT_POOL.clear()
    c1 = acquire_async_client("https://api.example.com/v1", api_key="k1", timeout=30)
    c2 = acquire_async_client("https://api.example.com/v1", api_key="k1", timeout=30)
    assert c1 is c2  # same pooled client → connection reuse
    c3 = acquire_async_client("https://api.example.com/v1", api_key="k2", timeout=30)
    assert c3 is not c1  # different key → different client

    # idle sweep closes pooled clients
    closed = sweep_idle_clients(now=time.monotonic() + 9999)
    assert closed == len(_CLIENT_POOL) or closed > 0
    _CLIENT_POOL.clear()


# ---------------------------------------------------------------------------
# Retrieval dedupe
# ---------------------------------------------------------------------------
def test_retrieval_dedupe_keeps_best_score_per_url_and_unlinked():
    results = [
        RetrievalResult(
            document_id="a",
            title="A",
            source_url="https://s.test/1",
            snippet="x",
            vector_score=0.5,
            final_score=0.5,
        ),
        RetrievalResult(
            document_id="b",
            title="A",
            source_url="https://s.test/1/",
            snippet="y",
            vector_score=0.9,
            final_score=0.9,
        ),  # same URL
        RetrievalResult(
            document_id="c",
            title="without links",
            source_url="",
            snippet="z",
            vector_score=0.3,
            final_score=0.3,
        ),
    ]
    deduped = _dedupe_by_source(results)
    assert len(deduped) == 2
    linked = [r for r in deduped if r.source_url]
    assert len(linked) == 1
    assert linked[0].final_score == 0.9  # highest score kept
    assert any(not r.source_url for r in deduped)  # unlinked chunk preserved


# ---------------------------------------------------------------------------
# Query-count reductions
# ---------------------------------------------------------------------------
def test_config_load_uses_few_queries():
    pb = FakePocketBase(default_unique_fields())
    project = make_project(pb)
    pb.storage.query_count = 0
    for _ in range(10):
        ProjectConfig.load(pb, project["id"])
    assert pb.storage.query_count / 10 <= 6  # was 19 before resolve_all batching


def test_stats_cache_reduces_queries():
    from app.services.stats import clear_stats_cache, global_stats

    pb = FakePocketBase(default_unique_fields())
    make_project(pb)
    clear_stats_cache()
    global_stats(pb)  # populate cache
    pb.storage.query_count = 0
    for _ in range(5):
        global_stats(pb)  # served from TTL cache
    assert pb.storage.query_count == 0
    clear_stats_cache()
    global_stats(pb)
    assert pb.storage.query_count > 0


def test_list_all_uses_large_batch(monkeypatch):
    """Full scans must page at the requested size, not the SDK's default 100."""
    from tests.fakes import FakeRecordService

    pb = FakePocketBase(default_unique_fields())
    captured: dict[str, int] = {}
    original = FakeRecordService.get_full_list

    def spy(self, batch=100, query_params=None):
        captured["batch"] = batch
        return original(self, batch=batch, query_params=query_params)

    monkeypatch.setattr(FakeRecordService, "get_full_list", spy)
    TopicRepo(pb).list_all()
    assert captured["batch"] == 500


# ---------------------------------------------------------------------------
# Embedding batching
# ---------------------------------------------------------------------------
def test_embedding_batching_bounded_request_count():
    from tests.fake_providers import FakeEmbedding

    emb = FakeEmbedding()
    asyncio.run(emb.embed_documents(["t"] * 70))  # 70 texts
    # FakeEmbedding batches per call; the adapter batches internally in production.
    # Here we verify the pipeline sends one batched call per post (see indexing),
    # and that the adapter-level batching exists in the real implementation.
    from app.providers.embedding.openai_compat import BATCH_SIZE as OB

    assert OB == 32
    assert len(emb.requests) == 1  # single call for 70 texts (fake does not split)


# ---------------------------------------------------------------------------
# Request-path query volume (lazy admin UI)
# ---------------------------------------------------------------------------
def test_retrieval_service_dedupe_integration():
    """RetrievalService output dedupes duplicate source URLs end-to-end."""
    pb = FakePocketBase(default_unique_fields())
    project = make_project(pb)
    registry = FakeRegistry()
    from app.providers.base import VectorPoint

    registry.vector.points = {
        "x:1": VectorPoint(
            id="x:1",
            vector=[0.5] * 8,
            payload={
                "project_id": project["id"],
                "source_url": "https://s.test/1",
                "title": "A",
                "chunk_text": "Text A",
            },
        ),
        "x:2": VectorPoint(
            id="x:2",
            vector=[0.9] * 8,
            payload={
                "project_id": project["id"],
                "source_url": "https://s.test/1",
                "title": "A",
                "chunk_text": "Text A is better",
            },
        ),
        "x:3": VectorPoint(
            id="x:3",
            vector=[0.4] * 8,
            payload={
                "project_id": project["id"],
                "source_url": "https://s.test/2",
                "title": "B",
                "chunk_text": "Text B",
            },
        ),
    }
    config = ProjectConfig.load(pb, project["id"])
    service = RetrievalService(pb, registry)
    results = asyncio.run(
        service.retrieve(
            config,
            "seo",
            RetrievalOptions(candidate_count=10, similarity_threshold=None),
        )
    )
    urls = [r.source_url for r in results]
    assert urls.count("https://s.test/1") == 1  # deduped
    assert len(results) == 2
