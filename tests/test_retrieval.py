"""Retrieval subsystem tests: RetrievalService, InternalLinkingService,
ContextBuilder, and writer integration (prompt gets only relevant context)."""

from __future__ import annotations

import asyncio
from typing import Any

from app.providers.base import VectorPoint
from app.repositories.jobs import JobEventRepo, JobRepo, now_utc
from app.repositories.projects import DEFAULT_SETTINGS
from app.repositories.prompts import PromptRepo
from app.schemas.retrieval import RetrievalOptions, RetrievalResult
from app.services.internal_linking import ContextBuilder, InternalLinkingService
from app.services.retrieval import RetrievalService, normalize_query
from app.services.settings import ProjectConfig
from tests.fake_providers import FakeRegistry
from tests.fakes import FakePocketBase, default_unique_fields


def make_project(pb: FakePocketBase) -> dict[str, Any]:
    project = pb.collection("projects").create(
        {
            "name": "\u067e\u0631\u0648\u0698\u0647",
            "slug": "proj-a",
            "language": "fa",
            "status": "active",
            "timezone": "Asia/Tehran",
        }
    )
    pb.collection("project_settings").create({"project": project["id"], **DEFAULT_SETTINGS})
    return project


def seed_points(registry: FakeRegistry, project_id: str, n: int = 5) -> None:
    for i in range(1, n + 1):
        registry.vector.points[f"proj-a:{i}:0"] = VectorPoint(
            id=f"proj-a:{i}:0",
            vector=[0.1 * i] * 8,
            payload={
                "project_id": project_id,
                "document_id": f"doc-{i}",
                "source_id": str(i),
                "source_url": f"https://site.test/{i}",
                "title": f"\u0645\u0642\u0627\u0644\u0647 {i}",
                "chunk_index": 0,
                "content_hash": f"hash-{i}",
                "language": "fa",
                "chunk_text": f"\u0645\u062a\u0646 \u0645\u0631\u062a\u0628\u0637 \u0645\u0642\u0627\u0644\u0647 {i} "
                * 20,
            },
        )


def make_config(pb: FakePocketBase, project: dict[str, Any]) -> ProjectConfig:
    return ProjectConfig.load(pb, project["id"])


# ---------------------------------------------------------------------------
# normalize_query
# ---------------------------------------------------------------------------
def test_normalize_query():
    assert (
        normalize_query("  <p>\u0633\u0626\u0648</p>  \u0648  \u0633\u0626\u0648  ")
        == "\u0633\u0626\u0648 \u0648 \u0633\u0626\u0648"
    )
    assert normalize_query("") == ""
    assert normalize_query("   ") == ""


# ---------------------------------------------------------------------------
# RetrievalService
# ---------------------------------------------------------------------------
def test_retrieve_defaults_and_filters():
    pb = FakePocketBase(default_unique_fields())
    project = make_project(pb)
    registry = FakeRegistry()
    seed_points(registry, project["id"])
    config = make_config(pb, project)
    service = RetrievalService(pb, registry)

    results = asyncio.run(
        service.retrieve(
            config,
            "\u0633\u0626\u0648",
            RetrievalOptions(candidate_count=20, similarity_threshold=None),
        )
    )

    assert len(results) == 5
    # normalized result contract
    first = results[0]
    assert first.document_id.startswith("doc-")
    assert first.title
    assert first.source_url.startswith("https://site.test/")
    assert first.snippet
    assert first.vector_score > 0
    assert first.rerank_score is None
    assert first.final_score == first.vector_score  # no rerank → vector score
    assert first.metadata["project_id"] == project["id"]
    # embed_queries used for queries
    assert registry.embedding.requests[0][0] == "queries"


