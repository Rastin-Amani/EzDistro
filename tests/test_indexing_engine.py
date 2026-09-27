"""Production indexing engine tests — fake WordPress + fake embeddings.

Covers: content-hash freshness (never post-id existence), skip-without-re-embed,
metadata-only updates without re-embedding, force reindex, stale cleanup on full
runs, chunk-config change detection, paginated incremental fetch, run counters,
and crash-safety of pending documents.
"""

from __future__ import annotations

from typing import Any

from app.jobs.context import JobContext, ProviderStack
from app.providers.base import WPPost
from app.repositories.jobs import JobEventRepo, JobRepo, now_utc
from app.repositories.projects import DEFAULT_SETTINGS
from app.repositories.prompts import PromptRepo
from app.services.indexing import handle_index_project
from app.services.settings import ProjectConfig
from tests.fake_providers import FakeEmbedding, FakePublisher, FakeRegistry, FakeVectorStore
from tests.fakes import FakePocketBase, default_unique_fields

LONG_TEXT = ("\u06a9\u0644\u0645\u0647 " * 600).strip()  # ~600 words → several chunks


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
    for ptype, content in (
        (
            "brand_voice",
            "\u062a\u0648 \u0646\u0648\u06cc\u0633\u0646\u062f\u0647 \u0633\u0626\u0648 \u0647\u0633\u062a\u06cc.",
        ),
        ("outline_user", "JSON \u0628\u0631\u06af\u0631\u062f\u0627\u0646."),
        ("section_user", "HTML \u0628\u0631\u06af\u0631\u062f\u0627\u0646."),
        ("seo_rules", "\u0642\u0648\u0627\u0646\u06cc\u0646 \u0633\u0626\u0648."),
    ):
        PromptRepo(pb).save_version(
            project_id=project["id"], ptype=ptype, name="default", content=content
        )
    return project


def make_job(
    pb: FakePocketBase, project_id: str, type: str, payload: dict, key: str
) -> dict[str, Any]:
    return JobRepo(pb).create(
        project=project_id,
        type=type,
        payload=payload,
        idempotency_key=key,
        max_attempts=3,
        entity_type="project",
        entity_id=project_id,
    )


def make_ctx(pb: FakePocketBase, registry: FakeRegistry, job: dict[str, Any]) -> JobContext:
    config = ProjectConfig.load(pb, job["project"])
    return JobContext(
        pb=pb,
        job=job,
        config=config,
        providers=ProviderStack(registry=registry, config=config),
        registry=registry,
        events=JobEventRepo(pb),
        set_progress=lambda jid, pct, **kw: pb.collection("jobs").update(
            jid, {"progress": pct, **kw}
        ),
        request_cancel=lambda jid: bool(
            (pb.collection("jobs").get_one(jid) or {}).get("cancelRequested")
        ),
    )


def posts(*ids: int, title_prefix: str = "\u067e\u0633\u062a") -> list[WPPost]:
    return [
        WPPost(
            i,
            f"{title_prefix} {i}",
            f"<p>{LONG_TEXT}</p><h2>\u062a\u06cc\u062a\u0631</h2><p>\u0645\u062a\u0646 {i}</p>",
            f"https://site.test/{i}",
            "publish",
        )
        for i in ids
    ]


# ---------------------------------------------------------------------------
# Freshness: content hash, not post-id existence
# ---------------------------------------------------------------------------
def test_unchanged_documents_are_skipped_without_reembedding():
    pb = FakePocketBase(default_unique_fields())
    project = make_project(pb)
    registry = FakeRegistry()
    registry.publisher = FakePublisher(posts=posts(1, 2))

    job = make_job(pb, project["id"], "index_project", {"trigger": "manual"}, "i1")
    result = asyncio_run(handle_index_project(make_ctx(pb, registry, job)))
    assert result["indexed"] == 2
    first_embed_requests = len(registry.embedding.requests)

    # second run: SAME content → unchanged, zero embedding calls
    job2 = make_job(pb, project["id"], "index_project", {"trigger": "manual"}, "i2")
    result2 = asyncio_run(handle_index_project(make_ctx(pb, registry, job2)))
    assert result2["unchanged"] == 2
    assert result2["indexed"] == 0
    assert len(registry.embedding.requests) == first_embed_requests  # NO re-embedding


