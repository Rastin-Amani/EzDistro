"""Failure-engineering suite.

Every external dependency can fail. These tests pin the retry / backoff /
permanent-failure / recovery behavior for each provider, and simulate worker
crashes (kill during indexing, during section generation, before publishing
completion) followed by a restart — verifying no duplicated sections, no
duplicated WordPress posts, no corrupt article state, and full job recovery.

Failure matrix: docs/FAILURES.md
"""

import asyncio
import contextlib
from typing import Any

import httpx
import pytest

from app.jobs.engine import JobEngine
from app.jobs.handlers import ensure_registered, get_handler
from app.providers.base import PermanentError, TransientError, VectorPoint
from app.repositories.jobs import JobRepo
from app.repositories.topics import TopicRepo
from tests.fake_providers import FakeEmbedding, FakeLLM, FakePublisher, FakeRegistry
from tests.fakes import FakePocketBase, default_unique_fields
from tests.test_pipelines import _run, make_job, make_project


def transport_for(handler):
    return httpx.MockTransport(handler)


# ---------------------------------------------------------------------------
# Cohere embeddings: timeout / 429 / 500 / malformed response
# ---------------------------------------------------------------------------


def cohere_handler(scenario: str):
    def handler(request: httpx.Request) -> httpx.Response:
        if scenario == "timeout":
            raise httpx.ReadTimeout("read timeout", request=request)
        if scenario == "429":
            return httpx.Response(
                429, json={"message": "rate limited"}, headers={"retry-after": "1"}
            )
        if scenario == "500":
            return httpx.Response(500, json={"message": "boom"})
        # malformed: valid HTTP, wrong shape
        return httpx.Response(200, json={"unexpected": True})

    return handler


def make_cohere(scenario: str):
    from app.providers.embedding.cohere import CohereEmbedding

    return CohereEmbedding(
        base_url="https://api.cohere.com/v1",
        model="embed-v4.0",
        api_key="k",
        attempts=1,  # adapter-level retry off: job-level retry covers it
        transport=transport_for(cohere_handler(scenario)),
    )


@pytest.mark.asyncio
async def test_cohere_timeout_is_transient_retryable():
    with pytest.raises(TransientError):
        await make_cohere("timeout").embed_documents(["سلام"])


@pytest.mark.asyncio
async def test_cohere_429_is_transient_retryable():
    with pytest.raises(TransientError):
        await make_cohere("429").embed_documents(["سلام"])


@pytest.mark.asyncio
async def test_cohere_500_is_transient_retryable():
    with pytest.raises(TransientError):
        await make_cohere("500").embed_documents(["سلام"])


@pytest.mark.asyncio
async def test_cohere_malformed_response_is_permanent():
    with pytest.raises(PermanentError):
        await make_cohere("malformed").embed_documents(["سلام"])


# ---------------------------------------------------------------------------
# LLM: timeout / 429 / invalid JSON / truncated / empty
# ---------------------------------------------------------------------------


def llm_handler(scenario: str):
    def handler(request: httpx.Request) -> httpx.Response:
        if scenario == "timeout":
            raise httpx.ReadTimeout("read timeout", request=request)
        if scenario == "429":
            return httpx.Response(
                429, json={"error": {"message": "rate limited"}}, headers={"retry-after": "2"}
            )
        if scenario == "invalid-json":
            return httpx.Response(200, json={"choices": [{"message": {"content": "{not json"}}]})
        if scenario == "truncated":
            return httpx.Response(200, json={"choices": [{"message": {}}]})  # content missing
        if scenario == "empty":
            return httpx.Response(200, json={"choices": [{"message": {"content": "   "}}]})
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    return handler


def make_llm(scenario: str):
    from app.providers.llm.openai_compat import OpenAICompatLLM

    return OpenAICompatLLM(
        base_url="https://api.openai.com/v1",
        model="m",
        api_key="k",
        attempts=1,
        transport=transport_for(llm_handler(scenario)),
    )


@pytest.mark.asyncio
async def test_llm_timeout_is_transient_retryable():
    with pytest.raises(TransientError):
        await make_llm("timeout").generate(system=None, user="hi")


@pytest.mark.asyncio
async def test_llm_429_is_transient_retryable():
    with pytest.raises(TransientError):
        await make_llm("429").generate(system=None, user="hi")


