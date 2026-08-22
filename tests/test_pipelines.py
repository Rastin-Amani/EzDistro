"""End-to-end pipeline tests (indexing, writing, publishing) with fake providers."""

from __future__ import annotations

from typing import Any

import pytest

from app.jobs.context import JobContext, ProviderStack
from app.providers.base import PermanentError, ProviderError, VectorPoint, WPPost
from app.repositories.jobs import JobEventRepo, JobRepo, now_utc
from app.repositories.projects import DEFAULT_SETTINGS
from app.repositories.prompts import PromptRepo
from app.repositories.topics import TopicRepo
from app.services.indexing import handle_index_project
from app.services.publishing_service import handle_publish_article
from app.services.settings import ProjectConfig
from app.services.writing import handle_write_article
from tests.fake_providers import FakePublisher, FakeRegistry
from tests.fakes import FakePocketBase, default_unique_fields

LONG_TEXT = ("کلمه " * 600).strip()  # ~600 words


def make_project(pb: FakePocketBase) -> dict[str, Any]:
    project = pb.collection("projects").create(
        {
            "name": "پروژه",
            "slug": "proj-a",
            "language": "fa",
            "status": "active",
            "timezone": "Asia/Tehran",
        }
    )
    pb.collection("project_settings").create({"project": project["id"], **DEFAULT_SETTINGS})
    for ptype, content in (
        ("brand_voice", "تو نویسنده سئو هستی."),
        ("outline_user", "JSON برگردان."),
        ("section_user", "HTML برگردان."),
        ("seo_rules", "قوانین سئو."),
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
        entity_type=type.replace("_article", "").replace("_run", ""),
        entity_id=payload.get("topicId") or payload.get("articleId") or project_id,
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


# ---------------------------------------------------------------------------
# Indexing
# ---------------------------------------------------------------------------
async def test_index_run_indexes_posts_and_is_idempotent():
    pb = FakePocketBase(default_unique_fields())
    project = make_project(pb)
    registry = FakeRegistry()
    registry.publisher = FakePublisher(
        posts=[
            WPPost(1, "پست اول", f"<p>{LONG_TEXT}</p>", "https://site.test/1", "publish"),
            WPPost(2, "پست دوم", f"<p>متن دوم {LONG_TEXT}</p>", "https://site.test/2", "publish"),
        ]
    )
    job = make_job(pb, project["id"], "index_project", {"trigger": "manual"}, "index:1")

    result = await _run(pb, registry, job)

    assert result["discovered"] == 2
    assert result["indexed"] == 2
    assert result["unchanged"] == 0
    assert result["changed"] == 0
    assert result["skipped"] == 0
    assert result["failed"] == 0

    # points upserted with stable ids
    assert registry.vector.points
    ids = list(registry.vector.points)
    assert all(p.startswith("proj-a:") for p in ids)

    # documents metadata recorded with content hash + embedding model
    docs = pb.collection("documents").get_full_list({"sort": "sourceId"})
    assert len(docs) == 2
    assert docs[0]["sourceId"] == "1"
    assert docs[0]["indexStatus"] == "indexed"
    assert docs[0]["contentHash"]
    assert docs[0]["embeddingModel"] == DEFAULT_SETTINGS["embeddingModel"]

    # run record finished with checkpoint
    run = pb.collection("index_runs").get_first_list_item(f'job="{job["id"]}"')
    assert run["status"] == "succeeded"
    assert run["lastSourceId"] == "2"

    # ---- second run: identical content → everything skipped, no new upserts
    registry.vector.upsert_calls.clear()
    job2 = make_job(pb, project["id"], "index_project", {"trigger": "manual"}, "index:2")
    result2 = await _run(pb, registry, job2)

    assert result2["unchanged"] == 2
    assert result2["indexed"] == 0
    assert registry.vector.upsert_calls == []  # embeddings NOT regenerated


async def test_index_run_resumes_from_checkpoint_after_crash():
    pb = FakePocketBase(default_unique_fields())
    project = make_project(pb)
    registry = FakeRegistry()
    registry.publisher = FakePublisher(
        posts=[
            WPPost(1, "اول", f"<p>{LONG_TEXT}</p>", "https://s.test/1", "publish"),
            WPPost(2, "دوم", f"<p>{LONG_TEXT}</p>", "https://s.test/2", "publish"),
            WPPost(3, "سوم", f"<p>{LONG_TEXT}</p>", "https://s.test/3", "publish"),
        ]
    )
    job = make_job(pb, project["id"], "index_project", {"trigger": "manual"}, "index:r1")

    # simulate a crash AFTER post 1 was fully indexed (Qdrant + document record)
    # but BEFORE the checkpoint persisted: run record has lastSourceId=1, job failed
    registry.vector.points["proj-a:1:0"] = VectorPoint(
        id="proj-a:1:0", vector=[0.1] * 8, payload={"project": "seoz-proj-a-model", "wp_post_id": 1}
    )
    pb.collection("documents").create(
        {
            "project": project["id"],
            "sourceType": "wordpress",
            "sourceId": "1",
            "title": "اول",
            "sourceUrl": "https://s.test/1",
            "contentHash": "abc",
            "embeddingProvider": "openai_compat",
            "embeddingModel": "text-embedding-3-small",
            "embeddingDimensions": 1536,
            "chunkCount": 1,
            "indexStatus": "indexed",
        }
    )
    run = pb.collection("index_runs").create(
        {
            "project": project["id"],
            "job": job["id"],
            "status": "failed",
            "trigger": "manual",
            "processedDocuments": 1,
            "lastSourceId": "1",
            "startedAt": now_utc().isoformat(),
        }
    )
    pb.collection("jobs").update(job["id"], {"status": "failed"})

    result = await _run(pb, registry, job)

    # resumed: posts 2,3 indexed (post 1 ≤ checkpoint assumed handled)
    assert result["discovered"] == 2
    assert result["indexed"] == 2
    assert result["unchanged"] == 0
    run2 = pb.collection("index_runs").get_one(run["id"])
    assert run2["status"] == "succeeded"
    assert run2["lastSourceId"] == "3"
    assert run2["totalDocuments"] == 2
    assert run2["elapsedSeconds"] >= 0


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------
OUTLINE_JSON = """{
  "title": "راهنمای سئو",
  "slug": "rahnama-seo",
  "sections": [
    {"heading": "مقدمه", "content_brief": "نکته ۱", "internal_links": [{"title": "مقاله مرتبط", "url": "https://site.test/1", "anchor_text": "مقاله مرتبط"}]},
    {"heading": "تکنیکها", "content_brief": "نکته ۲"},
    {"heading": "نتیجهگیری", "content_brief": "جمعبندی"}
  ]
}"""


SECTION_HTML = "<p>" + ("کلمه محتوای بخش " * 40).strip() + "</p>"


async def test_write_article_full_flow():
    from app.services.writing import handle_assemble_article, handle_generate_section

    pb = FakePocketBase(default_unique_fields())
    project = make_project(pb)
    topic = TopicRepo(pb).create(
        project=project["id"], title="راهنمای سئو", keyword="سئو", priority=5
    )

    registry = FakeRegistry()
    registry.llm.responses = [OUTLINE_JSON] + [SECTION_HTML] * 3
    registry.vector.points["proj-a:1:0"] = VectorPoint(
        id="proj-a:1:0",
        vector=[0.5] * 8,
        payload={"title": "مقاله مرتبط", "url": "https://site.test/1", "chunk_text": "متن مرتبط"},
    )

    # --- stage 1: write_article chains per-section jobs + assembler
    job = make_job(pb, project["id"], "write_article", {"topicId": topic["id"]}, "write:1")
    result = await _run(pb, registry, job)

    assert result["articleId"]
    article = pb.collection("articles").get_one(result["articleId"])
    assert article["status"] == "generating"
    assert article["outlineVersion"] == 1
    assert article["outline"]["slug"] == "rahnama-seo"  # immutable snapshot saved

    sections = pb.collection("article_sections").get_full_list({"sort": "position"})
    assert len(sections) == 3
    assert all(s["status"] == "pending" for s in sections)
    assert sections[0]["contentBrief"] == "نکته ۱"
    assert sections[0]["internalLinks"][0]["anchor_text"] == "مقاله مرتبط"

    section_jobs = pb.collection("jobs").get_full_list({"filter": 'type="generate_section"'})
    assert len(section_jobs) == 3
    assert all(j["entityType"] == "section" for j in section_jobs)
    assemble_job = pb.collection("jobs").get_first_list_item('type="assemble_article"')
    assert assemble_job["status"] == "pending"

    # --- stage 2: sections generate independently (each its own job)
    for section_job in section_jobs:
        await _run(pb, registry, section_job)
    sections = pb.collection("article_sections").get_full_list({"sort": "position"})
    assert all(s["status"] == "done" for s in sections)
    assert sections[0]["provider"] == "fake"
    assert sections[0]["model"] == "fake-model"
    assert sections[0]["tokenUsage"]["completion_tokens"] == 5
    assert sections[0]["generationLatency"] > 0
    assert sections[0]["generationAttempts"] == 1

    # --- stage 3: assembler waits, assembles, validates → review
    await _run(pb, registry, assemble_job)
    article = pb.collection("articles").get_one(result["articleId"])
    assert article["status"] == "review"
    assert article["validation"]["ok"] is True
    assert "<h1>" in article["finalHtml"]
    assert "<h2>" in article["finalHtml"]
    assert article["wordCount"] > 0
    assert article["seoScore"] > 0
    assert "https://site.test/1" in article["finalHtml"]  # intended internal link preserved

    topic_after = pb.collection("topics").get_one(topic["id"])
    assert topic_after["status"] == "review"
    assert topic_after["articleId"] == result["articleId"]

    # publish happens ONLY when requested — no publish job auto-chained
    assert pb.collection("jobs").get_full_list({"filter": 'type="publish_article"'}) == []

    # ---- re-run with same topic → short-circuits (already in pipeline)
    job2 = make_job(pb, project["id"], "write_article", {"topicId": topic["id"]}, "write:2")
    result2 = await _run(pb, registry, job2)
    assert result2["alreadyWritten"] is True
    assert result2["articleId"] == result["articleId"]


async def test_write_article_llm_garbage_outline_is_repaired_by_validation_prompt():
    pb = FakePocketBase(default_unique_fields())
    project = make_project(pb)
    PromptRepo(pb).save_version(
        project_id=project["id"],
        ptype="validation",
        name="default",
        content="خروجی معیوب است. JSON اصلاح‌شده را برگردان: {raw_output}",
    )
    topic = TopicRepo(pb).create(project=project["id"], title="تست", keyword="تست")

    registry = FakeRegistry()
    registry.llm.responses = [
        "این خروجی JSON نیست",  # garbage outline
        OUTLINE_JSON,  # repaired by validation pass
        "<p>مقدمه</p>",
        "<p>تکنیک‌ها</p>",
        "<p>نتیجه</p>",
    ]

    job = make_job(pb, project["id"], "write_article", {"topicId": topic["id"]}, "write:g1")
    result = await _run(pb, registry, job)

    assert result["articleId"]
    article = pb.collection("articles").get_one(result["articleId"])
    assert article["status"] == "generating"
    assert article["outline"]["title"] == "راهنمای سئو"
    # validation prompt was used → outline garbage (1) + repair (1); sections are
    # separate jobs and were not executed here
    assert len(registry.llm.calls) == 2


async def test_write_article_unrepairable_outline_fails_cleanly():
    pb = FakePocketBase(default_unique_fields())
    project = make_project(pb)
    topic = TopicRepo(pb).create(project=project["id"], title="تست", keyword="تست")

    registry = FakeRegistry()
    registry.llm.responses = ["این خروجی JSON نیست", "هنوز هم JSON نیست"]

    job = make_job(pb, project["id"], "write_article", {"topicId": topic["id"]}, "write:g2")
    with pytest.raises(ValueError):
        await _run(pb, registry, job)

    # topic + article marked failed, no sections created
    assert pb.collection("topics").get_one(topic["id"])["status"] == "failed"
    assert pb.collection("article_sections").get_full_list() == []


# ---------------------------------------------------------------------------
# Publishing
# ---------------------------------------------------------------------------
async def test_publish_article_publishes_once_and_guards_duplicates():
    pb = FakePocketBase(default_unique_fields())
    project = make_project(pb)
    topic = TopicRepo(pb).create(project=project["id"], title="عنوان", keyword="ک")
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

    registry = FakeRegistry()
    job = make_job(pb, project["id"], "publish_article", {"articleId": article["id"]}, "pub:1")
    result = await _run(pb, registry, job)

    assert result["postId"]
    assert len(registry.publisher.created) == 1
    created = registry.publisher.created[0]
    assert created["title"] == "عنوان مقاله"
    assert created["status"] == "publish"  # default publishing mode
    # WP renders the title itself — the leading <h1> must not be sent
    assert "<h1>" not in created["html"]
    assert "<p>محتوا</p>" in created["html"]

    runs = pb.collection("publishing_runs").get_full_list()
    assert len(runs) == 1
    assert runs[0]["status"] == "published"
    assert runs[0]["attempt"] == 1
    assert runs[0]["wordpressPostId"] == result["postId"]
    assert pb.collection("articles").get_one(article["id"])["status"] == "published"
    assert pb.collection("topics").get_one(topic["id"])["status"] == "published"

    # ---- second publish (stray "publish" action) on a published article → refused
    job2 = make_job(pb, project["id"], "publish_article", {"articleId": article["id"]}, "pub:2")
    with pytest.raises(ProviderError):
        await _run(pb, registry, job2)
    assert len(registry.publisher.created) == 1  # NO duplicate created

    # ---- content refresh: explicit "update" action → UPDATE, never a duplicate
    job3 = make_job(
        pb,
        project["id"],
        "publish_article",
        {"articleId": article["id"], "action": "update"},
        "pub:3",
    )
    result3 = await _run(pb, registry, job3)
    assert result3["updated"] is True
    assert len(registry.publisher.created) == 1  # still only one CREATE
    assert len(registry.publisher.updated) == 1  # the existing post was updated


async def test_publish_article_failure_records_attempt_and_fails():
    pb = FakePocketBase(default_unique_fields())
    project = make_project(pb)
    topic = TopicRepo(pb).create(project=project["id"], title="عنوان", keyword="ک")
    article = pb.collection("articles").create(
        {
            "project": project["id"],
            "topicId": topic["id"],
            "title": "عنوان",
            "status": "approved",
            "finalHtml": "<p>محتوا</p>",
            "outlineVersion": 1,
        }
    )

    registry = FakeRegistry()

    async def boom(*args, **kwargs):
        from app.providers.base import PermanentError

        raise PermanentError("401 unauthorized")

    registry.publisher.create_post = boom  # type: ignore[method-assign]

    job = make_job(pb, project["id"], "publish_article", {"articleId": article["id"]}, "pub:f1")
    with pytest.raises(PermanentError):
        await _run(pb, registry, job)

    runs = pb.collection("publishing_runs").get_full_list()
    assert runs[0]["status"] == "failed"
    assert runs[0]["error"]["type"] == "PermanentError"
    assert pb.collection("articles").get_one(article["id"])["status"] == "failed"


# ---------------------------------------------------------------------------
# helper
# ---------------------------------------------------------------------------
async def _run(pb: FakePocketBase, registry: FakeRegistry, job: dict[str, Any]) -> dict[str, Any]:
    from app.services.writing import handle_assemble_article, handle_generate_section

    ctx = make_ctx(pb, registry, job)
    handler = {
        "index_project": handle_index_project,
        "write_article": handle_write_article,
        "generate_section": handle_generate_section,
        "assemble_article": handle_assemble_article,
        "publish_article": handle_publish_article,
    }[job["type"]]
    return await handler(ctx)