def test_metadata_only_change_updates_without_reembedding():
    pb = FakePocketBase(default_unique_fields())
    project = make_project(pb)
    registry = FakeRegistry()
    registry.publisher = FakePublisher(posts=posts(1))

    job = make_job(pb, project["id"], "index_project", {"trigger": "manual"}, "m1")
    result = asyncio_run(handle_index_project(make_ctx(pb, registry, job)))
    assert result["indexed"] == 1
    embed_calls = len(registry.embedding.requests)

    # title changes on WordPress, content identical → metadata-only path
    registry.publisher.posts = posts(
        1, title_prefix="\u0639\u0646\u0648\u0627\u0646 \u062c\u062f\u06cc\u062f"
    )
    job2 = make_job(pb, project["id"], "index_project", {"trigger": "manual"}, "m2")
    result2 = asyncio_run(handle_index_project(make_ctx(pb, registry, job2)))

    assert result2["changed"] == 1  # counted as changed
    assert result2["unchanged"] == 0
    assert len(registry.embedding.requests) == embed_calls  # NO re-embedding
    assert registry.vector.upsert_calls == [registry.vector.upsert_calls[0]]  # no new upserts

    # PocketBase metadata updated
    doc = pb.collection("documents").get_first_list_item('sourceId="1"')
    assert doc["title"] == "\u0639\u0646\u0648\u0627\u0646 \u062c\u062f\u06cc\u062f 1"
    assert doc["indexStatus"] == "indexed"

    # vector payload updated via set_payload (metadata preserved in Qdrant too)
    assert any(
        p.payload["title"] == "\u0639\u0646\u0648\u0627\u0646 \u062c\u062f\u06cc\u062f 1"
        for p in registry.vector.points.values()
    )


def test_content_change_triggers_reembedding():
    pb = FakePocketBase(default_unique_fields())
    project = make_project(pb)
    registry = FakeRegistry()
    registry.publisher = FakePublisher(posts=posts(1))

    job = make_job(pb, project["id"], "index_project", {"trigger": "manual"}, "c1")
    asyncio_run(handle_index_project(make_ctx(pb, registry, job)))

    # content changes → hash differs → re-indexed + re-embedded
    registry.publisher.posts = [
        WPPost(
            1,
            "\u067e\u0633\u062a 1",
            f"<p>{LONG_TEXT} \u0645\u062d\u062a\u0648\u0627\u06cc \u06a9\u0627\u0645\u0644\u0627\u064b \u062c\u062f\u06cc\u062f \u0627\u0633\u062a</p>",
            "https://site.test/1",
            "publish",
        )
    ]
    job2 = make_job(pb, project["id"], "index_project", {"trigger": "manual"}, "c2")
    result2 = asyncio_run(handle_index_project(make_ctx(pb, registry, job2)))

    assert result2["changed"] == 1
    assert result2["indexed"] == 1
    assert len(registry.embedding.requests) > 0  # re-embedded


def test_chunk_config_change_detected_and_reindexed():
    pb = FakePocketBase(default_unique_fields())
    project = make_project(pb)
    registry = FakeRegistry()
    registry.publisher = FakePublisher(posts=posts(1))

    job = make_job(pb, project["id"], "index_project", {"trigger": "manual"}, "cc1")
    asyncio_run(handle_index_project(make_ctx(pb, registry, job)))

    # chunk size changes → same content hash but different chunking → re-index
    pb.collection("project_settings").update(
        pb.collection("project_settings").get_first_list_item(f'project="{project["id"]}"')["id"],
        {"chunkSize": 300, "chunkOverlap": 50},
    )
    job2 = make_job(pb, project["id"], "index_project", {"trigger": "manual"}, "cc2")
    result2 = asyncio_run(handle_index_project(make_ctx(pb, registry, job2)))

    assert result2["changed"] == 1
    assert result2["unchanged"] == 0
    doc = pb.collection("documents").get_first_list_item('sourceId="1"')
    assert doc["chunkCount"] != 0