def test_retrieve_passes_project_filter_and_threshold():
    pb = FakePocketBase(default_unique_fields())
    project = make_project(pb)
    registry = FakeRegistry()
    seed_points(registry, project["id"])
    # foreign project point must never appear
    registry.vector.points["other:1:0"] = VectorPoint(
        id="other:1:0",
        vector=[0.99] * 8,
        payload={
            "project_id": "OTHER",
            "document_id": "x",
            "title": "\u062e\u0627\u0631\u062c\u06cc",
            "source_url": "https://x.test",
            "chunk_text": "\u062e\u0627\u0631\u062c\u06cc",
        },
    )
    config = make_config(pb, project)
    service = RetrievalService(pb, registry)

    results = asyncio.run(
        service.retrieve(
            config,
            "\u0633\u0626\u0648",
            RetrievalOptions(candidate_count=3, similarity_threshold=0.5),
        )
    )
    assert all(r.metadata["project_id"] == project["id"] for r in results)
    assert len(results) <= 3
    assert all(r.vector_score >= 0.5 for r in results)


def test_retrieve_with_rerank_uses_larger_pool_and_sets_scores():
    pb = FakePocketBase(default_unique_fields())
    project = make_project(pb)
    registry = FakeRegistry()
    seed_points(registry, project["id"], n=6)
    config = make_config(pb, project)
    service = RetrievalService(pb, registry)

    results = asyncio.run(
        service.retrieve(
            config,
            "\u0633\u0626\u0648",
            RetrievalOptions(candidate_count=6, rerank=True, rerank_top_n=3),
        )
    )
    assert len(results) == 3  # top N after rerank
    for r in results:
        assert r.rerank_score is not None
        assert r.final_score == r.rerank_score
        assert r.vector_score > 0  # original score preserved
    assert registry.reranker.calls == 1  # reranker invoked
    # rerank received the full candidate pool (6 documents)
    assert registry.reranker is not None


def test_retrieve_rerank_falls_back_when_no_reranker():
    pb = FakePocketBase(default_unique_fields())
    project = make_project(pb)
    registry = FakeRegistry()
    registry.reranker = None  # reranking NOT configured
    seed_points(registry, project["id"])
    config = make_config(pb, project)
    service = RetrievalService(pb, registry)

    results = asyncio.run(
        service.retrieve(
            config, "\u0633\u0626\u0648", RetrievalOptions(rerank=True, rerank_top_n=2)
        )
    )
    assert len(results) == 5  # vector order stands — reranking never mandatory
    assert all(r.rerank_score is None for r in results)


def test_retrieve_empty_query_or_no_results():
    pb = FakePocketBase(default_unique_fields())
    project = make_project(pb)
    registry = FakeRegistry()
    config = make_config(pb, project)
    service = RetrievalService(pb, registry)

    assert asyncio.run(service.retrieve(config, "  ")) == []
    seed_points(registry, project["id"])
    # threshold above all scores → empty
    results = asyncio.run(
        service.retrieve(config, "\u0633\u0626\u0648", RetrievalOptions(similarity_threshold=0.99))
    )
    assert results == []


# ---------------------------------------------------------------------------
# InternalLinkingService
# ---------------------------------------------------------------------------
def _results(**overrides) -> list[RetrievalResult]:
    base = RetrievalResult(
        document_id="d",
        title="\u0645\u0642\u0627\u0644\u0647",
        source_url="https://site.test/1",
        snippet="\u0645\u062a\u0646",
        vector_score=0.5,
        final_score=0.5,
    )
    return [RetrievalResult(**{**base.__dict__, **kw}) for kw in overrides.get("items", [])]


def test_internal_linking_filters_and_ranks():
    results = [
        RetrievalResult(
            document_id="1",
            title="\u0627\u0644\u0641",
            source_url="https://s.test/a",
            snippet="x",
            vector_score=0.9,
            final_score=0.9,
        ),
        RetrievalResult(
            document_id="2",
            title="\u0628",
            source_url="https://s.test/b",
            snippet="x",
            vector_score=0.7,
            final_score=0.7,
        ),
        RetrievalResult(
            document_id="3",
            title="",
            source_url="https://s.test/c",
            snippet="x",
            vector_score=0.8,
            final_score=0.8,
        ),  # no title → dropped
        RetrievalResult(
            document_id="4",
            title="\u062f",
            source_url="",
            snippet="x",
            vector_score=0.8,
            final_score=0.8,
        ),  # no url → dropped
        RetrievalResult(
            document_id="5",
            title="\u06a9\u0645",
            source_url="https://s.test/low",
            snippet="x",
            vector_score=0.1,
            final_score=0.1,
        ),  # below min score
    ]
    links = InternalLinkingService().build(results, max_links=5, min_score=0.5)
    assert [link.title for link in links] == ["\u0627\u0644\u0641", "\u0628"]


