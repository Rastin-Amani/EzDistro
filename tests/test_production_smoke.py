"""Production smoke test — the full happy path through the real engine:
index → write → assemble → publish, plus the documented restart/recovery
guarantees (re-run of the same batch after a simulated worker restart)."""

from __future__ import annotations

import asyncio

import pytest

from app.jobs.engine import JobEngine
from app.jobs.handlers import ensure_registered, get_handler
from app.repositories.jobs import JobRepo
from app.repositories.projects import ProjectRepo
from app.repositories.topics import TopicRepo
from tests.fake_providers import FakeLLM, FakeRegistry
from tests.fakes import FakePocketBase, default_unique_fields


@pytest.fixture(autouse=True)
def _restore_fake_llm_next():
    """Class-level FakeLLM._next patches must never leak into other modules."""
    original = FakeLLM._next
    yield
    FakeLLM._next = original


def syn_next(self: FakeLLM, json_mode: bool = False, user: str = "") -> str:
    if "JSON" in user or "json" in user:
        sections = ",".join(f'{{"heading": "B {i}", "content_brief": "X"}}' for i in range(1, 4))
        return '{"title": "article DMand", "slug": "demo-post", "sections": [' + sections + "]}"
    return "<p>" + ("content word " * 150).strip() + "</p>"


def make_engine(pb: FakePocketBase, registry: FakeRegistry, worker_id: str) -> JobEngine:
    engine = JobEngine(
        pb,
        worker_id=worker_id,
        lease_seconds=300,
        heartbeat_interval=60,
        max_concurrent_jobs=4,
        llm_concurrency=2,
        embedding_concurrency=2,
        publish_concurrency=1,
        handlers={
            t: get_handler(t)
            for t in (
                "index_project",
                "write_article",
                "generate_section",
                "assemble_article",
                "publish_article",
            )
        },
    )
    engine.registry = registry
    return engine


async def _run_to_done(pb: FakePocketBase, engine: JobEngine, max_polls: int = 6000) -> int:
    polls = 0
    while polls < max_polls:
        await engine.poll_and_run()
        await asyncio.sleep(0.01)
        polls += 1
        remaining = len(
            pb.collection("jobs").get_full_list(
                {"filter": '(status="pending" || status="retrying" || status="running")'}
            )
        )
        if remaining == 0:
            break
    return polls


def _prepare() -> tuple[FakePocketBase, FakeRegistry]:
    ensure_registered()
    pb = FakePocketBase(default_unique_fields())
    project = ProjectRepo(pb).create(name="Demo", slug="demo")
    pb.collection("project_settings").create({"project": project["id"]})
    pb.collection("project_settings").update(
        pb.collection("project_settings").get_first_list_item(f'project="{project["id"]}"')["id"],
        {
            "retryPolicy": {"max_attempts": 3, "backoff_base": 2, "backoff_max": 60},
            "publishingMode": "publish",
        },
    )
    registry = FakeRegistry()
    registry.llm = FakeLLM([], delay=0.005)
    FakeLLM._next = syn_next  # type: ignore[method-assign]
    return pb, registry


def test_sample_indexing_generation_publishing_succeeds():
    """The full production happy path: index → write → assemble → publish."""
    import pytest

    from app.providers.base import WPPost
    from tests.fake_providers import FakeEmbedding, FakePublisher

    pb, registry = _prepare()
    registry.publisher = FakePublisher(
        posts=[
            WPPost(
                id=i + 1,
                title=f"source {i}",
                content_html="<p>" + ("content for indexing " * 40) + "</p>",
                link=f"https://s/?p={i + 1}",
                status="publish",
            )
            for i in range(3)
        ]
    )
    registry.embedding = FakeEmbedding()

    async def run():
        JobRepo(pb).create(
            project=pb.collection("projects").get_full_list()[0]["id"],
            type="index_project",
            payload={"trigger": "manual", "full": True},
            idempotency_key="smoke-index",
            max_attempts=3,
            entity_type="project",
            entity_id=pb.collection("projects").get_full_list()[0]["id"],
        )
        project = pb.collection("projects").get_full_list()[0]
        topic = TopicRepo(pb).create(
            project=project["id"],
            title="Topic DMand",
            keyword="word",
        )
        JobRepo(pb).create(
            project=project["id"],
            type="write_article",
            payload={"topicId": topic["id"]},
            idempotency_key="smoke-write",
            max_attempts=3,
            entity_type="topic",
            entity_id=topic["id"],
        )
        engine = make_engine(pb, registry, "smoke-a")
        assert await _run_to_done(pb, engine) < 6000

        # --- assertions: indexing ---
        run_row = pb.collection("index_runs").get_full_list()[0]
        assert run_row["status"] == "succeeded"
        assert len(registry.vector.points) == 3

        # --- generation ---
        article = pb.collection("articles").get_full_list()[0]
        assert article["status"] == "review"
        assert article.get("wordCount") >= 300
        assert len(pb.collection("article_sections").get_full_list()) == 3
        for job in pb.collection("jobs").get_full_list():
            assert job["status"] == "completed", job.get("errorMessage")

        # --- publishing ---
        pb.collection("articles").update(article["id"], {"status": "approved"})
        JobRepo(pb).create(
            project=article["project"],
            type="publish_article",
            payload={"articleId": article["id"], "action": "publish"},
            idempotency_key="smoke-pub",
            max_attempts=3,
            entity_type="article",
            entity_id=article["id"],
        )
        await _run_to_done(pb, engine)
        article_after = pb.collection("articles").get_one(article["id"])
        assert article_after["status"] == "published"
        assert article_after.get("wordpressPostId")
        runs = pb.collection("publishing_runs").get_full_list()
        assert [r["status"] for r in runs] == ["published"]

    asyncio.run(run())


def test_sample_batch_survives_worker_restart():
    """Same batch after a simulated worker kill + restart: every job completes,
    exactly 3 articles and 9 sections, no duplicates (recovery guarantees)."""
    from tests.test_failure_engineering import CrashSimulator

    pb, registry = _prepare()
    project = pb.collection("projects").get_full_list()[0]
    topics = [
        TopicRepo(pb).create(project=project["id"], title=f"T {i}", keyword=f"k{i}")
        for i in range(3)
    ]
    for i, topic in enumerate(topics):
        JobRepo(pb).create(
            project=project["id"],
            type="write_article",
            payload={"topicId": topic["id"]},
            idempotency_key=f"batch-{i}",
            max_attempts=3,
            entity_type="topic",
            entity_id=topic["id"],
        )

    async def run():
        crash = CrashSimulator(pb, registry, "worker-a")
        await crash.run_until(n_completed=2, max_polls=2000)
        restart = CrashSimulator(pb, registry, "worker-b")
        polls = await restart.run_to_completion(max_polls=5000)
        assert polls < 5000
        for job in pb.collection("jobs").get_full_list():
            assert job["status"] == "completed", job.get("errorMessage")
        sections = pb.collection("article_sections").get_full_list()
        assert len(sections) == 9
        assert len({(s["article"], s["position"]) for s in sections}) == 9
        articles = pb.collection("articles").get_full_list()
        assert len(articles) == 3
        assert all(a["status"] == "review" and a.get("finalHtml") for a in articles)

    asyncio.run(run())