def test_force_reindex_ignores_hashes():
    pb = FakePocketBase(default_unique_fields())
    project = make_project(pb)
    registry = FakeRegistry()
    registry.publisher = FakePublisher(posts=posts(1))

    job = make_job(pb, project["id"], "index_project", {"trigger": "manual"}, "f1")
    asyncio_run(handle_index_project(make_ctx(pb, registry, job)))
    embeds_before = len(registry.embedding.requests)

    job2 = make_job(
        pb, project["id"], "index_project", {"trigger": "manual", "force": True, "full": True}, "f2"
    )
    result2 = asyncio_run(handle_index_project(make_ctx(pb, registry, job2)))

    assert result2["unchanged"] == 0
    assert result2["changed"] == 1
    assert result2["indexed"] == 1
    assert len(registry.embedding.requests) > embeds_before  # re-embedded everything


# ---------------------------------------------------------------------------
# Stale vector cleanup (full runs only)
# ---------------------------------------------------------------------------
def test_full_run_cleans_stale_vectors():
    pb = FakePocketBase(default_unique_fields())
    project = make_project(pb)
    registry = FakeRegistry()
    registry.publisher = FakePublisher(posts=posts(1, 2, 3))

    job = make_job(pb, project["id"], "index_project", {"trigger": "manual"}, "s1")
    asyncio_run(handle_index_project(make_ctx(pb, registry, job)))
    assert len(registry.vector.points) > 0

    # post 1 deleted from WordPress; full run must delete its vectors
    registry.publisher.posts = posts(2, 3)
    job2 = make_job(
        pb, project["id"], "index_project", {"trigger": "manual", "full": True, "force": True}, "s2"
    )
    result2 = asyncio_run(handle_index_project(make_ctx(pb, registry, job2)))

    assert result2["staleDeleted"] > 0
    remaining = [p for p in registry.vector.points.values() if p.payload.get("source_id") == "1"]
    assert remaining == []  # stale vectors gone


def test_incremental_run_does_not_clean_stale():
    pb = FakePocketBase(default_unique_fields())
    project = make_project(pb)
    registry = FakeRegistry()
    registry.publisher = FakePublisher(posts=posts(1, 2, 3))

    job = make_job(pb, project["id"], "index_project", {"trigger": "manual"}, "inc1")
    asyncio_run(handle_index_project(make_ctx(pb, registry, job)))

    # delete post 1; incremental run skips it but must NOT delete its vectors
    registry.publisher.posts = posts(2, 3)
    job2 = make_job(pb, project["id"], "index_project", {"trigger": "manual"}, "inc2")
    result2 = asyncio_run(handle_index_project(make_ctx(pb, registry, job2)))
    assert result2["staleDeleted"] == 0
    assert any(p.payload.get("source_id") == "1" for p in registry.vector.points.values())


# ---------------------------------------------------------------------------
# Paginated incremental fetch
# ---------------------------------------------------------------------------
def test_paginated_fetch_and_checkpoint():
    pb = FakePocketBase(default_unique_fields())
    project = make_project(pb)
    registry = FakeRegistry()
    all_posts = posts(*range(1, 51))
    registry.publisher = FakePublisher(posts=all_posts)

    job = make_job(pb, project["id"], "index_project", {"trigger": "manual"}, "p1")
    result = asyncio_run(handle_index_project(make_ctx(pb, registry, job)))

    assert result["discovered"] == 50
    assert result["indexed"] == 50
    run = pb.collection("index_runs").get_first_list_item(f'job="{job["id"]}"')
    assert run["lastSourceId"] == "50"

    # fresh incremental run scans from 0: unchanged skipped WITHOUT re-embedding,
    # new posts indexed (cross-run change detection via content hash)
    embeds_after_first = len(registry.embedding.requests)
    registry.publisher.posts = all_posts + posts(51, 52)
    job2 = make_job(pb, project["id"], "index_project", {"trigger": "manual"}, "p2")
    result2 = asyncio_run(handle_index_project(make_ctx(pb, registry, job2)))
    assert result2["discovered"] == 52
    assert result2["unchanged"] == 50
    assert result2["indexed"] == 2
    # exactly 2 more embedding requests (one batch for the 2 new posts)
    assert len(registry.embedding.requests) == embeds_after_first + 2  # one call per new post


