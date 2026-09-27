"""Image ↔ publishing integration: placeholder resolution, featured cover,
WP media upload idempotency, cover-failure publish gate."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from app.domain.images import MAX_SOURCE_BYTES
from app.providers.base import ProviderError
from app.repositories.article_images import ArticleImageRepo
from app.repositories.jobs import JobRepo
from app.services.publishing_service import handle_publish_article
from tests.fake_providers import FakeLLM, FakeRegistry
from tests.fakes import FakePocketBase
from tests.helpers import make_pb, make_project, make_prompt
from tests.test_image_jobs import (
    PLAN_JSON,
    img_provider,
    latest_row,
    make_plan_article,
    run_job,
    set_plan,
)
from tests.test_wordpress_publishing import make_ctx

PLAN_JSON_E2E = json.dumps(
    {
        "images": [
            {
                "role": "cover",
                "prompt": "closeup of a car dashboard with an amber warning light",
                "alt_text": "\u0646\u0645\u0627\u06cc\u0634\u06af\u0631 \u062e\u0648\u062f\u0631\u0648 \u0628\u0627 \u0686\u0631\u0627\u063a \u0647\u0634\u062f\u0627\u0631 \u0631\u0648\u0634\u0646",
                "caption": "\u0686\u0631\u0627\u063a \u0647\u0634\u062f\u0627\u0631 \u0645\u0648\u062a\u0648\u0631 \u0631\u0648\u06cc \u0635\u0641\u062d\u0647 \u06a9\u06cc\u0644\u0648\u0645\u062a\u0631",
                "aspect_ratio": "16:9",
            },
            {
                "role": "interior",
                "section_key": "section-1",
                "prompt": "open engine bay photographed in daylight",
                "alt_text": "\u0645\u0648\u062a\u0648\u0631 \u062e\u0648\u062f\u0631\u0648 \u0627\u0632 \u0646\u0645\u0627\u06cc \u0628\u0627\u0644\u0627",
            },
            {
                "role": "interior",
                "section_key": "section-2",
                "prompt": "mechanic plugging a diagnostic scanner into the car",
                "alt_text": "\u062a\u0639\u0645\u06cc\u0631\u06a9\u0627\u0631 \u062f\u0631 \u062d\u0627\u0644 \u0627\u062a\u0635\u0627\u0644 \u062f\u0633\u062a\u06af\u0627\u0647 \u062f\u06cc\u0627\u06af",
            },
        ]
    },
    ensure_ascii=False,
)


def make_publishable_article(pb: FakePocketBase, project_id: str) -> dict[str, Any]:
    html = (
        "<h1>\u0639\u0646\u0648\u0627\u0646</h1>"
        "<p>\u0645\u0642\u062f\u0645\u0647 \u0645\u0642\u0627\u0644\u0647 \u0628\u0631\u0627\u06cc \u0627\u0646\u062a\u0634\u0627\u0631.</p>"
        "{{IMAGE:cover}}"
        "<h2>\u0628\u062e\u0634 \u0627\u0648\u0644</h2>"
        "<p>\u0645\u062a\u0646 \u0628\u062e\u0634 \u0627\u0648\u0644.</p>"
        "{{IMAGE:section-1}}"
    )
    article = make_plan_article(pb, project_id)
    from app.repositories.articles import ArticleRepo

    ArticleRepo(pb).update(article["id"], {"finalHtml": html, "status": "approved"})
    set_plan(pb, article["id"], ["section-1"])
    return pb.collection("articles").get_one(article["id"])


def make_images_ready(pb: FakePocketBase, project, article) -> None:
    """Generate the cover + every planned interior through the job handlers."""
    payload = {"projectId": project["id"], "articleId": article["id"]}
    run_job(pb, FakeRegistry(), "generate_cover_image", dict(payload))
    refreshed = pb.collection("articles").get_one(article["id"])
    for spec in (refreshed.get("imagePlan") or {}).get("images", []):
        if spec.get("role") != "interior":
            continue
        run_job(
            pb,
            FakeRegistry(),
            "generate_interior_image",
            {**payload, "sectionKey": spec.get("section_key")},
        )


def patch_download(monkeypatch, pb: FakePocketBase) -> None:
    async def fake_download(url: str, *, max_bytes: int = MAX_SOURCE_BYTES) -> bytes:
        record_id = url.rstrip("/").split("/")[-2]
        for field in ("optimizedFile", "sourceFile"):
            data = pb.storage.file_bytes("article_images", record_id, field)
            if data:
                return data
        raise AssertionError(f"no stored file for {url}")

    monkeypatch.setattr("app.services.images._download_file", fake_download)


def run_publish(
    pb: FakePocketBase,
    registry: FakeRegistry,
    article_id: str,
    project_id: str,
    *,
    key: str,
    payload_extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload = {"articleId": article_id, "action": "publish", **(payload_extra or {})}
    job = JobRepo(pb).create(
        project=project_id,
        type="publish_article",
        payload=payload,
        idempotency_key=key,
        max_attempts=3,
    )
    return asyncio.run(handle_publish_article(make_ctx(pb, registry, job)))


def test_publish_resolves_placeholders_and_sets_featured(monkeypatch):
    pb = make_pb()
    project = make_project(pb)
    article = make_publishable_article(pb, project["id"])
    make_images_ready(pb, project, article)
    registry = FakeRegistry()
    patch_download(monkeypatch, pb)

    result = run_publish(pb, registry, article["id"], project["id"], key="pub1")

    content = registry.publisher.created[-1]["html"]
    assert "{{IMAGE:" not in content  # placeholders fully resolved
    assert "<figure" in content and "<figcaption>" in content  # caption present
    assert "<img" in content
    assert 'loading="lazy"' in content  # interior lazy
    assert 'width="' in content and 'height="' in content  # CLS-safe
    # cover became the WP featured image
    assert (
        registry.publisher.featured_calls
        and registry.publisher.featured_calls[-1][0] == result["postId"]
    )
    # metadata localized from the plan
    upload = registry.publisher.media_uploads_of("")[0]
    assert (
        upload["alt_text"]
        == "\u0686\u0631\u0627\u063a \u0647\u0634\u062f\u0627\u0631 \u0645\u0648\u062a\u0648\u0631 \u0631\u0648\u06cc \u062f\u0627\u0634\u0628\u0648\u0631\u062f"
    )

    # retry/update → media reused, NOT re-uploaded (no duplicates in WP)
    before = len(registry.publisher.media)
    run_publish(
        pb, registry, article["id"], project["id"], key="pub2", payload_extra={"action": "update"}
    )
    assert len(registry.publisher.media) == before


def test_missing_cover_blocks_publish(monkeypatch):
    pb = make_pb()
    project = make_project(pb)
    article = make_publishable_article(pb, project["id"])
    registry = FakeRegistry()
    patch_download(monkeypatch, pb)

    with pytest.raises(ProviderError, match="Publishing stopped"):
        run_publish(pb, registry, article["id"], project["id"], key="nc1")

    refreshed = pb.collection("articles").get_one(article["id"])
    assert refreshed["status"] == "failed"
    assert registry.publisher.created == []  # nothing pushed to WP


def test_publish_without_cover_override(monkeypatch):
    pb = make_pb()
    project = make_project(pb)
    article = make_publishable_article(pb, project["id"])
    registry = FakeRegistry()
    patch_download(monkeypatch, pb)

    result = run_publish(
        pb,
        registry,
        article["id"],
        project["id"],
        key="ov1",
        payload_extra={"publishWithoutCover": True},
    )
    assert result["postId"]
    assert registry.publisher.featured_calls == []


def test_interior_failure_does_not_block_publish(monkeypatch):
    pb = make_pb()
    project = make_project(pb)
    article = make_publishable_article(pb, project["id"])
    # only the cover is generated; the interior stays missing
    run_job(
        pb,
        FakeRegistry(),
        "generate_cover_image",
        {"projectId": project["id"], "articleId": article["id"]},
    )
    registry = FakeRegistry()
    patch_download(monkeypatch, pb)

    run_publish(pb, registry, article["id"], project["id"], key="if1")
    content = registry.publisher.created[-1]["html"]
    assert "<figure" in content  # cover inserted (has caption)
    assert "{{IMAGE:section-1}}" not in content  # placeholder cleaned up


def test_full_e2e_scenario(monkeypatch):
    """Task E2E: content → plan → cover → 2 interiors → optimize → publish
    (upload + featured + insert) → retry reuses media, no duplicates."""
    pb = make_pb()
    project = make_project(pb)
    html = (
        "<h1>\u0639\u0646\u0648\u0627\u0646</h1>"
        "<p>\u0645\u0642\u062f\u0645\u0647.</p>"
        "{{IMAGE:cover}}"
        "<h2>\u0628\u062e\u0634 \u0627\u0648\u0644</h2><p>\u0645\u062a\u0646.</p>"
        "{{IMAGE:section-1}}"
        "<h2>\u0628\u062e\u0634 \u062f\u0648\u0645</h2><p>\u0645\u062a\u0646.</p>"
        "{{IMAGE:section-2}}"
    )
    article = make_plan_article(pb, project["id"])
    from app.repositories.articles import ArticleRepo

    ArticleRepo(pb).update(article["id"], {"finalHtml": html, "status": "approved"})

    # 1) plan through the job (LLM-driven, as in production)
    make_prompt(pb, project["id"], "image_plan_user")
    make_prompt(pb, project["id"], "image_plan_system")
    plan_registry = FakeRegistry()
    plan_registry.llm = FakeLLM([PLAN_JSON_E2E])
    plan_result = run_job(
        pb,
        plan_registry,
        "plan_article_images",
        {"projectId": project["id"], "articleId": article["id"]},
    )
    assert plan_result["version"] == 1
    assert plan_result["interiors"] == 2

    registry = FakeRegistry()
    patch_download(monkeypatch, pb)

    # 2) cover + 2 interiors
    payload = {"projectId": project["id"], "articleId": article["id"]}
    run_job(pb, FakeRegistry(), "generate_cover_image", dict(payload))
    for key in ("section-1", "section-2"):
        run_job(pb, FakeRegistry(), "generate_interior_image", {**payload, "sectionKey": key})

    # 3) all ready + optimized, then explicit re-optimize of the cover
    cover = latest_row(pb, article["id"], "cover")
    assert cover["status"] == "ready" and cover["optimizedFile"]
    run_job(
        pb,
        FakeRegistry(),
        "optimize_article_image",
        {"projectId": project["id"], "imageId": cover["id"]},
    )

    # 4) publish: upload media → featured → placeholders resolved
    result = run_publish(pb, registry, article["id"], project["id"], key="e2e1")
    content = registry.publisher.created[-1]["html"]
    assert "{{IMAGE:" not in content
    assert content.count("<img") == 3  # cover + 2 interiors inserted
    assert content.count("<figure") == 1  # figure wraps only the captioned cover
    assert registry.publisher.featured_calls[-1][0] == result["postId"]
    uploads = registry.publisher.media_uploads_of("")
    assert len(uploads) == 3

    # 5) retry (update) → zero new media uploads; featured re-set is idempotent
    #    (same media id, no duplicate media in WP)
    run_publish(
        pb, registry, article["id"], project["id"], key="e2e2", payload_extra={"action": "update"}
    )
    assert len(registry.publisher.media_uploads_of("")) == 3
    assert registry.publisher.featured_calls[-1] == registry.publisher.featured_calls[0]


def test_interiors_inserted_without_placeholders(monkeypatch):
    """Real LLM articles have no {{IMAGE:section-N}} markers — figures must be
    placed right after the matching section <h2> anyway."""
    pb = make_pb()
    project = make_project(pb)
    article = make_plan_article(pb, project["id"])
    from app.repositories.articles import ArticleRepo

    ArticleRepo(pb).update(
        article["id"],
        {
            "finalHtml": (
                "<h1>\u0639\u0646\u0648\u0627\u0646</h1><p>\u0645\u0642\u062f\u0645\u0647.</p>"
                "<h2>\u0628\u062e\u0634 \u0627\u0648\u0644</h2><p>\u0645\u062a\u0646 \u0627\u0648\u0644.</p>"
                "<h2>\u0628\u062e\u0634 \u062f\u0648\u0645</h2><p>\u0645\u062a\u0646 \u062f\u0648\u0645.</p>"
            ),
            "status": "approved",
        },
    )
    set_plan(pb, article["id"], ["section-1", "section-2"])
    article = pb.collection("articles").get_one(article["id"])
    make_images_ready(pb, project, article)
    patch_download(monkeypatch, pb)

    registry = FakeRegistry()
    run_publish(pb, registry, article["id"], project["id"], key="nip1")
    content = registry.publisher.created[-1]["html"]
    first_h2_end = content.index("</h2>") + len("</h2>")
    second_h2_end = content.index("</h2>", first_h2_end) + len("</h2>")
    figs = [i for i, ch in enumerate(content) if content.startswith("<img", i)]
    assert len(figs) == 2
    assert first_h2_end <= figs[0] < content.index("\u0628\u062e\u0634 \u062f\u0648\u0645")
    assert second_h2_end <= figs[1]
    assert 'loading="lazy"' in content


def test_interior_beyond_sections_appended(monkeypatch):
    """Section key with no matching <h2> → figure still lands (end of body)."""
    pb = make_pb()
    project = make_project(pb)
    article = make_plan_article(pb, project["id"])
    from app.repositories.articles import ArticleRepo

    ArticleRepo(pb).update(
        article["id"],
        {
            "finalHtml": "<h1>\u0639\u0646\u0648\u0627\u0646</h1><p>\u0645\u062a\u0646.</p>",
            "status": "approved",
        },
    )
    set_plan(pb, article["id"], ["section-3"])
    article = pb.collection("articles").get_one(article["id"])
    make_images_ready(pb, project, article)
    patch_download(monkeypatch, pb)

    registry = FakeRegistry()
    run_publish(pb, registry, article["id"], project["id"], key="nip2")
    content = registry.publisher.created[-1]["html"]
    assert "<img" in content and content.rstrip().endswith('decoding="async">')
