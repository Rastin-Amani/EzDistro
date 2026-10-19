"""No artifact may sit in an in-progress state without a live job.

Three layers, one guarantee:

* an *enhancement* stage (metadata, QA) can never fail the article;
* a job that fails permanently settles its artifact to `failed`;
* the reaper resumes (or parks) an artifact whose job is simply gone.

The first two are what a stuck article is made of: a fatal error from a
nice-to-have LLM call, and a failure that left the article at "generating".
"""

from __future__ import annotations

import pytest

from app.jobs.engine import JobEngine
from app.providers.base import PermanentError
from app.providers.llm.openai_compat import OpenAICompatLLM
from app.repositories.jobs import JobRepo
from app.services.reaper import reconcile_stuck
from tests.helpers import make_article, make_job, make_pb, make_project, make_topic


def _age(pb, collection: str, record_id: str) -> None:
    """Make a row look abandoned — the reaper only touches stale artifacts."""
    pb.collection(collection).update(record_id, {"updated": "2020-01-01 00:00:00.000Z"})


def _section_status(pb, article_id: str, status: str) -> None:
    for row in pb.collection("article_sections").get_full_list(
        query_params={"filter": f'article="{article_id}"'}
    ):
        pb.collection("article_sections").update(row["id"], {"status": status})


# ---------------------------------------------------------------------------
# The reaper
# ---------------------------------------------------------------------------
def test_reaper_requeues_an_abandoned_assembly():
    pb = make_pb()
    project = make_project(pb)
    topic = make_topic(pb, project["id"], status="writing")
    article = make_article(pb, project["id"], status="generating", topic_id=topic["id"])
    _age(pb, "articles", article["id"])
    _age(pb, "topics", topic["id"])

    actions = reconcile_stuck(pb)

    assert actions.get("assemble_requeued") == 1
    job = JobRepo(pb).first(
        filter=f'type="assemble_article" && entityId="{article["id"]}" && status="pending"'
    )
    assert job is not None
    assert job["payload"]["articleId"] == article["id"]
    # a recap never auto-publishes on its own
    assert job["payload"]["trigger"] == ""
    assert pb.collection("articles").get_one(article["id"])["status"] == "generating"


def test_reaper_parks_an_article_with_nothing_to_assemble():
    pb = make_pb()
    project = make_project(pb)
    topic = make_topic(pb, project["id"], status="writing")
    article = make_article(pb, project["id"], status="generating", topic_id=topic["id"])
    _section_status(pb, article["id"], "pending")  # no finished work to assemble
    _age(pb, "articles", article["id"])
    _age(pb, "topics", topic["id"])

    actions = reconcile_stuck(pb)

    assert actions.get("article_parked") == 1
    assert pb.collection("articles").get_one(article["id"])["status"] == "failed"
    assert pb.collection("topics").get_one(topic["id"])["status"] == "failed"


def test_reaper_requeues_an_abandoned_publish():
    pb = make_pb()
    project = make_project(pb)
    topic = make_topic(pb, project["id"], status="review")
    article = make_article(pb, project["id"], status="publishing", topic_id=topic["id"])
    _age(pb, "articles", article["id"])

    actions = reconcile_stuck(pb)

    assert actions.get("publish_requeued") == 1
    assert pb.collection("articles").get_one(article["id"])["status"] == "approved"
    job = JobRepo(pb).first(
        filter=f'type="publish_article" && entityId="{article["id"]}" && status="pending"'
    )
    assert job is not None and job["payload"]["action"] == "publish"


def test_reaper_leaves_a_live_pipeline_alone():
    """The initial write job points at the topic, not the article yet."""
    pb = make_pb()
    project = make_project(pb)
    topic = make_topic(pb, project["id"], status="writing")
    article = make_article(pb, project["id"], status="generating", topic_id=topic["id"])
    make_job(pb, project["id"], job_type="write_article", payload={"topicId": topic["id"]})
    _age(pb, "articles", article["id"])
    _age(pb, "topics", topic["id"])

    actions = reconcile_stuck(pb)

    assert actions == {}
    assert pb.collection("articles").get_one(article["id"])["status"] == "generating"
    assert pb.collection("topics").get_one(topic["id"])["status"] == "writing"


def test_reaper_replans_a_topic_whose_job_vanished():
    pb = make_pb()
    project = make_project(pb)
    topic = make_topic(pb, project["id"], status="writing")
    _age(pb, "topics", topic["id"])

    actions = reconcile_stuck(pb)

    assert actions.get("topic_replanned") == 1
    assert pb.collection("topics").get_one(topic["id"])["status"] == "planned"


def test_reaper_never_rewrites_a_topic_with_a_live_article():
    """Replanning here would let the write schedule make a second article."""
    pb = make_pb()
    project = make_project(pb)
    topic = make_topic(pb, project["id"], status="writing")
    article = make_article(pb, project["id"], status="generating", topic_id=topic["id"])
    _age(pb, "topics", topic["id"])
    _age(pb, "articles", article["id"])

    actions = reconcile_stuck(pb)

    assert actions.get("topic_replanned") is None
    assert pb.collection("topics").get_one(topic["id"])["status"] == "writing"


def test_reaper_recovers_at_most_twice_then_parks():
    pb = make_pb()
    project = make_project(pb)
    topic = make_topic(pb, project["id"], status="writing")
    article = make_article(pb, project["id"], status="generating", topic_id=topic["id"])
    _age(pb, "articles", article["id"])
    _age(pb, "topics", topic["id"])
    for window in (1, 2):  # two earlier recoveries, both finished
        job = JobRepo(pb).create(
            project=project["id"],
            type="assemble_article",
            payload={"articleId": article["id"]},
            idempotency_key=f"reap:assemble:{article['id']}:{window}",
            entity_type="article",
            entity_id=article["id"],
        )
        pb.collection("jobs").update(job["id"], {"status": "completed"})

    actions = reconcile_stuck(pb)

    assert actions.get("article_parked") == 1
    assert pb.collection("articles").get_one(article["id"])["status"] == "failed"


