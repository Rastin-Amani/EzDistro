"""Image job handlers: planning, generation, quality gate, fallback,
optimization, WP upload idempotency."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from app.domain.images import ArticleImagePlan
from app.jobs.context import JobContext, ProviderStack
from app.providers.base import ImageResult, PermanentError, ProviderError
from app.repositories.article_images import ArticleImageRepo
from app.repositories.articles import ArticleRepo
from app.repositories.jobs import JobRepo
from app.services.image_planning import handle_plan_article_images
from app.services.images import (
    handle_generate_cover_image,
    handle_generate_interior_image,
    handle_optimize_article_image,
    handle_publish_article_image,
)
from app.services.settings import ProjectConfig
from tests.fake_providers import FakeImageProvider, FakeLLM, FakeRegistry
from tests.fakes import FakePocketBase
from tests.helpers import make_article, make_pb, make_project, make_prompt
from tests.test_wordpress_publishing import make_ctx

PLAN_JSON = json.dumps(
    {
        "images": [
            {
                "role": "cover",
                "prompt": "closeup of a car dashboard with an amber warning light",
                "alt_text": "car display with the warning light on",
                "caption": "engine warning light on the speedometer",
                "aspect_ratio": "16:9",
            },
            {
                "role": "interior",
                "section_key": "section-1",
                "prompt": "open engine bay photographed in daylight",
                "alt_text": "car engine from above",
            },
        ]
    }
)


def make_plan_article(pb: FakePocketBase, project_id: str) -> dict[str, Any]:
    article = make_article(
        pb,
        project_id,
        final_html="<h1>Title</h1><p>article text</p>",
    )
    ArticleRepo(pb).set_final_content(
        article["id"],
        html=article.get("finalHtml") or "<h1>Title</h1><p>article text</p>",
        word_count=600,
        seo_score=80,
        meta_description="M",
    )
    return pb.collection("articles").get_one(article["id"])


def set_plan(pb: FakePocketBase, article_id: str, sections: list[str]) -> dict[str, Any]:
    """Store a valid plan directly (cover + one interior per given key)."""
    images: list[dict[str, Any]] = [
        {
            "role": "cover",
            "prompt": "amber check-engine warning on a dashboard",
            "alt_text": "engine warning light on the dashboard",
            "caption": "engine warning light when the car starts",
            "aspect_ratio": "16:9",
        }
    ]
    for key in sections:
        images.append(
            {
                "role": "interior",
                "section_key": key,
                "prompt": f"photo illustrating {key}",
                "alt_text": "image of the relevant section",
            }
        )
    plan = ArticleImagePlan.model_validate({"version": 1, "style": {}, "images": images})
    ArticleRepo(pb).set_image_plan(article_id, plan.to_dict(), 1)
    return plan.to_dict()


def latest_row(pb: FakePocketBase, article_id: str, role: str) -> Any:
    rows = [r for r in ArticleImageRepo(pb).list_for_article(article_id) if r["role"] == role]
    assert rows, f"no {role} rows"
    return rows[0]  # sorted -version


def img_provider(registry: FakeRegistry, name: str) -> Any:
    return registry.images.setdefault(name, FakeImageProvider(provider_name=name))


def run_job(
    pb: FakePocketBase,
    registry: FakeRegistry,
    job_type: str,
    payload: dict[str, Any],
    *,
    key: str = "",
) -> dict[str, Any]:
    job = JobRepo(pb).create(
        project=payload["projectId"],
        type=job_type,
        payload=payload,
        idempotency_key=f"t:{key}:{job_type}:{payload.get('articleId')}:{payload.get('imageId', '')}:{payload.get('sectionKey', '')}:{payload.get('version', '')}",
        max_attempts=3,
    )
    ctx: JobContext = make_ctx(pb, registry, job)
    handler = {
        "plan_article_images": handle_plan_article_images,
        "generate_cover_image": handle_generate_cover_image,
        "generate_interior_image": handle_generate_interior_image,
        "optimize_article_image": handle_optimize_article_image,
        "publish_article_image": handle_publish_article_image,
    }[job_type]
    return asyncio.run(handler(ctx))


# ---------------------------------------------------------------------------
# planning
# ---------------------------------------------------------------------------
def test_plan_job_builds_and_stores_plan():
    pb = make_pb()
    project = make_project(pb)
    article = make_plan_article(pb, project["id"])
    make_prompt(pb, project["id"], "image_plan_user")
    make_prompt(pb, project["id"], "image_plan_system")
    registry = FakeRegistry()
    registry.llm = FakeLLM([PLAN_JSON])

    result = run_job(
        pb,
        registry,
        "plan_article_images",
        {"projectId": project["id"], "articleId": article["id"]},
    )
    assert result["version"] == 1
    assert result["cover"] == 1 and result["interiors"] == 1

    refreshed = pb.collection("articles").get_one(article["id"])
    assert refreshed["imagePlanVersion"] == 1
    plan = refreshed["imagePlan"]
    assert len(plan["images"]) == 2
    cover = plan["images"][0]
    # composed prompt + negative prompt + suggested size filled in
    assert cover["negative_prompt"].startswith("text, letters, words")
    assert cover["suggested_size"][0] >= 1200
    interior = plan["images"][1]
    assert interior["section_key"] == "section-1"


def test_plan_job_requires_content():
    pb = make_pb()
    project = make_project(pb)
    article = make_article(pb, project["id"], final_html=None)
    make_prompt(pb, project["id"], "image_plan_user")
    registry = FakeRegistry()
    with pytest.raises(
        ProviderError,
        match="has no content yet",
    ):
        run_job(
            pb,
            registry,
            "plan_article_images",
            {"projectId": project["id"], "articleId": article["id"]},
        )


# ---------------------------------------------------------------------------
# generation
# ---------------------------------------------------------------------------
def test_generate_cover_happy_path():
    pb = make_pb()
    project = make_project(pb)
    article = make_plan_article(pb, project["id"])
    set_plan(pb, article["id"], [])
    registry = FakeRegistry()

    result = run_job(
        pb,
        registry,
        "generate_cover_image",
        {"projectId": project["id"], "articleId": article["id"]},
    )
    assert result["imageId"]
    repo = ArticleImageRepo(pb)
    row = repo.active_for_role(article["id"], "cover")
    assert row["status"] == "ready"
    assert row["optimizedFile"] and row["format"] == "image/webp"
    assert row["width"] >= 1200
    assert row["provider"] == "gemini"
    assert row.get("estimatedCost") in (0, None, "")  # gemini cost unknown
    assert row["fingerprint"]
    # files stored as PB files (no base64 in metadata)
    assert pb.storage.file_bytes("article_images", row["id"], "sourceFile")
    assert pb.storage.file_bytes("article_images", row["id"], "optimizedFile")
    # generation actually hit the provider at planned dims
    assert img_provider(registry, "gemini").calls[0].width == row["width"]
    # metadata (alt/caption) came from the plan
    assert row["altText"] == "engine warning light on the dashboard"


def test_generation_is_idempotent_via_fingerprint():
    pb = make_pb()
    project = make_project(pb)
    article = make_plan_article(pb, project["id"])
    set_plan(pb, article["id"], [])
    registry = FakeRegistry()
    payload = {"projectId": project["id"], "articleId": article["id"]}

    run_job(pb, registry, "generate_cover_image", dict(payload))
    calls_after_first = len(registry.image.calls)
    result = run_job(pb, registry, "generate_cover_image", dict(payload))

    assert result["reused"] is True
    assert len(registry.image.calls) == calls_after_first  # no re-billing
    repo = ArticleImageRepo(pb)
    rows = repo.list_for_article(article["id"])
    assert len(rows) == 1  # no duplicate version row


def test_generate_interior_and_versions():
    pb = make_pb()
    project = make_project(pb)
    article = make_plan_article(pb, project["id"])
    set_plan(pb, article["id"], ["section-1"])
    registry = FakeRegistry()

    run_job(
        pb,
        registry,
        "generate_interior_image",
        {"projectId": project["id"], "articleId": article["id"], "sectionKey": "section-1"},
    )
    # regenerate (explicit version, like the API's regenerate route) → new row
    payload = {
        "projectId": project["id"],
        "articleId": article["id"],
        "sectionKey": "section-1",
        "version": 2,
    }
    run_job(pb, registry, "generate_interior_image", payload, key="regen")

    repo = ArticleImageRepo(pb)
    rows = repo.list_for_article(article["id"])  # history: all versions
    interiors = [r for r in rows if r["role"] == "interior"]
    assert len(interiors) == 2
    active = [r for r in interiors if r["active"]]
    assert len(active) == 1
    assert active[0]["version"] == 2


def test_fallback_provider_used_on_primary_failure():
    pb = make_pb()
    project = make_project(pb)
    settings_id = pb.collection("project_settings").get_first_list_item(
        f'project="{project["id"]}"'
    )["id"]
    pb.collection("project_settings").update(
        settings_id, {"imageFallbackProvider": "fallback", "imageFallbackModel": "fb-1"}
    )
    article = make_plan_article(pb, project["id"])
    set_plan(pb, article["id"], ["section-1"])
    registry = FakeRegistry()
    img_provider(registry, "bfl").queue(PermanentError("bfl down"))

    run_job(
        pb,
        registry,
        "generate_interior_image",
        {"projectId": project["id"], "articleId": article["id"], "sectionKey": "section-1"},
    )
    repo = ArticleImageRepo(pb)
    row = repo.active_for_role(article["id"], "interior")
    assert row["status"] == "ready"
    assert row["provider"] == "fallback"
    assert row["model"] == "fb-1"


def test_quality_gate_failure_marks_row_failed():
    pb = make_pb()
    project = make_project(pb)
    article = make_plan_article(pb, project["id"])
    set_plan(pb, article["id"], ["section-1"])
    registry = FakeRegistry()
    img_provider(registry, "bfl").queue(
        ImageResult(data=b"not-an-image", mime_type="image/png", provider="bfl", model="m")
    )

    with pytest.raises(PermanentError):
        run_job(
            pb,
            registry,
            "generate_interior_image",
            {"projectId": project["id"], "articleId": article["id"], "sectionKey": "section-1"},
        )
    row = latest_row(pb, article["id"], "interior")
    assert row["status"] == "failed"
    assert row["error"]["category"] == "decode_failure"


def test_ai_qa_skipped_when_llm_has_no_vision():
    pb = make_pb()
    project = make_project(pb)
    settings_id = pb.collection("project_settings").get_first_list_item(
        f'project="{project["id"]}"'
    )["id"]
    pb.collection("project_settings").update(settings_id, {"imageAiQaEnabled": True})
    article = make_plan_article(pb, project["id"])
    set_plan(pb, article["id"], [])
    registry = FakeRegistry()

    result = run_job(
        pb,
        registry,
        "generate_cover_image",
        {"projectId": project["id"], "articleId": article["id"]},
    )
    assert result["imageId"]
    row = ArticleImageRepo(pb).active_for_role(article["id"], "cover")
    assert row["status"] == "ready"


# ---------------------------------------------------------------------------
# optimization + WP upload
# ---------------------------------------------------------------------------
def _ready_row(pb: FakePocketBase, registry: FakeRegistry, project, article) -> dict[str, Any]:
    run_job(
        pb,
        registry,
        "generate_cover_image",
        {"projectId": project["id"], "articleId": article["id"]},
    )
    return ArticleImageRepo(pb).active_for_role(article["id"], "cover")


def test_optimize_handler_reoptimizes_from_source(monkeypatch):
    pb = make_pb()
    project = make_project(pb)
    article = make_plan_article(pb, project["id"])
    set_plan(pb, article["id"], [])
    registry = FakeRegistry()
    row = _ready_row(pb, registry, project, article)

    async def fake_download(url: str, max_bytes: int = 0) -> bytes:
        record_id = url.rstrip("/").split("/")[-2]
        data = pb.storage.file_bytes("article_images", record_id, "optimizedFile")
        assert data
        return data

    monkeypatch.setattr("app.services.images._download_file", fake_download)
    result = run_job(
        pb,
        registry,
        "optimize_article_image",
        {"projectId": project["id"], "articleId": article["id"], "imageId": row["id"]},
    )
    refreshed = pb.collection("article_images").get_one(row["id"])
    assert refreshed["status"] == "ready"
    assert pb.storage.file_bytes("article_images", row["id"], "optimizedFile")
    assert result["imageId"] == row["id"]


def test_publish_article_image_uploads_once_and_sets_featured(monkeypatch):
    pb = make_pb()
    project = make_project(pb)
    article = make_plan_article(pb, project["id"])
    set_plan(pb, article["id"], [])
    registry = FakeRegistry()
    row = _ready_row(pb, registry, project, article)
    pb.collection("articles").update(article["id"], {"wordpressPostId": 42})

    from app.domain.images import MAX_SOURCE_BYTES

    async def fake_download(url: str, *, max_bytes: int = MAX_SOURCE_BYTES) -> bytes:
        record_id = url.rstrip("/").split("/")[-2]
        data = pb.storage.file_bytes("article_images", record_id, "optimizedFile")
        assert data
        return data

    monkeypatch.setattr("app.services.images._download_file", fake_download)

    payload = {
        "projectId": project["id"],
        "articleId": article["id"],
        "imageId": row["id"],
        "featured": True,
    }
    run_job(pb, registry, "publish_article_image", dict(payload))
    run_job(pb, registry, "publish_article_image", dict(payload))  # retry

    uploads = registry.publisher.media_uploads_of("")
    assert len(uploads) == 1  # idempotent: stored media id reused, no duplicate
    assert uploads[0]["alt_text"] == "engine warning light on the dashboard"
    # featured set for the published post (repeat calls are WP-idempotent, same id)
    assert uploads[0]["id"] == 5001
    assert registry.publisher.featured_calls[-1] == (42, 5001)
    refreshed = pb.collection("article_images").get_one(row["id"])
    assert refreshed["wordpressMediaId"] == uploads[0]["id"]
    assert refreshed["wordpressUrl"]


def test_publish_article_image_featured_requires_published_article():
    pb = make_pb()
    project = make_project(pb)
    article = make_plan_article(pb, project["id"])
    set_plan(pb, article["id"], [])
    registry = FakeRegistry()
    row = _ready_row(pb, registry, project, article)
    # no wordpressPostId
    with pytest.raises(
        PermanentError,
        match="not published on WordPress yet",
    ):
        run_job(
            pb,
            registry,
            "publish_article_image",
            {
                "projectId": project["id"],
                "articleId": article["id"],
                "imageId": row["id"],
                "featured": True,
            },
        )


def test_project_config_images_defaults():
    pb = make_pb()
    project = make_project(pb)
    config = ProjectConfig.load(pb, project["id"])
    imgs = config.images
    assert imgs["cover_provider"] == "gemini"
    assert imgs["cover_model"] == "gemini-3-pro-image"
    assert imgs["interior_provider"] == "bfl"
    assert imgs["max_interior_images"] == 4
    assert imgs["optimization_format"] == "webp"


def test_auto_pipeline_writes_article_then_plans_then_generates():
    """write_article completion chains plan_article_images; the plan chains
    cover + interior generation automatically."""
    pb = make_pb()
    project = make_project(pb)
    article = make_plan_article(pb, project["id"])
    make_prompt(pb, project["id"], "image_plan_user")
    make_prompt(pb, project["id"], "image_plan_system")
    # active image integration (guard for auto-generation)
    pb.collection("integrations").create(
        {
            "project": project["id"],
            "category": "image",
            "provider": "fakeimg",
            "displayName": "img",
            "configuration": {},
            "secretsEnc": "",
            "enabled": True,
        }
    )
    registry = FakeRegistry()
    registry.llm = FakeLLM([PLAN_JSON])

    run_job(
        pb,
        registry,
        "plan_article_images",
        {"projectId": project["id"], "articleId": article["id"]},
    )

    types = sorted(
        j["type"]
        for j in pb.collection("jobs").get_full_list()
        if j["type"] != "plan_article_images"
    )
    assert types == ["generate_cover_image", "generate_interior_image"], types
    for j in pb.collection("jobs").get_full_list():
        if j["type"].startswith("generate_"):
            assert j["payload"]["version"] == 1
            assert j["idempotencyKey"].endswith(":v1")


def test_auto_generation_skipped_without_image_integration():
    pb = make_pb()
    project = make_project(pb)
    article = make_plan_article(pb, project["id"])
    make_prompt(pb, project["id"], "image_plan_user")
    make_prompt(pb, project["id"], "image_plan_system")
    registry = FakeRegistry()
    registry.llm = FakeLLM([PLAN_JSON])

    run_job(
        pb,
        registry,
        "plan_article_images",
        {"projectId": project["id"], "articleId": article["id"]},
    )

    types = [j["type"] for j in pb.collection("jobs").get_full_list()]
    assert "generate_cover_image" not in types