def test_internal_linking_removes_current_article_and_dedupes():
    results = [
        RetrievalResult(
            document_id="1",
            title="\u062e\u0648\u062f\u0645\u0627\u0646",
            source_url="https://s.test/current",
            snippet="x",
            vector_score=0.99,
            final_score=0.99,
        ),
        RetrievalResult(
            document_id="2",
            title="\u062a\u06a9\u0631\u0627\u0631\u06cc",
            source_url="https://s.test/dup",
            snippet="x",
            vector_score=0.7,
            final_score=0.7,
        ),
        RetrievalResult(
            document_id="3",
            title="\u062a\u06a9\u0631\u0627\u0631\u06cc",
            source_url="https://s.test/dup/",
            snippet="x",
            vector_score=0.95,
            final_score=0.95,
        ),  # same URL (trailing slash)
        RetrievalResult(
            document_id="4",
            title="\u0645\u0642\u0627\u0644\u0647 \u062f\u06cc\u06af\u0631",
            source_url="https://s.test/other",
            snippet="x",
            vector_score=0.6,
            final_score=0.6,
        ),
    ]
    links = InternalLinkingService().build(
        results, current_url="https://s.test/current", max_links=5
    )
    titles = [link.title for link in links]
    assert "\u062e\u0648\u062f\u0645\u0627\u0646" not in titles  # current article removed
    assert titles.count("\u062a\u06a9\u0631\u0627\u0631\u06cc") == 1  # deduped, highest score kept
    dup = next(link for link in links if link.title == "\u062a\u06a9\u0631\u0627\u0631\u06cc")
    assert dup.relevance_score == 0.95


def test_internal_linking_caps_max_links():
    results = [
        RetrievalResult(
            document_id=str(i),
            title=f"\u062a{i}",
            source_url=f"https://s.test/{i}",
            snippet="x",
            vector_score=(10 - i) / 10,
            final_score=(10 - i) / 10,
        )
        for i in range(1, 8)
    ]
    links = InternalLinkingService().build(results, max_links=3)
    assert len(links) == 3
    assert links[0].relevance_score > links[-1].relevance_score  # ranked desc


# ---------------------------------------------------------------------------
# ContextBuilder
# ---------------------------------------------------------------------------
def test_context_builder_respects_budget():
    results = [
        RetrievalResult(
            document_id=str(i),
            title=f"\u062a{i}",
            source_url=f"https://s.test/{i}",
            snippet=("\u06a9\u0644\u0645\u0647 " * 300).strip(),
            vector_score=1.0,
            final_score=1.0,
        )
        for i in range(6)
    ]
    builder = ContextBuilder(max_links=2, max_passages=2, max_chars=800)
    context = builder.build("\u0633\u0626\u0648", results, current_title="")

    assert len(context.links) == 2
    assert len(context.passages) == 2
    assert context.used_chars <= 800  # char budget respected
    assert all(len(p) <= 400 for p in context.passages)  # per-passage share

    formatted = builder.format_for_prompt(context)
    assert (
        "\u0645\u0642\u0627\u0644\u0627\u062a \u0645\u0648\u062c\u0648\u062f \u062f\u0631 \u0633\u0627\u06cc\u062a"
        in formatted
    )
    assert "\u0628\u062e\u0634\u200c\u0647\u0627\u06cc \u0645\u0631\u062a\u0628\u0637" in formatted
    assert len(formatted) <= 800 + 200  # formatting overhead is small