# ---------------------------------------------------------------------------
# Crash safety: pending documents are re-indexed
# ---------------------------------------------------------------------------
def test_pending_document_from_crashed_run_is_reindexed():
    pb = FakePocketBase(default_unique_fields())
    project = make_project(pb)
    registry = FakeRegistry()
    registry.publisher = FakePublisher(posts=posts(1))

    # simulate a crash AFTER metadata write but BEFORE vector upsert
    pb.collection("documents").create(
        {
            "project": project["id"],
            "sourceType": "wordpress",
            "sourceId": "1",
            "title": "\u067e\u0633\u062a 1",
            "sourceUrl": "https://site.test/1",
            "contentHash": "deadbeef",
            "embeddingProvider": "cohere",
            "embeddingModel": "embed-v4.0",
            "embeddingDimensions": 1024,
            "chunkCount": 5,
            "indexStatus": "pending",
        }
    )
    job = make_job(pb, project["id"], "index_project", {"trigger": "manual"}, "crash1")
    result = asyncio_run(handle_index_project(make_ctx(pb, registry, job)))
    # pending record → hash mismatch OR status guard → re-indexed
    assert result["indexed"] == 1
    doc = pb.collection("documents").get_first_list_item('sourceId="1"')
    assert doc["indexStatus"] == "indexed"
    assert len(registry.vector.points) > 0


# ---------------------------------------------------------------------------
# Counters & payload contract
# ---------------------------------------------------------------------------
def test_run_counters_and_vector_payload_contract():
    pb = FakePocketBase(default_unique_fields())
    project = make_project(pb)
    registry = FakeRegistry()
    # post 2 has empty content → skipped
    registry.publisher = FakePublisher(
        posts=[
            WPPost(
                1, "\u067e\u0633\u062a 1", f"<p>{LONG_TEXT}</p>", "https://site.test/1", "publish"
            ),
            WPPost(2, "\u062e\u0627\u0644\u06cc", "<p>   </p>", "https://site.test/2", "publish"),
        ]
    )
    job = make_job(pb, project["id"], "index_project", {"trigger": "manual"}, "ct1")
    result = asyncio_run(handle_index_project(make_ctx(pb, registry, job)))

    assert result["discovered"] == 2
    assert result["indexed"] == 1
    assert result["skipped"] == 1
    assert result["failed"] == 0

    # payload contract: every vector carries the documented fields
    for point in registry.vector.points.values():
        payload = point.payload
        assert payload["project_id"] == project["id"]
        assert payload["document_id"]  # PB document record id
        assert payload["source_id"] == "1"
        assert payload["source_url"] == "https://site.test/1"
        assert payload["title"] == "\u067e\u0633\u062a 1"
        assert payload["chunk_index"] >= 0
        assert payload["content_hash"]
        assert payload["language"] == "fa"
        assert payload["created_at"]

    # all documents recorded with counters
    run = pb.collection("index_runs").get_first_list_item(f'job="{job["id"]}"')
    assert run["indexedDocuments"] == 1
    assert run["skippedDocuments"] == 1
    assert run["unchangedDocuments"] == 0


