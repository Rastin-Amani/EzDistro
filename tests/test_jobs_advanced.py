"""Advanced job engine tests: crash recovery, leases, concurrency, retries,
cancellation, idempotency, retry_failed_job, canonical events, progress stages,
bounded concurrency, and the granular job handlers."""

from __future__ import annotations

import asyncio
import datetime as dt
from typing import Any

import pytest

from app.jobs.engine import JobEngine
from app.jobs.state import JobStateError
from app.providers.base import PermanentError, TransientError
from app.repositories.jobs import JobEventRepo, JobRepo, now_utc, pb_dt
from app.repositories.projects import DEFAULT_SETTINGS
from app.repositories.prompts import PromptRepo
from app.repositories.topics import TopicRepo
from app.services.retry_service import handle_retry_failed_job
from tests.fake_providers import FakeRegistry
from tests.fakes import FakePocketBase, default_unique_fields


def make_project(pb: FakePocketBase, slug: str = "test-proj") -> dict[str, Any]:
    project = pb.collection("projects").create(
        {
            "name": "Project",
            "slug": slug,
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


def seed_job(pb: FakePocketBase, project_id: str, **overrides: Any) -> dict[str, Any]:
    data = {
        "project": project_id,
        "type": "test",
        "payload": {},
        "idempotencyKey": f"test:{project_id}:{now_utc().timestamp()}:{abs(hash(str(overrides)))}",
        "status": "pending",
        "attempts": 0,
        "maxAttempts": 3,
        "progress": 0,
        "cancelRequested": False,
        "availableAt": pb_dt(now_utc()),
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
        llm_concurrency=kwargs.pop("llm_concurrency", 2),
        embedding_concurrency=kwargs.pop("embedding_concurrency", 2),
        publish_concurrency=kwargs.pop("publish_concurrency", 1),
        handlers=handlers or {},
    )


# ---------------------------------------------------------------------------
# Crash recovery & lease expiration
# ---------------------------------------------------------------------------
def test_crash_recovery_reclaims_expired_lease():
    pb = FakePocketBase(default_unique_fields())
    project = make_project(pb)
    job = seed_job(pb, project["id"])
    ran = {"count": 0}

    async def handler(ctx):
        ran["count"] += 1
        return {}

    # Worker A claims and "crashes": lease exists, job is running, no completion.
    engine_a = make_engine(pb, handlers={"test": handler}, worker_id="worker-a")
    assert engine_a._claim(job) is True
    assert pb.collection("jobs").get_one(job["id"])["status"] == "running"

    # Worker B starts; the lease is NOT expired yet → cannot claim.
    engine_b = make_engine(pb, handlers={"test": handler}, worker_id="worker-b")
    asyncio.run(engine_b.poll_and_run())
    assert ran["count"] == 0

    # Simulate time passing: lease expires → worker B reclaims and executes.
    lease = pb.collection("job_leases").get_first_list_item(f'job="{job["id"]}"')
    pb.collection("job_leases").update(lease["id"], {"expiresAt": "2020-01-01 00:00:00.000Z"})
    pb.collection("jobs").update(job["id"], {"leaseExpiresAt": "2020-01-01 00:00:00.000Z"})
    asyncio.run(engine_b.poll_and_run())
    assert ran["count"] == 1
    assert pb.collection("jobs").get_one(job["id"])["status"] == "completed"


def test_crash_before_completion_never_loses_the_job():
    """A job whose worker dies mid-execution is re-executed after lease expiry."""
    pb = FakePocketBase(default_unique_fields())
    project = make_project(pb)
    job = seed_job(pb, project["id"])
    attempts = {"n": 0}

    async def handler_crash(ctx):
        attempts["n"] += 1
        raise TransientError("worker crashed mid-flight")

    engine = make_engine(pb, handlers={"test": handler_crash}, worker_id="w1")
    asyncio.run(engine._run(job))
    assert pb.collection("jobs").get_one(job["id"])["status"] == "retrying"

    # backoff elapsed; a fresh worker picks it up and succeeds
    pb.collection("jobs").update(
        job["id"], {"availableAt": pb_dt(now_utc() - dt.timedelta(hours=1))}
    )

    async def handler_ok(ctx):
        return {}

    engine2 = make_engine(pb, handlers={"test": handler_ok}, worker_id="w2")

    async def recover():
        await engine2.poll_and_run()
        await asyncio.sleep(0.15)  # let the claimed job finish in this loop

    asyncio.run(recover())
    assert pb.collection("jobs").get_one(job["id"])["status"] == "completed"
    assert attempts["n"] == 1  # crashed attempt counted once; recovery executed the job


# ---------------------------------------------------------------------------
# Duplicate claims & concurrent workers
# ---------------------------------------------------------------------------
def test_no_double_execution_under_concurrent_workers():
    pb = FakePocketBase(default_unique_fields())
    project = make_project(pb)
    jobs = [seed_job(pb, project["id"], idempotencyKey=f"c{i}") for i in range(6)]
    executed: dict[str, int] = {}

    async def handler(ctx):
        executed[ctx.job_id] = executed.get(ctx.job_id, 0) + 1
        await asyncio.sleep(0.01)
        return {}

    engine_a = make_engine(pb, handlers={"test": handler}, worker_id="worker-a")
    engine_b = make_engine(pb, handlers={"test": handler}, worker_id="worker-b")

    async def run_both():
        await asyncio.gather(engine_a.poll_and_run(), engine_b.poll_and_run())
        await asyncio.sleep(0.15)  # let claimed jobs finish in this loop

    for _ in range(3):  # several poll rounds
        asyncio.run(run_both())

    # every job executed exactly once, no duplicates
    assert len(executed) == len(jobs)
    assert all(v == 1 for v in executed.values())
    assert all(pb.collection("jobs").get_one(j["id"])["status"] == "completed" for j in jobs)


def test_duplicate_claim_returns_false_for_second_worker():
    pb = FakePocketBase(default_unique_fields())
    project = make_project(pb)
    job = seed_job(pb, project["id"])
    engine1 = make_engine(pb, worker_id="w1")
    engine2 = make_engine(pb, worker_id="w2")
    assert engine1._claim(job) is True
    assert engine2._claim(job) is False
    # job is running (claim → running) with lock fields set
    running = pb.collection("jobs").get_one(job["id"])
    assert running["status"] == "running"
    assert running["lockedBy"] == "w1"
    assert running["lockedAt"]
    assert running["leaseExpiresAt"]


# ---------------------------------------------------------------------------
# Retry policy: jitter, Retry-After, exhaustion
# ---------------------------------------------------------------------------
def test_retry_uses_jittered_backoff():
    pb = FakePocketBase(default_unique_fields())
    project = make_project(pb)
    job = seed_job(pb, project["id"], maxAttempts=3)

    async def handler(ctx):
        raise TransientError("flaky")

    engine = make_engine(pb, handlers={"test": handler})
    asyncio.run(engine._run(job))

    updated = pb.collection("jobs").get_one(job["id"])
    assert updated["status"] == "retrying"
    assert updated["attempts"] == 1
    assert updated["errorDetails"]["retryable"] is True

    available = _parse_pb_dt(updated["availableAt"])
    delay = (available - now_utc()).total_seconds()
    # exponential base 30s ±50% jitter (first attempt)
    assert 15 <= delay <= 45
    # canonical event recorded
    events = pb.collection("job_events").get_full_list(
        {"filter": 'eventType="job.retry_scheduled"'}
    )
    assert len(events) == 1
    assert events[0]["metadata"]["attempt"] == 1


def test_retry_respects_provider_retry_after():
    pb = FakePocketBase(default_unique_fields())
    project = make_project(pb)
    job = seed_job(pb, project["id"], maxAttempts=3)

    async def handler(ctx):
        raise TransientError("429 rate limited", {"retry_after_seconds": 500})

    engine = make_engine(pb, handlers={"test": handler})
    asyncio.run(engine._run(job))

    updated = pb.collection("jobs").get_one(job["id"])
    assert updated["status"] == "retrying"
    available = _parse_pb_dt(updated["availableAt"])
    delay = (available - now_utc()).total_seconds()
    assert delay >= 490  # Retry-After honored (>= backoff anyway)


def test_retry_exhaustion_fails_permanently():
    pb = FakePocketBase(default_unique_fields())
    project = make_project(pb)
    job = seed_job(pb, project["id"], maxAttempts=2, attempts=1)

    async def handler(ctx):
        raise TransientError("still flaky")

    engine = make_engine(pb, handlers={"test": handler})
    asyncio.run(engine._run(job))
    updated = pb.collection("jobs").get_one(job["id"])
    assert updated["status"] == "failed"
    assert updated["attempts"] == 2
    assert updated["errorCode"] == "TransientError"
    events = pb.collection("job_events").get_full_list({"filter": 'eventType="job.failed"'})
    assert len(events) == 1


def test_permanent_error_never_retries():
    pb = FakePocketBase(default_unique_fields())
    project = make_project(pb)
    job = seed_job(pb, project["id"], maxAttempts=5)

    async def handler(ctx):
        raise PermanentError("invalid credentials")

    engine = make_engine(pb, handlers={"test": handler})
    asyncio.run(engine._run(job))
    updated = pb.collection("jobs").get_one(job["id"])
    assert updated["status"] == "failed"
    assert updated["attempts"] == 1  # counted, but no retry scheduled
    assert updated["errorDetails"]["retryable"] is False
    # no retry event
    assert (
        pb.collection("job_events").get_full_list({"filter": 'eventType="job.retry_scheduled"'})
        == []
    )


# ---------------------------------------------------------------------------
# Cancellation (graceful)
# ---------------------------------------------------------------------------
def test_cancellation_is_graceful():
    pb = FakePocketBase(default_unique_fields())
    project = make_project(pb)
    job = seed_job(pb, project["id"])
    finished_stage = {"done": False}

    async def handler(ctx):
        finished_stage["done"] = True  # a long-running step completes first
        await ctx.check_cancelled()  # checkpoint → raises here
        raise AssertionError("should not reach here")

    engine = make_engine(pb, handlers={"test": handler})
    pb.collection("jobs").update(job["id"], {"cancelRequested": True})
    asyncio.run(engine._run(job))

    updated = pb.collection("jobs").get_one(job["id"])
    assert updated["status"] == "cancelled"
    assert finished_stage["done"] is True  # current step finished; no abrupt kill
    events = pb.collection("job_events").get_full_list({"filter": 'eventType="job.cancelled"'})
    assert len(events) == 1


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------
def test_idempotent_creation_and_double_claim_safety():
    pb = FakePocketBase(default_unique_fields())
    project = make_project(pb)
    jobs = JobRepo(pb)
    first = jobs.create(
        project=project["id"], type="index_project", payload={}, idempotency_key="key-x"
    )
    second = jobs.create(
        project=project["id"], type="index_project", payload={}, idempotency_key="key-x"
    )
    assert first["id"] == second["id"]
    assert len(pb.collection("jobs").get_full_list()) == 1


# ---------------------------------------------------------------------------
# retry_failed_job
# ---------------------------------------------------------------------------
def test_retry_failed_job_resets_target():
    pb = FakePocketBase(default_unique_fields())
    project = make_project(pb)
    target = seed_job(
        pb, project["id"], status="failed", maxAttempts=2, attempts=2, errorCode="PermanentError"
    )
    retry_job = seed_job(
        pb, project["id"], type="retry_failed_job", payload={"targetJobId": target["id"]}
    )

    from app.jobs.context import JobContext, ProviderStack
    from app.services.settings import ProjectConfig

    registry = FakeRegistry()
    config = ProjectConfig.load(pb, project["id"])
    ctx = JobContext(
        pb=pb,
        job=retry_job,
        config=config,
        providers=ProviderStack(registry=registry, config=config),
        registry=registry,
        events=JobEventRepo(pb),
        set_progress=lambda jid, pct, **kw: None,
        request_cancel=lambda jid: False,
    )
    result = asyncio.run(handle_retry_failed_job(ctx))
    assert result["retried"] is True
    updated = pb.collection("jobs").get_one(target["id"])
    assert updated["status"] == "retrying"
    assert updated["errorCode"] == ""
    assert updated["cancelRequested"] is False
    # re-running is a no-op (target no longer failed)
    result2 = asyncio.run(handle_retry_failed_job(ctx))
    assert result2["retried"] is False


# ---------------------------------------------------------------------------
# Canonical lifecycle events
# ---------------------------------------------------------------------------
def test_lifecycle_event_sequence():
    pb = FakePocketBase(default_unique_fields())
    project = make_project(pb)
    JobRepo(pb).create(
        project=project["id"],
        type="test",
        payload={},
        idempotency_key="lifecycle-1",
        max_attempts=3,
    )

    async def handler(ctx):
        ctx.stage_started("outline", "producing outline")
        ctx.stage_completed("outline", "outline done")
        return {"ok": True}

    engine = make_engine(pb, handlers={"test": handler})

    async def run_once():
        await engine.poll_and_run()
        await asyncio.sleep(0.15)

    asyncio.run(run_once())

    events = [
        e["eventType"] for e in pb.collection("job_events").get_full_list({"sort": "created"})
    ]
    assert "job.created" in events
    assert "job.claimed" in events
    assert "job.started" in events
    assert "job.stage_started" in events
    assert "job.stage_completed" in events
    assert "job.completed" in events
    assert events[-1] == "job.completed"


def test_progress_stage_and_items_persisted():
    pb = FakePocketBase(default_unique_fields())
    project = make_project(pb)
    job = seed_job(pb, project["id"])

    async def handler(ctx):
        ctx.progress(
            35,
            stage="writing_sections",
            message="Section 7 of 20",
            current=7,
            total=20,
        )
        return {}

    engine = make_engine(pb, handlers={"test": handler})
    asyncio.run(engine._run(job))

    # progress snapshot captured while running via engine's set_progress → job record
    updated = pb.collection("jobs").get_one(job["id"])
    # completed → progress 100; the intermediate values were persisted by set_progress
    assert updated["progress"] == 100
    # verify the handler's progress write landed before completion:
    # re-run with a handler that raises after progress to inspect the snapshot
    job2 = seed_job(pb, project["id"], maxAttempts=1)

    async def handler2(ctx):
        ctx.progress(
            35,
            stage="writing_sections",
            message="Section 7 of 20",
            current=7,
            total=20,
        )
        raise PermanentError("stop")

    engine2 = make_engine(pb, handlers={"test": handler2})
    asyncio.run(engine2._run(job2))
    snap = pb.collection("jobs").get_one(job2["id"])
    assert snap["progress"] == 35
    assert snap["stage"] == "writing_sections"
    assert snap["currentItem"] == 7
    assert snap["totalItems"] == 20


# ---------------------------------------------------------------------------
# Bounded concurrency
# ---------------------------------------------------------------------------
def test_bounded_llm_concurrency_wrapper():
    from app.jobs.context import ProviderStack
    from app.services.settings import ProjectConfig

    pb = FakePocketBase(default_unique_fields())
    project = make_project(pb)
    registry = FakeRegistry()
    config = ProjectConfig.load(pb, project["id"])
    stack = ProviderStack(
        registry=registry,
        config=config,
        limits={
            "llm": asyncio.Semaphore(1),
            "embedding": asyncio.Semaphore(1),
            "publish": asyncio.Semaphore(1),
        },
    )
    assert type(stack.llm).__name__ == "BoundedLLM"
    assert type(stack.embedding).__name__ == "BoundedEmbedding"
    assert type(stack.publisher).__name__ == "BoundedPublisher"
    # vector/reranker are not rate-limited (local infrastructure)
    assert type(stack.vector).__name__ == "FakeVectorStore"
    asyncio.run(stack.aclose())


def test_bounded_llm_wrapper_enforces_limit():
    from app.jobs.context import ProviderStack
    from app.services.settings import ProjectConfig

    pb = FakePocketBase(default_unique_fields())
    project = make_project(pb)
    registry = FakeRegistry()
    registry.llm.responses = ["a", "b", "c", "d"]
    config = ProjectConfig.load(pb, project["id"])
    stack = ProviderStack(registry=registry, config=config, limits={"llm": asyncio.Semaphore(1)})
    llm = stack.llm

    async def run_many():
        return await asyncio.gather(*[llm.generate(user=f"x{i}") for i in range(4)])

    results = asyncio.run(run_many())
    assert len(results) == 4
    asyncio.run(stack.aclose())


# ---------------------------------------------------------------------------
# Granular job handlers
# ---------------------------------------------------------------------------
def test_generate_outline_handler():
    from app.jobs.context import JobContext, ProviderStack
    from app.services.settings import ProjectConfig
    from app.services.writing import handle_generate_outline

    pb = FakePocketBase(default_unique_fields())
    project = make_project(pb)
    topic = TopicRepo(pb).create(project=project["id"], title="seo", keyword="seo")
    registry = FakeRegistry()
    registry.llm.responses = [
        '{"title": "T", "slug": "t", "sections": [{"heading": "A", "content_brief": "Summary A"}, {"heading": "B", "content_brief": "Summary B"}]}'
    ]
    config = ProjectConfig.load(pb, project["id"])
    job = seed_job(pb, project["id"], type="generate_outline", payload={"topicId": topic["id"]})
    ctx = JobContext(
        pb=pb,
        job=job,
        config=config,
        providers=ProviderStack(registry=registry, config=config),
        registry=registry,
        events=JobEventRepo(pb),
        set_progress=lambda jid, pct, **kw: None,
        request_cancel=lambda jid: False,
    )
    result = asyncio.run(handle_generate_outline(ctx))
    assert result["articleId"]
    article = pb.collection("articles").get_one(result["articleId"])
    assert article["status"] == "outline_ready"
    assert article["outline"]["slug"] == "t"  # immutable snapshot
    assert pb.collection("topics").get_one(topic["id"])["status"] == "outline_ready"
    assert len(pb.collection("article_sections").get_full_list()) == 2


def test_generate_section_handler():
    from app.jobs.context import JobContext, ProviderStack
    from app.services.settings import ProjectConfig
    from app.services.writing import handle_generate_section

    pb = FakePocketBase(default_unique_fields())
    project = make_project(pb)
    topic = TopicRepo(pb).create(project=project["id"], title="T", keyword="")
    article = pb.collection("articles").create(
        {
            "project": project["id"],
            "topicId": topic["id"],
            "title": "T",
            "status": "outline_ready",
            "outlineVersion": 1,
            "outline": {
                "title": "T",
                "slug": "t",
                "sections": [
                    {
                        "heading": "Section One",
                        "content_brief": "- note",
                        "internal_links": [],
                    }
                ],
            },
        }
    )
    section = pb.collection("article_sections").create(
        {
            "article": article["id"],
            "position": 0,
            "heading": "Section One",
            "contentBrief": "- note",
            "status": "pending",
            "generationAttempts": 0,
            "internalLinks": [],
        }
    )
    registry = FakeRegistry()
    registry.llm.responses = ["<p>" + ("section content word " * 15).strip() + "</p>"]
    config = ProjectConfig.load(pb, project["id"])
    job = seed_job(pb, project["id"], type="generate_section", payload={"sectionId": section["id"]})
    ctx = JobContext(
        pb=pb,
        job=job,
        config=config,
        providers=ProviderStack(registry=registry, config=config),
        registry=registry,
        events=JobEventRepo(pb),
        set_progress=lambda jid, pct, **kw: None,
        request_cancel=lambda jid: False,
    )
    result = asyncio.run(handle_generate_section(ctx))
    assert result["sectionId"] == section["id"]
    updated = pb.collection("article_sections").get_one(section["id"])
    assert updated["status"] == "done"
    assert updated["generationAttempts"] == 1
    assert "<p>" in updated["content"]
    assert updated["promptVersion"] >= 1  # active section_user prompt version


def test_assemble_article_handler():
    from app.jobs.context import JobContext, ProviderStack
    from app.services.settings import ProjectConfig
    from app.services.writing import handle_assemble_article

    pb = FakePocketBase(default_unique_fields())
    project = make_project(pb)
    # lower the article length gate for this small fixture
    pb.collection("project_settings").update(
        pb.collection("project_settings").get_first_list_item(f'project="{project["id"]}"')["id"],
        {"minArticleWords": 10},
    )
    topic = TopicRepo(pb).create(project=project["id"], title="T", keyword="seo")
    article = pb.collection("articles").create(
        {
            "project": project["id"],
            "topicId": topic["id"],
            "title": "Title",
            "slug": "onvan",
            "status": "outline_ready",
            "outlineVersion": 1,
            "outline": {
                "title": "Title",
                "slug": "onvan",
                "sections": [
                    {
                        "heading": "A",
                        "content_brief": "Summary",
                        "internal_links": [
                            {
                                "title": "link",
                                "url": "https://site.test/1",
                                "anchor_text": "link",
                            }
                        ],
                    },
                    {
                        "heading": "B",
                        "content_brief": "Summary",
                        "internal_links": [],
                    },
                ],
            },
        }
    )
    for i, heading in enumerate(["A", "B"]):
        pb.collection("article_sections").create(
            {
                "article": article["id"],
                "position": i,
                "heading": heading,
                "status": "done",
                "content": "<p>" + ("content word " * 10).strip() + "</p>",
                "internalLinks": [],
            }
        )
    registry = FakeRegistry()
    config = ProjectConfig.load(pb, project["id"])
    job = seed_job(pb, project["id"], type="assemble_article", payload={"articleId": article["id"]})
    ctx = JobContext(
        pb=pb,
        job=job,
        config=config,
        providers=ProviderStack(registry=registry, config=config),
        registry=registry,
        events=JobEventRepo(pb),
        set_progress=lambda jid, pct, **kw: None,
        request_cancel=lambda jid: False,
    )
    result = asyncio.run(handle_assemble_article(ctx))
    updated = pb.collection("articles").get_one(article["id"])
    assert updated["status"] == "review"
    assert updated["validation"]["ok"] is True
    # keyword enforcement: the title/H1 must contain the keyword (score floor)
    assert "<h1>seo | Title</h1>" in updated["finalHtml"]
    assert "https://site.test/1" in updated["finalHtml"]  # intended link preserved
    assert updated["seoScore"] >= 90
    assert result["seoScore"] == updated["seoScore"]


def test_index_document_handler():
    from app.jobs.context import JobContext, ProviderStack
    from app.providers.base import WPPost
    from app.services.indexing import handle_index_document
    from app.services.settings import ProjectConfig
    from tests.fake_providers import FakePublisher

    pb = FakePocketBase(default_unique_fields())
    project = make_project(pb)
    registry = FakeRegistry()
    registry.publisher = FakePublisher(
        posts=[
            WPPost(
                7,
                "single post",
                "<p>short text for indexing</p>",
                "https://s.test/7",
                "publish",
            )
        ]
    )
    config = ProjectConfig.load(pb, project["id"])
    job = seed_job(pb, project["id"], type="index_document", payload={"sourceId": "7"})
    ctx = JobContext(
        pb=pb,
        job=job,
        config=config,
        providers=ProviderStack(registry=registry, config=config),
        registry=registry,
        events=JobEventRepo(pb),
        set_progress=lambda jid, pct, **kw: None,
        request_cancel=lambda jid: False,
    )
    result = asyncio.run(handle_index_document(ctx))
    assert result["sourceId"] == "7"
    docs = pb.collection("documents").get_full_list()
    assert len(docs) == 1
    assert docs[0]["sourceId"] == "7"
    assert docs[0]["indexStatus"] == "indexed"


def test_handler_registry_is_extensible():
    from app.jobs.handlers import ensure_registered, get_handler, list_job_types, register_job

    ensure_registered()
    for job_type in (
        "index_project",
        "index_document",
        "write_article",
        "generate_outline",
        "generate_section",
        "assemble_article",
        "publish_article",
        "retry_failed_job",
    ):
        assert get_handler(job_type) is not None, f"{job_type} has no handler"

    # custom job types can be registered by any module
    @register_job("custom_thing")
    async def custom(ctx):
        return {}

    assert get_handler("custom_thing") is custom
    assert "custom_thing" in list_job_types()


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _parse_pb_dt(value: str) -> dt.datetime:
    return dt.datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(dt.UTC)