@pytest.mark.asyncio
async def test_llm_invalid_json_is_retryable():
    """Unparseable JSON is a model-output error — the job retries (engine
    classifies a raw ValueError as transient), not a permanent config error."""
    with pytest.raises(ValueError):
        await make_llm("invalid-json").generate_json(system=None, user="json plz")


@pytest.mark.asyncio
async def test_llm_truncated_response_is_permanent():
    with pytest.raises(PermanentError):
        await make_llm("truncated").generate(system=None, user="hi")


@pytest.mark.asyncio
async def test_llm_empty_response_is_permanent():
    with pytest.raises(PermanentError):
        await make_llm("empty").generate(system=None, user="hi")


# ---------------------------------------------------------------------------
# Qdrant: timeout / connection failure / dimension mismatch
# ---------------------------------------------------------------------------


class FailingQdrant:
    def __init__(self, scenario: str) -> None:
        self.scenario = scenario

    async def collection_exists(self, *a, **kw) -> bool:
        if self.scenario == "dimension":
            raise ValueError("collection exists with 512 dimensions but model produces 1024")
        return False

    async def get_collection(self, *a, **kw):
        raise AssertionError("not used")

    async def create_collection(self, *a, **kw):
        return None

    async def upsert(self, *a, **kw):
        if self.scenario == "timeout":
            raise TimeoutError("qdrant read timeout")
        if self.scenario == "connection":
            raise httpx.ConnectError(
                "connection refused", request=httpx.Request("POST", "http://x")
            )
        return None

    async def close(self) -> None:
        pass


async def qdrant_store(scenario: str, monkeypatch: pytest.MonkeyPatch):
    from app.providers.vector.qdrant_store import QdrantStore

    store = QdrantStore(url="http://localhost:6333", namespace="ezdistro-test", api_key=None)

    async def fake_get_client():
        return FailingQdrant(scenario)

    monkeypatch.setattr(store, "_get_client", fake_get_client)
    return store


@pytest.mark.asyncio
async def test_qdrant_timeout_is_transient(monkeypatch):
    store = await qdrant_store("timeout", monkeypatch)
    with pytest.raises(asyncio.TimeoutError):
        await store.upsert([VectorPoint(id="1", vector=[0.1], payload={})])


@pytest.mark.asyncio
async def test_qdrant_connection_failure_is_transient(monkeypatch):
    store = await qdrant_store("connection", monkeypatch)
    with pytest.raises(httpx.ConnectError):
        await store.upsert([VectorPoint(id="1", vector=[0.1], payload={})])


@pytest.mark.asyncio
async def test_qdrant_dimension_mismatch_is_permanent(monkeypatch):
    """Dimension mismatch surfaces through ensure_collection → the indexing
    handler classifies it PermanentError: retrying can never help."""
    from app.providers.base import WPPost
    from app.providers.vector.qdrant_store import QdrantStore
    from app.services.indexing import handle_index_document

    class MismatchedStore:
        provider_name = "fake"

        async def ensure_collection(self, dimensions: int) -> None:
            raise ValueError("collection exists with 512 dimensions but model produces 1024")

    ensure_registered()
    pb = FakePocketBase(default_unique_fields())
    project, topic, article = make_article_setup(pb)
    registry = FakeRegistry()
    registry.publisher.posts = [
        WPPost(
            id=1, title="پ", content_html="<p>x</p>", link="https://s.test/?p=1", status="publish"
        )
    ]
    registry.vector = MismatchedStore()  # type: ignore[assignment]

    job = make_job(pb, project["id"], "index_document", {"sourceId": 1}, "qd-dim")
    from tests.failure_helpers import make_ctx

    with pytest.raises(PermanentError) as exc_info:
        await handle_index_document(make_ctx(pb, registry, job))
    assert "dimension mismatch" in str(exc_info.value)


# ---------------------------------------------------------------------------
# WordPress: timeout / 401 / duplicate post / invalid content
# ---------------------------------------------------------------------------