def test_queries_filter_by_project_id():
    """Retrieval must never leak vectors from other projects."""
    from app.providers.base import VectorPoint
    from app.repositories.topics import TopicRepo
    from app.services.writing import _retrieval_data

    pb = FakePocketBase(default_unique_fields())
    project = make_project(pb)
    registry = FakeRegistry()
    registry.vector.points["proj-a:1:0"] = VectorPoint(
        id="proj-a:1:0",
        vector=[0.5] * 8,
        payload={
            "project_id": project["id"],
            "source_id": "1",
            "title": "\u0645\u0642\u0627\u0644\u0647 \u062e\u0648\u062f\u0645\u0627\u0646",
            "url": "https://site.test/1",
            "chunk_text": "\u0645\u062a\u0646 \u0645\u0631\u062a\u0628\u0637 \u062e\u0648\u062f\u0645\u0627\u0646",
        },
    )
    registry.vector.points["other:9:0"] = VectorPoint(
        id="other:9:0",
        vector=[0.9] * 8,
        payload={
            "project_id": "OTHER",
            "source_id": "9",
            "title": "\u0645\u0642\u0627\u0644\u0647 \u062f\u06cc\u06af\u0631\u0627\u0646",
            "url": "https://other.test/9",
            "chunk_text": "\u0645\u062a\u0646 \u067e\u0631\u0648\u0698\u0647 \u062f\u06cc\u06af\u0631",
        },
    )
    topic = TopicRepo(pb).create(
        project=project["id"], title="\u0633\u0626\u0648", keyword="\u0633\u0626\u0648"
    )
    job = make_job(pb, project["id"], "index_project", {}, "qf1")
    ctx = make_ctx(pb, registry, job)

    import asyncio

    data = asyncio.run(_retrieval_data(ctx, topic))
    context = data["context"]
    assert (
        "\u0645\u062a\u0646 \u0645\u0631\u062a\u0628\u0637 \u062e\u0648\u062f\u0645\u0627\u0646"
        in context
    )
    assert (
        "\u0645\u062a\u0646 \u067e\u0631\u0648\u0698\u0647 \u062f\u06cc\u06af\u0631" not in context
    )  # foreign project excluded


def asyncio_run(coro):
    import asyncio

    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# ---------------------------------------------------------------------------
# H2 — embedding model switch must re-embed (freshness guard covers model/dims)
# ---------------------------------------------------------------------------
def test_embedding_model_switch_triggers_reindex():
    """Changing the embedding model (new vector namespace) must NOT be treated
    as 'unchanged' — the document is re-embedded so the new namespace gets
    vectors (HIGH H2 regression)."""
    pb = FakePocketBase(default_unique_fields())
    project = make_project(pb)
    registry = FakeRegistry()
    registry.publisher = FakePublisher(
        posts=[
            WPPost(
                id=1,
                title="\u067e",
                content_html="<p>\u0645\u062a\u0646</p>" * 30,
                link="https://s.test/?p=1",
                status="publish",
            )
        ]
    )
    registry.embedding = FakeEmbedding()

    # first run under model A
    job = make_job(pb, project["id"], "index_project", {}, "h2a")
    asyncio_run(handle_index_project(make_ctx(pb, registry, job)))
    doc = pb.collection("documents").get_full_list()[0]
    assert doc["embeddingModel"] == "embed-v4.0"
    assert len(registry.vector.points) == 1

    # switch embedding model (namespace changes in production); bump the run
    pb.collection("project_settings").update(
        pb.collection("project_settings").get_first_list_item(f'project="{project["id"]}"')["id"],
        {"embeddingModel": "embed-v3.0", "embeddingDimensions": 1024},
    )
    registry.vector = FakeVectorStore()  # fresh namespace (as in prod)
    registry.embedding = FakeEmbedding()
    job2 = make_job(pb, project["id"], "index_project", {}, "h2b")
    asyncio_run(handle_index_project(make_ctx(pb, registry, job2)))

    # the document was re-embedded into the NEW namespace (not left empty)
    doc2 = pb.collection("documents").get_full_list()[0]
    assert doc2["embeddingModel"] == "embed-v3.0"
    assert len(registry.vector.points) == 1  # re-embedded in the new namespace