def test_context_builder_metadata_rules_and_empty():
    builder = ContextBuilder(max_links=0, max_passages=0, max_chars=0)
    context = builder.build("\u0633\u0626\u0648", [], current_title="")
    assert context.links == []
    assert context.passages == []
    assert context.used_chars == 0


# ---------------------------------------------------------------------------
# Writer integration — prompt receives only relevant context
# ---------------------------------------------------------------------------
def test_writer_outline_prompt_contains_budgeted_context():
    from app.jobs.context import JobContext, ProviderStack
    from app.repositories.topics import TopicRepo
    from app.services.writing import handle_write_article

    pb = FakePocketBase(default_unique_fields())
    project = make_project(pb)
    for ptype, content in (
        (
            "brand_voice",
            "\u062a\u0648 \u0646\u0648\u06cc\u0633\u0646\u062f\u0647 \u0633\u0626\u0648 \u0647\u0633\u062a\u06cc.",
        ),
        # variable-driven prompt: retrieval data injected via {{ }} tokens
        (
            "outline_user",
            "JSON \u0628\u0631\u06af\u0631\u062f\u0627\u0646.\n\u0645\u0648\u0636\u0648\u0639: {{ topic.title }}\n\u0645\u0642\u0627\u0644\u0627\u062a \u0645\u0648\u062c\u0648\u062f \u062f\u0631 \u0633\u0627\u06cc\u062a:\n{{ internal_links }}\n\u0632\u0645\u06cc\u0646\u0647:\n{{ retrieved_context }}",
        ),
        ("section_user", "HTML \u0628\u0631\u06af\u0631\u062f\u0627\u0646."),
        ("seo_rules", "\u0642\u0648\u0627\u0646\u06cc\u0646."),
    ):
        PromptRepo(pb).save_version(
            project_id=project["id"], ptype=ptype, name="default", content=content
        )

    registry = FakeRegistry()
    seed_points(registry, project["id"], n=6)
    # FakeLLM: outline JSON then section HTMLs
    registry.llm.responses = [
        '{"title": "\u062a", "slug": "t", "sections": [{"heading": "\u0627\u0644\u0641", "content_brief": "\u062e\u0644\u0627\u0635\u0647"}, {"heading": "\u0628", "content_brief": "\u062e\u0644\u0627\u0635\u0647"}]}',
        "<p>"
        + ("\u06a9\u0644\u0645\u0647 \u0628\u062e\u0634 \u0627\u0644\u0641 " * 15).strip()
        + "</p>",
        "<p>" + ("\u06a9\u0644\u0645\u0647 \u0628\u062e\u0634 \u0628 " * 15).strip() + "</p>",
    ]
    # threshold 0 so retrieval returns regardless of score luck
    pb.collection("project_settings").update(
        pb.collection("project_settings").get_first_list_item(f'project="{project["id"]}"')["id"],
        {"similarityThreshold": 0},
    )
    topic = TopicRepo(pb).create(
        project=project["id"], title="\u0633\u0626\u0648", keyword="\u0633\u0626\u0648"
    )
    job = JobRepo(pb).create(
        project=project["id"],
        type="write_article",
        payload={"topicId": topic["id"]},
        idempotency_key="w-retr-1",
        max_attempts=3,
    )
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
    asyncio.run(handle_write_article(ctx))

    # the LLM prompt contained link candidates + budgeted passages, not full docs
    outline_call = registry.llm.calls[0]["user"]
    assert "https://site.test/" in outline_call  # internal link candidates present
    assert (
        "\u0645\u0642\u0627\u0644\u0627\u062a \u0645\u0648\u062c\u0648\u062f \u062f\u0631 \u0633\u0627\u06cc\u062a"
        in outline_call
    )
    # passages are snippet-length, NOT the full 20x-repeated text
    assert (
        "\u0645\u062a\u0646 \u0645\u0631\u062a\u0628\u0637 \u0645\u0642\u0627\u0644\u0647"
        in outline_call
    )
    assert len(outline_call) < 6000  # context budget enforced (not full documents)