def wp_handler(scenario: str):
    def handler(request: httpx.Request) -> httpx.Response:
        if scenario == "timeout":
            raise httpx.ReadTimeout("read timeout", request=request)
        if scenario == "401":
            return httpx.Response(
                401,
                json={
                    "code": "rest_cannot_create",
                    "message": "Sorry, you are not allowed to create posts",
                },
            )
        if scenario == "invalid-content":
            return httpx.Response(
                400, json={"code": "rest_invalid_param", "message": "Invalid content."}
            )
        if scenario == "duplicate":
            # WP answered 201 with a post id that was never stored locally
            return httpx.Response(201, json={"id": 42, "link": "https://site.test/?p=42"})
        return httpx.Response(201, json={"id": 42, "link": "https://site.test/?p=42"})

    return handler


def make_wp(scenario: str):
    from app.providers.publish.wordpress import WordPressPublisher

    return WordPressPublisher(
        base_url="https://site.test",
        username="u",
        password="p",
        attempts=1,
        transport=transport_for(wp_handler(scenario)),
    )


@pytest.mark.asyncio
async def test_wp_timeout_is_transient_retryable():
    with pytest.raises(TransientError):
        await make_wp("timeout").create_post(title="t", html="<p>x</p>", status="draft", slug="t")


@pytest.mark.asyncio
async def test_wp_401_auth_error_is_permanent():
    with pytest.raises(PermanentError):
        await make_wp("401").create_post(title="t", html="<p>x</p>", status="draft", slug="t")


@pytest.mark.asyncio
async def test_wp_invalid_content_is_permanent():
    with pytest.raises(PermanentError):
        await make_wp("invalid-content").create_post(
            title="t", html="<p>x</p>", status="draft", slug="t"
        )


@pytest.mark.asyncio
async def test_wp_duplicate_post_recovery_updates_orphan():
    """Crash window: WP created the post, the worker died before storing the
    id. The retry finds it via ezdistro_article_id meta and UPDATEs it — never a
    second CREATE."""
    from app.services.publishing_service import handle_publish_article

    ensure_registered()
    pb = FakePocketBase(default_unique_fields())
    project, topic, article = make_article_setup(pb)
    registry = FakeRegistry()
    registry.publisher.crash_after_create = True  # dies after WP accepted the post

    job = make_job(pb, project["id"], "publish_article", {"articleId": article["id"]}, "pub-crash")
    from tests.failure_helpers import make_ctx

    ctx = make_ctx(pb, registry, job)
    with pytest.raises(SystemExit):
        await handle_publish_article(ctx)

    # orphan exists on WP; DB knows nothing about the post id
    assert len(registry.publisher.post_meta) == 1
    assert not pb.collection("articles").get_one(article["id"]).get("wordpressPostId")

    # worker restarts → retry → meta lookup finds the orphan → UPDATE
    registry.publisher.crash_after_create = False
    result = await _run(pb, registry, job)
    orphan_id = next(iter(registry.publisher.post_meta))
    assert result["postId"] == orphan_id
    assert len(registry.publisher.created) == 1  # exactly one CREATE ever
    assert len(registry.publisher.updated) == 1  # orphan updated in place
    article_after = pb.collection("articles").get_one(article["id"])
    assert article_after["status"] == "published"
    assert article_after["wordpressPostId"] == orphan_id


# ---------------------------------------------------------------------------
# PocketBase: temporary unavailability / stale job / worker crash
# ---------------------------------------------------------------------------