# ---------------------------------------------------------------------------
# The engine settles its artifact
# ---------------------------------------------------------------------------
def test_permanent_failure_settles_article_and_topic():
    pb = make_pb()
    project = make_project(pb)
    topic = make_topic(pb, project["id"], status="writing")
    article = make_article(pb, project["id"], status="generating", topic_id=topic["id"])
    engine = JobEngine(
        pb,
        worker_id="w1",
        lease_seconds=300,
        heartbeat_interval=60.0,
        max_concurrent_jobs=8,
        handlers={},
    )
    job = JobRepo(pb).create(
        project=project["id"],
        type="assemble_article",
        payload={"articleId": article["id"]},
        idempotency_key="assemble:x",
        entity_type="article",
        entity_id=article["id"],
    )

    engine._finalize_failure(job["id"], "ProviderError", "boom", retryable=False)

    assert pb.collection("articles").get_one(article["id"])["status"] == "failed"
    assert pb.collection("topics").get_one(topic["id"])["status"] == "failed"


def test_retryable_failure_leaves_the_artifact_alone():
    pb = make_pb()
    project = make_project(pb)
    topic = make_topic(pb, project["id"], status="writing")
    article = make_article(pb, project["id"], status="generating", topic_id=topic["id"])
    engine = JobEngine(
        pb,
        worker_id="w1",
        lease_seconds=300,
        heartbeat_interval=60.0,
        max_concurrent_jobs=8,
        handlers={},
    )
    job = JobRepo(pb).create(
        project=project["id"],
        type="assemble_article",
        payload={"articleId": article["id"]},
        idempotency_key="assemble:y",
        max_attempts=3,
        entity_type="article",
        entity_id=article["id"],
    )

    engine._finalize_failure(job["id"], "TransientError", "later", retryable=True)

    assert pb.collection("articles").get_one(article["id"])["status"] == "generating"
    assert pb.collection("topics").get_one(topic["id"])["status"] == "writing"


# ---------------------------------------------------------------------------
# The LLM gives a reasoning model more room before giving up
# ---------------------------------------------------------------------------
class _StubLLM(OpenAICompatLLM):
    def __init__(self, bodies: list[dict]) -> None:  # noqa: D107
        self._model = "reasoner"
        self.timeout = 30.0
        self._attempts = 1
        self._bodies = list(bodies)
        self.seen_max_tokens: list[int] = []

    async def _request(self, body, retry_counter):  # type: ignore[override]
        self.seen_max_tokens.append(int(body.get("max_tokens") or 0))
        return _StubResponse(self._bodies.pop(0))

    async def _observed(self, operation, prompt_len, fn, retry_counter):  # type: ignore[override]
        return await fn()


class _StubResponse:
    def __init__(self, data: dict) -> None:
        self._data = data

    def json(self) -> dict:
        return self._data

    status_code = 200
    is_success = True
    text = ""


def _empty_length_response() -> dict:
    return {"choices": [{"message": {"content": ""}, "finish_reason": "length"}]}


def _answer(text: str) -> dict:
    return {
        "choices": [{"message": {"content": text}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1},
    }


def test_llm_doubles_the_budget_instead_of_failing():
    llm = _StubLLM([_empty_length_response(), _answer("done")])
    from app.providers.base import GenerationParams

    result = __import__("asyncio").run(
        llm.generate(system=None, user="hi", params=GenerationParams(max_tokens=16000))
    )

    assert result.text == "done"
    assert llm.seen_max_tokens == [16000, 32000]


def test_llm_stops_at_the_ceiling():
    llm = _StubLLM([_empty_length_response(), _empty_length_response(), _empty_length_response()])
    from app.providers.base import GenerationParams

    with pytest.raises(PermanentError):
        __import__("asyncio").run(
            llm.generate(system=None, user="hi", params=GenerationParams(max_tokens=64000))
        )

    assert llm.seen_max_tokens == [64000]  # at the ceiling: nothing left to raise


def test_qa_and_metadata_never_fail_the_article():
    """A provider blow-up in a nice-to-have stage must not fail the article."""
    import asyncio

    from app.jobs.context import JobContext, ProviderStack
    from app.repositories.jobs import JobEventRepo
    from app.repositories.prompts import PromptRepo
    from app.services.settings import ProjectConfig
    from app.services.writing import _generate_metadata, _qa_and_repair
    from tests.fake_providers import FakeLLM, FakeRegistry

    class _Boom(FakeLLM):
        def __init__(self) -> None:
            super().__init__([])

        async def generate(self, **_kwargs):  # type: ignore[override]
            raise PermanentError("budget gone", retryable=False)

    pb = make_pb()
    project = make_project(pb)
    for ptype in ("metadata_user", "article_qa_user"):
        PromptRepo(pb).save_version(
            project_id=project["id"], ptype=ptype, name="default", content="Do the thing."
        )
    job = make_job(pb, project["id"], job_type="assemble_article")
    registry = FakeRegistry()
    registry.llm = _Boom()
    config = ProjectConfig.load(pb, project["id"])
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

    meta = asyncio.run(_generate_metadata(ctx, None, "T", "t", "<p>b</p>", "kw"))
    html, qa = asyncio.run(
        _qa_and_repair(ctx, None, {}, [], 300, "kw", "T", "t", "m", "<p>keep me</p>")
    )

    assert meta == {}
    assert html == "<p>keep me</p>"  # the validated article survives untouched
    assert qa["ran"] is False
    assert "PermanentError" in qa["error"]