class FlakyPocketBase(FakePocketBase):
    """Fails the next N storage operations (read or write), then recovers.

    The failure is injected at the storage layer (where every collection
    operation lands), so ANY code path — claim, config load, handler — can be
    made to observe a dead PocketBase.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fail_ops = 0
        storage = self.storage
        original_create = storage.create
        original_update = storage.update
        original_records = storage.records
        original_get = storage.get

        def _maybe_fail() -> None:
            if self.fail_ops > 0:
                self.fail_ops -= 1
                raise ConnectionError("PocketBase is temporarily unavailable")

        def wrapped_create(name, data):
            _maybe_fail()
            return original_create(name, data)

        def wrapped_update(name, record_id, data):
            _maybe_fail()
            return original_update(name, record_id, data)

        def wrapped_records(name):
            _maybe_fail()
            return original_records(name)

        def wrapped_get(name, record_id):
            _maybe_fail()
            return original_get(name, record_id)

        storage.create = wrapped_create  # type: ignore[method-assign]
        storage.update = wrapped_update  # type: ignore[method-assign]
        storage.records = wrapped_records  # type: ignore[method-assign]
        storage.get = wrapped_get  # type: ignore[method-assign]


def make_engine(
    pb: FakePocketBase, registry: FakeRegistry, worker_id: str, max_jobs: int = 4
) -> JobEngine:
    engine = JobEngine(
        pb,
        worker_id=worker_id,
        lease_seconds=300,
        heartbeat_interval=60,
        max_concurrent_jobs=max_jobs,
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


@pytest.fixture(autouse=True)
def _restore_fake_llm_next():
    """Class-level FakeLLM._next patches must never leak into other modules."""
    original = FakeLLM._next
    yield
    FakeLLM._next = original


def syn_next(self: FakeLLM, json_mode: bool = False, user: str = "") -> str:
    self.calls.append({"json_mode": json_mode, "user": user})
    if "JSON" in user or "json" in user:
        sections = ",".join(f'{{"heading": "ب {s}", "content_brief": "خ"}}' for s in range(1, 4))
        return '{"title": "ت", "slug": "t", "sections": [' + sections + "]}"
    return "<p>" + ("کلمه محتوا " * 120).strip() + "</p>"


def make_article_setup(pb: FakePocketBase):
    project = make_project(pb)
    topic = TopicRepo(pb).create(project=project["id"], title="ت", keyword="ک")
    article = pb.collection("articles").create(
        {
            "project": project["id"],
            "topicId": topic["id"],
            "title": "عنوان مقاله",
            "slug": "onvan",
            "status": "approved",
            "finalHtml": "<h1>عنوان</h1><p>محتوا</p>",
            "metaDescription": "خلاصه",
            "wordCount": 10,
            "outlineVersion": 1,
        }
    )
    return project, topic, article


async def _engine_poll_until(pb: FakePocketBase, engine: JobEngine, max_polls: int = 3000) -> int:
    """Poll until no claimable jobs remain (surviving transient PB outages).
    Returns poll count."""
    polls = 0
    while polls < max_polls:
        with contextlib.suppress(ConnectionError, TimeoutError, OSError):
            await engine.poll_and_run()  # PB briefly unavailable — poll again
        await asyncio.sleep(0.01)
        polls += 1
        try:
            remaining = len(
                pb.collection("jobs").get_full_list(
                    {"filter": '(status="pending" || status="retrying" || status="running")'}
                )
            )
        except (ConnectionError, TimeoutError, OSError):
            continue
        if remaining == 0:
            break
    return polls


async def test_pb_temporary_unavailability_retries_job_until_success():
    """A PB outage during a job → transient failure → retry (with backoff) →
    completes once PB recovers. Never lost, never duplicated."""
    ensure_registered()
    pb = FlakyPocketBase(unique_fields=default_unique_fields())
    project = make_project(pb)
    topic = TopicRepo(pb).create(project=project["id"], title="ت", keyword="ک")
    registry = FakeRegistry()
    FakeLLM._next = syn_next  # type: ignore[method-assign]

    job = JobRepo(pb).create(
        project=project["id"],
        type="write_article",
        payload={"topicId": topic["id"]},
        idempotency_key="pb-outage",
        max_attempts=3,
        entity_type="article",
        entity_id=topic["id"],
    )
    # short deterministic backoff so retries land within the poll budget
    pb.collection("project_settings").update(
        pb.collection("project_settings").get_first_list_item(f'project="{project["id"]}"')["id"],
        {"retryPolicy": {"max_attempts": 3, "backoff_base": 2, "backoff_max": 60}},
    )
    # PocketBase goes down for the next 25 storage ops (mid-job outage)
    pb.fail_ops = 25
    engine = make_engine(pb, registry, "w-pb")
    polls = await _engine_poll_until(pb, engine, max_polls=1500)
    assert polls < 1500
    # outage fully consumed; the job retried and completed once PB recovered
    final_status = pb.collection("jobs").get_one(job["id"])["status"]
    assert final_status == "completed"


async def test_pb_outage_during_config_load_is_retryable():
    """Config load hitting a dead PB must NOT permanently fail the job."""
    ensure_registered()
    pb = FlakyPocketBase(unique_fields=default_unique_fields())
    project = make_project(pb)
    topic = TopicRepo(pb).create(project=project["id"], title="ت", keyword="ک")
    registry = FakeRegistry()
    FakeLLM._next = syn_next  # type: ignore[method-assign]
    job = JobRepo(pb).create(
        project=project["id"],
        type="write_article",
        payload={"topicId": topic["id"]},
        idempotency_key="pb-cfg",
        max_attempts=3,
        entity_type="article",
        entity_id=topic["id"],
    )
    pb.collection("project_settings").update(
        pb.collection("project_settings").get_first_list_item(f'project="{project["id"]}"')["id"],
        {"retryPolicy": {"max_attempts": 3, "backoff_base": 2, "backoff_max": 60}},
    )
    # dead PB exactly when the handler's config is loaded (after the claim)
    pb.fail_ops = 2
    engine = make_engine(pb, registry, "w-cfg")
    with pytest.raises(ConnectionError):
        await engine.poll_and_run()
    pb.fail_ops = 0  # disarm: the outage budget is exhausted
    status = pb.collection("jobs").get_one(job["id"])["status"]
    assert status in ("retrying", "pending", "running")  # NOT failed

    # PB recovers; the job completes
    polls = await _engine_poll_until(pb, engine, max_polls=1000)
    assert polls < 1000
    assert pb.collection("jobs").get_one(job["id"])["status"] == "completed"


async def test_stale_running_job_with_expired_lease_is_recovered():
    """A job left 'running' by a dead worker (lease expired) is reclaimed and
    completed by the next worker — never stuck, never double-executed."""
    ensure_registered()
    pb = FakePocketBase(default_unique_fields())
    project = make_project(pb)
    topic = TopicRepo(pb).create(project=project["id"], title="ت", keyword="ک")
    registry = FakeRegistry()
    registry.llm = FakeLLM([], delay=0.01)
    FakeLLM._next = syn_next  # type: ignore[method-assign]

    job = JobRepo(pb).create(
        project=project["id"],
        type="write_article",
        payload={"topicId": topic["id"]},
        idempotency_key="stale-1",
        max_attempts=3,
        entity_type="article",
        entity_id=topic["id"],
    )
    # worker A claims and dies: job stuck in running with a lease
    engine_a = make_engine(pb, registry, "worker-a")
    assert engine_a._claim(job) is True
    lease = pb.collection("job_leases").get_first_list_item(f'job="{job["id"]}"')
    # ...but that lease and the job's lock expire
    pb.collection("job_leases").update(lease["id"], {"expiresAt": "2020-01-01 00:00:00.000Z"})
    pb.collection("jobs").update(job["id"], {"leaseExpiresAt": "2020-01-01 00:00:00.000Z"})

    # worker B (restart) reclaims and completes the job
    engine_b = make_engine(pb, registry, "worker-b")
    await engine_b.poll_and_run()
    await asyncio.sleep(0.2)
    assert pb.collection("jobs").get_one(job["id"])["status"] == "completed"
    articles = pb.collection("articles").get_full_list()
    assert len(articles) == 1  # exactly one article — no duplicate


# ---------------------------------------------------------------------------
# Worker crash simulations (kill + restart)
# ---------------------------------------------------------------------------


class CrashSimulator:
    """Runs an engine loop and 'kills' it at a scripted point.

    The kill cancels every in-flight engine task — exactly what SIGKILL does:
    no cleanup, no finalize. Jobs stay 'running' with (or without) a lease and
    must be recovered by the restarted worker.
    """

    def __init__(self, pb: FakePocketBase, registry: FakeRegistry, worker_id: str):
        self.engine = make_engine(pb, registry, worker_id)
        self.engine.registry = registry
        self.pb = pb

    async def run_until(
        self, n_completed: int | None = None, stop_when: Any = None, max_polls: int = 3000
    ) -> None:
        """Poll until `n_completed` jobs are done (or `stop_when(pb, registry)`
        returns True), then SIGKILL every in-flight task."""
        baseline = set(asyncio.all_tasks())
        polls = 0
        while polls < max_polls:
            await self.engine.poll_and_run()
            await asyncio.sleep(0.01)
            polls += 1
            if stop_when is not None and stop_when(self.pb, self.engine.registry):
                break
            if n_completed is not None:
                done = len(
                    self.pb.collection("jobs").get_full_list({"filter": 'status="completed"'})
                )
                if done >= n_completed:
                    break
        await self._kill(baseline)

    async def run_to_completion(self, max_polls: int = 4000) -> int:
        polls = await _engine_poll_until(self.pb, self.engine, max_polls=max_polls)
        return polls

    async def _kill(self, baseline: set[asyncio.Task]) -> None:
        current = asyncio.current_task()
        victims = [t for t in asyncio.all_tasks() if t not in baseline and t is not current]
        for t in victims:
            t.cancel()
        if victims:
            await asyncio.gather(*victims, return_exceptions=True)
        # SIGKILL leaves running jobs mid-flight. In production their leases
        # expire (lease_seconds) and the next worker reclaims them — emulate
        # that: any job still "running" WITHOUT a live lease is stale.
        for job in self.pb.collection("jobs").get_full_list({"filter": 'status="running"'}):
            leases = self.pb.collection("job_leases").get_full_list(
                {"filter": f'job="{job["id"]}"'}
            )
            if not leases:
                self.pb.collection("jobs").update(
                    job["id"], {"leaseExpiresAt": "2020-01-01 00:00:00.000Z"}
                )


async def test_crash_during_indexing_no_duplicate_points():
    """Kill the worker mid index_run; the restarted worker resumes the SAME
    run record — deterministic point ids mean no duplicate vectors, and the
    run completes with every document counted exactly once."""
    from app.providers.base import WPPost

    ensure_registered()
    pb = FakePocketBase(default_unique_fields())
    project = make_project(pb)
    registry = FakeRegistry()
    registry.publisher = FakePublisher(
        posts=[
            WPPost(
                id=i + 1,
                title=f"پست {i}",
                content_html=("<p>محتوا</p>" * 40),
                link=f"https://site.test/?p={i + 1}",
                status="publish",
            )
            for i in range(30)
        ]
    )
    registry.embedding = FakeEmbedding(delay=0.02)  # 30 docs × 0.02s → spans many polls
    job = JobRepo(pb).create(
        project=project["id"],
        type="index_project",
        payload={"trigger": "manual"},
        idempotency_key="crash-index",
        max_attempts=3,
        entity_type="project",
        entity_id=project["id"],
    )

    def vectors_exist(pb_any, reg):
        return (
            len(reg.vector.points) >= 8
            and pb_any.collection("jobs").get_one(job["id"])["status"] == "running"
        )

    crash = CrashSimulator(pb, registry, "worker-a")
    await crash.run_until(stop_when=vectors_exist, max_polls=3000)
    assert len(registry.vector.points) >= 8  # crashed mid-run, some vectors written
    assert pb.collection("jobs").get_one(job["id"])["status"] == "running"  # run was NOT finished

    # restart: brand-new worker resumes the same run record from its checkpoint
    restart = CrashSimulator(pb, registry, "worker-b")
    polls = await restart.run_to_completion(max_polls=4000)
    assert polls < 4000

    run = pb.collection("index_runs").get_first_list_item(f'job="{job["id"]}"')
    assert run["status"] == "succeeded"
    # exactly 30 unique point ids — the pre-crash writes were never duplicated
    points = list(registry.vector.points.values())
    assert len(points) == 30
    assert len({p.id for p in points}) == 30


async def test_crash_during_section_generation_no_duplicate_sections():
    """Kill the worker while section jobs are in flight. The (article,
    position) unique constraint + deterministic section ids force updates —
    restarting regenerates the SAME rows, never new ones."""
    ensure_registered()
    pb = FakePocketBase(default_unique_fields())
    project, topic, _ = make_article_setup(pb)
    registry = FakeRegistry()
    registry.llm = FakeLLM([], delay=0.01)
    FakeLLM._next = syn_next  # type: ignore[method-assign]

    JobRepo(pb).create(
        project=project["id"],
        type="write_article",
        payload={"topicId": topic["id"]},
        idempotency_key="crash-sections",
        max_attempts=3,
        entity_type="article",
        entity_id=topic["id"],
    )
    crash = CrashSimulator(pb, registry, "worker-a")
    await crash.run_until(n_completed=1, max_polls=1500)
    # at least one section job was dispatched before the kill
    assert len(pb.collection("jobs").get_full_list({"filter": 'type="generate_section"'})) >= 1

    restart = CrashSimulator(pb, registry, "worker-b")
    polls = await restart.run_to_completion(max_polls=5000)
    assert polls < 5000

    sections = pb.collection("article_sections").get_full_list()
    assert len(sections) == 3  # exactly the outline's 3 sections — no extras
    assert len({s["position"] for s in sections}) == 3  # unique positions
    article = pb.collection("articles").get_one(
        pb.collection("articles").get_first_list_item(f'topicId="{topic["id"]}"')["id"]
    )
    assert article["status"] == "review"  # recovered, awaiting human review
    assert article.get("finalHtml")


async def test_crash_before_publish_completion_no_duplicate_posts():
    """The dangerous window: WP accepted the post, the worker died before the
    id was stored. Restart → retried publish finds the orphan via its
    ezdistro_article_id meta and UPDATEs it — exactly one post exists on WP."""
    from tests.failure_helpers import make_ctx

    ensure_registered()
    pb = FakePocketBase(default_unique_fields())
    project, topic, article = make_article_setup(pb)
    registry = FakeRegistry()
    registry.publisher.crash_after_create = True

    job = make_job(
        pb, project["id"], "publish_article", {"articleId": article["id"]}, "pub-crash-2"
    )
    from app.services.publishing_service import handle_publish_article

    ctx = make_ctx(pb, registry, job)
    with pytest.raises(SystemExit):
        await handle_publish_article(ctx)
    assert len(registry.publisher.post_meta) == 1  # orphan on WP
    assert not pb.collection("articles").get_one(article["id"]).get("wordpressPostId")

    registry.publisher.crash_after_create = False
    result = await _run(pb, registry, job)
    orphan_id = next(iter(registry.publisher.post_meta))
    assert result["postId"] == orphan_id
    assert len(registry.publisher.created) == 1  # never a second create
    assert len(registry.publisher.updated) == 1  # orphan updated in place
    article_after = pb.collection("articles").get_one(article["id"])
    assert article_after["status"] == "published"
    assert article_after["wordpressPostId"] == orphan_id
    published_runs = [
        r for r in pb.collection("publishing_runs").get_full_list() if r["status"] == "published"
    ]
    assert len(published_runs) == 1


async def test_full_batch_crash_and_restart_everything_recovers():
    """End-to-end: 3-article write batch, kill mid-way, restart with a new
    worker — every job recovers, sections are not duplicated, articles are
    not corrupt, and no assemble ran twice against the same version."""
    ensure_registered()
    pb = FakePocketBase(default_unique_fields())
    project = make_project(pb)
    pb.collection("project_settings").update(
        pb.collection("project_settings").get_first_list_item(f'project="{project["id"]}"')["id"],
        {"retryPolicy": {"max_attempts": 3, "backoff_base": 2, "backoff_max": 60}},
    )
    topics = [
        TopicRepo(pb).create(project=project["id"], title=f"ت {i}", keyword=f"k{i}")
        for i in range(3)
    ]
    registry = FakeRegistry()
    registry.llm = FakeLLM([], delay=0.01)
    FakeLLM._next = syn_next  # type: ignore[method-assign]

    for i, t in enumerate(topics):
        JobRepo(pb).create(
            project=project["id"],
            type="write_article",
            payload={"topicId": t["id"]},
            idempotency_key=f"batch-{i}",
            max_attempts=3,
            entity_type="article",
            entity_id=t["id"],
        )

    crash = CrashSimulator(pb, registry, "worker-a")
    await crash.run_until(n_completed=2, max_polls=2000)

    restart = CrashSimulator(pb, registry, "worker-b")
    polls = await restart.run_to_completion(max_polls=4000)
    assert polls < 4000

    jobs = pb.collection("jobs").get_full_list()
    assert all(j["status"] == "completed" for j in jobs), [j["status"] for j in jobs]

    sections = pb.collection("article_sections").get_full_list()
    assert len(sections) == 9  # 3 articles × 3 sections
    assert len({(s["article"], s["position"]) for s in sections}) == 9  # no duplicates

    articles = pb.collection("articles").get_full_list()
    assert len(articles) == 3
    assert all(a["status"] == "review" and a.get("finalHtml") for a in articles)

    assembles = [j for j in jobs if j["type"] == "assemble_article"]
    assert len(assembles) == 3
