"""Article review workflow tests: approval gating, send-back, regeneration
preserving history, revision snapshot/rollback, and the new validator checks."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from app.domain.article_validation import ArticleValidator
from app.jobs.context import JobContext, ProviderStack
from app.providers.base import ProviderError
from app.repositories.articles import ArticleRepo, SectionRepo
from app.repositories.jobs import JobEventRepo, JobRepo
from app.repositories.projects import DEFAULT_SETTINGS
from app.repositories.prompts import PromptRepo
from app.repositories.topics import TopicRepo
from app.services.publishing_service import handle_publish_article
from app.services.revisions import RevisionService
from app.services.settings import ProjectConfig
from app.services.writing import handle_generate_section, handle_write_article
from tests.fake_providers import FakeRegistry
from tests.fakes import FakePocketBase, default_unique_fields


def make_project(pb: FakePocketBase) -> dict[str, Any]:
    project = pb.collection("projects").create(
        {
            "name": "\u067e",
            "slug": "p1",
            "language": "fa",
            "status": "active",
            "timezone": "Asia/Tehran",
        }
    )
    pb.collection("project_settings").create({"project": project["id"], **DEFAULT_SETTINGS})
    return project


def make_article(pb: FakePocketBase, project_id: str, status: str = "review") -> dict[str, Any]:
    topic = TopicRepo(pb).create(
        project=project_id, title="\u0633\u0626\u0648", keyword="\u0633\u0626\u0648"
    )
    article = pb.collection("articles").create(
        {
            "project": project_id,
            "topicId": topic["id"],
            "title": "\u0631\u0627\u0647\u0646\u0645\u0627\u06cc \u0633\u0626\u0648",
            "slug": "rahnama-seo",
            "status": status,
            "outlineVersion": 1,
            "outline": {
                "title": "\u0631\u0627\u0647\u0646\u0645\u0627\u06cc \u0633\u0626\u0648",
                "slug": "rahnama-seo",
                "sections": [
                    {
                        "heading": "\u0645\u0642\u062f\u0645\u0647",
                        "content_brief": "\u062e\u0644\u0627\u0635\u0647 \u0645\u0642\u062f\u0645\u0647",
                        "internal_links": [],
                    },
                    {
                        "heading": "\u062a\u06a9\u0646\u06cc\u06a9\u0647\u0627",
                        "content_brief": "\u062e\u0644\u0627\u0635\u0647 \u062a\u06a9\u0646\u06cc\u06a9\u0647\u0627",
                        "internal_links": [],
                    },
                ],
            },
            "finalHtml": "<h1>\u0631\u0627\u0647\u0646\u0645\u0627\u06cc \u0633\u0626\u0648</h1><h2>\u0645\u0642\u062f\u0645\u0647</h2><p>"
            + ("\u06a9\u0644\u0645\u0647 " * 200).strip()
            + "</p><h2>\u062a\u06a9\u0646\u06cc\u06a9\u0647\u0627</h2><p>"
            + ("\u06a9\u0644\u0645\u0647 " * 200).strip()
            + "</p>",
            "metaDescription": "\u062e\u0644\u0627\u0635\u0647",
            "wordCount": 400,
            "seoScore": 70,
        }
    )
    for i, plan in enumerate(article["outline"]["sections"]):
        pb.collection("article_sections").create(
            {
                "article": article["id"],
                "position": i,
                "heading": plan["heading"],
                "contentBrief": plan["content_brief"],
                "internalLinks": [],
                "status": "done",
                "content": "<p>" + ("\u06a9\u0644\u0645\u0647 " * 200).strip() + "</p>",
                "generationAttempts": 1,
                "promptVersion": 1,
                "provider": "openai_compat",
                "model": "gpt-4o-mini",
                "generationLatency": 100,
            }
        )
    return pb.collection("articles").get_one(article["id"])


def make_ctx(pb: FakePocketBase, registry: FakeRegistry, job: dict[str, Any]) -> JobContext:
    config = ProjectConfig.load(pb, job["project"])
    return JobContext(
        pb=pb,
        job=job,
        config=config,
        providers=ProviderStack(registry=registry, config=config),
        registry=registry,
        events=JobEventRepo(pb),
        set_progress=lambda jid, pct, **kw: None,
        request_cancel=lambda jid: False,
    )


# ---------------------------------------------------------------------------
# Validator: new checks
# ---------------------------------------------------------------------------
def test_validator_flags_duplicate_headings_broken_links_suspicious():
    html = (
        "<h2>\u062a\u06a9\u0631\u0627\u0631</h2><p>\u0645\u062a\u0646</p><h2>\u062a\u06a9\u0631\u0627\u0631</h2><p>\u0645\u062a\u0646</p>"
        '<a href="javascript:alert(1)">x</a>'
        "<p>{{ unrendered }}</p><p>lorem ipsum</p>"
    )
    report = ArticleValidator(min_words=10).validate(
        title="\u062a",
        slug="t",
        outline={},
        sections=[{"heading": "\u0627\u0644\u0641", "content": "<p>\u0645\u062a\u0646</p>"}],
        html=html,
    )
    codes = {i.code for i in report.issues}
    assert "duplicate_heading" in codes
    assert "broken_internal_link" in codes
    assert "suspicious_output" in codes


def test_validator_keyword_requirements():
    html = "<h2>\u0628\u062e\u0634</h2><p>" + ("\u06a9\u0644\u0645\u0647 " * 50).strip() + "</p>"
    report = ArticleValidator(min_words=10, keyword="\u0633\u0626\u0648").validate(
        title="\u0628\u062f\u0648\u0646 \u06a9\u0644\u06cc\u062f\u0648\u0627\u0698\u0647",
        slug="t",
        outline={},
        sections=[{"heading": "\u0628", "content": "<p>x</p>"}],
        html=html,
    )
    codes = {i.code for i in report.issues}
    assert "keyword_in_title" in codes
    assert "keyword_absent" in codes


# ---------------------------------------------------------------------------
# Revision service
# ---------------------------------------------------------------------------
def test_revision_snapshot_and_rollback_never_destroys_previous():
    pb = FakePocketBase(default_unique_fields())
    project = make_project(pb)
    article = make_article(pb, project["id"])
    service = RevisionService(pb)

    v1 = service.snapshot(article, "generated", note="first")
    # mutate the article (simulate regeneration)
    pb.collection("articles").update(
        article["id"],
        {
            "title": "\u0639\u0646\u0648\u0627\u0646 \u062c\u062f\u06cc\u062f",
            "finalHtml": "<h1>\u062c\u062f\u06cc\u062f</h1>",
        },
    )
    changed = pb.collection("articles").get_one(article["id"])
    v2 = service.snapshot(changed, "generated", note="second")

    assert v1["revision"] == 1
    assert v2["revision"] == 2
    assert len(service.list(article["id"])) == 2  # history grows, nothing destroyed

    # rollback to v1 restores the old title + sections
    restored = service.rollback(article["id"], v1["id"], created_by="reviewer")
    assert restored["title"] == "\u0631\u0627\u0647\u0646\u0645\u0627\u06cc \u0633\u0626\u0648"
    assert "\u0631\u0627\u0647\u0646\u0645\u0627\u06cc \u0633\u0626\u0648" in (
        restored.get("finalHtml") or ""
    )
    # rollback itself is recorded (3 revisions now)
    assert len(service.list(article["id"])) == 3
    # sections restored with content
    rows = SectionRepo(pb).list_for_article(article["id"])
    assert all(r["status"] == "done" and r["content"] for r in rows)


def test_rollback_unknown_revision_raises():
    pb = FakePocketBase(default_unique_fields())
    project = make_project(pb)
    article = make_article(pb, project["id"])
    service = RevisionService(pb)
    with pytest.raises(ValueError):
        service.rollback(article["id"], "nope", created_by="x")


# ---------------------------------------------------------------------------
# Publishing gate
# ---------------------------------------------------------------------------
def test_publish_requires_approved_state():
    pb = FakePocketBase(default_unique_fields())
    project = make_project(pb)
    article = make_article(pb, project["id"], status="review")
    registry = FakeRegistry()
    job = JobRepo(pb).create(
        project=project["id"],
        type="publish_article",
        payload={"articleId": article["id"]},
        idempotency_key="pub-gate-1",
        max_attempts=3,
    )
    with pytest.raises(ProviderError) as excinfo:
        asyncio.run(handle_publish_article(make_ctx(pb, registry, job)))
    assert excinfo.value.retryable is False
    assert excinfo.value.details.get("required_state") == "approved"
    assert registry.publisher.created == []  # WordPress never called

    # approved → publishes
    pb.collection("articles").update(article["id"], {"status": "approved"})
    job2 = JobRepo(pb).create(
        project=project["id"],
        type="publish_article",
        payload={"articleId": article["id"]},
        idempotency_key="pub-gate-2",
        max_attempts=3,
    )
    result = asyncio.run(handle_publish_article(make_ctx(pb, registry, job2)))
    assert result["postId"]
    assert len(registry.publisher.created) == 1


# ---------------------------------------------------------------------------
# Regeneration preserves history
# ---------------------------------------------------------------------------
OUTLINE_JSON = (
    '{"title": "\u0631\u0627\u0647\u0646\u0645\u0627\u06cc \u0633\u0626\u0648", "slug": "rahnama-seo", "sections": ['
    '{"heading": "\u0645\u0642\u062f\u0645\u0647", "content_brief": "\u062e\u0644\u0627\u0635\u0647 \u0645\u0642\u062f\u0645\u0647"},'
    '{"heading": "\u062a\u06a9\u0646\u06cc\u06a9\u0647\u0627", "content_brief": "\u062e\u0644\u0627\u0635\u0647 \u062a\u06a9\u0646\u06cc\u06a9\u0647\u0627"}]}'
)


def test_regenerate_article_preserves_previous_version():
    pb = FakePocketBase(default_unique_fields())
    project = make_project(pb)
    for ptype, content in (
        (
            "brand_voice",
            "\u062a\u0648 \u0646\u0648\u06cc\u0633\u0646\u062f\u0647 \u0633\u0626\u0648 \u0647\u0633\u062a\u06cc.",
        ),
        ("outline_user", "JSON \u0628\u0631\u06af\u0631\u062f\u0627\u0646."),
        ("section_user", "HTML \u0628\u0631\u06af\u0631\u062f\u0627\u0646."),
        ("seo_rules", "\u0642\u0648\u0627\u0646\u06cc\u0646."),
    ):
        PromptRepo(pb).save_version(
            project_id=project["id"], ptype=ptype, name="default", content=content
        )

    article = make_article(pb, project["id"], status="sent_back")
    pb.collection("articles").update(
        article["id"],
        {
            "finalHtml": "<h1>\u0646\u0633\u062e\u0647 \u0642\u0628\u0644\u06cc</h1>",
            "title": "\u0646\u0633\u062e\u0647 \u0642\u0628\u0644\u06cc",
        },
    )
    registry = FakeRegistry()
    registry.llm.responses = [OUTLINE_JSON] + [
        "<p>" + ("\u06a9\u0644\u0645\u0647 " * 200).strip() + "</p>"
    ] * 2

    # regenerate from sent_back
    job = JobRepo(pb).create(
        project=project["id"],
        type="write_article",
        payload={"topicId": article["topicId"], "regenerate": True},
        idempotency_key=f"write:article:{article['topicId']}:regen:1",
        max_attempts=3,
    )
    result = asyncio.run(handle_write_article(make_ctx(pb, registry, job)))
    assert result["articleId"] == article["id"]

    # the previous version was checkpointed before regeneration
    revisions = RevisionService(pb).list(article["id"])
    kinds = [r["kind"] for r in revisions]
    assert "checkpoint" in kinds
    checkpoint = next(r for r in revisions if r["kind"] == "checkpoint")
    assert checkpoint["snapshot"]["title"] == "\u0646\u0633\u062e\u0647 \u0642\u0628\u0644\u06cc"
    assert (
        "\u0646\u0633\u062e\u0647 \u0642\u0628\u0644\u06cc" in checkpoint["snapshot"]["finalHtml"]
    )


def test_regenerate_from_review_proceeds_not_already_written():
    """Regression: regenerating an article whose topic is already `review`
    must actually run — it used to short-circuit with `alreadyWritten`, leaving
    the article stuck in `generating` while the topic stayed `review`."""
    pb = FakePocketBase(default_unique_fields())
    project = make_project(pb)
    for ptype, content in (
        (
            "brand_voice",
            "\u062a\u0648 \u0646\u0648\u06cc\u0633\u0646\u062f\u0647 \u0633\u0626\u0648 \u0647\u0633\u062a\u06cc.",
        ),
        ("outline_user", "JSON \u0628\u0631\u06af\u0631\u062f\u0627\u0646."),
        ("section_user", "HTML \u0628\u0631\u06af\u0631\u062f\u0627\u0646."),
        ("seo_rules", "\u0642\u0648\u0627\u0646\u06cc\u0646."),
    ):
        PromptRepo(pb).save_version(
            project_id=project["id"], ptype=ptype, name="default", content=content
        )

    article = make_article(pb, project["id"], status="review")
    # the split state the user reported: topic review, article stuck generating
    TopicRepo(pb).set_status(article["topicId"], "review")

    registry = FakeRegistry()
    registry.llm.responses = [OUTLINE_JSON] + [
        "<p>" + ("\u06a9\u0644\u0645\u0647 " * 200).strip() + "</p>"
    ] * 2
    job = JobRepo(pb).create(
        project=project["id"],
        type="write_article",
        payload={"topicId": article["topicId"], "regenerate": True},
        idempotency_key=f"write:article:{article['topicId']}:regen:review",
        max_attempts=3,
    )
    result = asyncio.run(handle_write_article(make_ctx(pb, registry, job)))
    assert result["articleId"] == article["id"]
    assert result.get("alreadyWritten") is not True
    assert result["sectionJobs"]  # regeneration actually dispatched sections
    assert TopicRepo(pb).get(article["topicId"])["status"] == "writing"
    assert ArticleRepo(pb).get(article["id"])["status"] == "generating"


def test_section_regeneration_restores_article_status():
    """Regression: a per-section regeneration leaves the article `generating`;
    once the section finishes and no assembler is waiting, it must return to
    `review` instead of staying stuck."""
    pb = FakePocketBase(default_unique_fields())
    project = make_project(pb)
    for ptype, content in (
        (
            "brand_voice",
            "\u062a\u0648 \u0646\u0648\u06cc\u0633\u0646\u062f\u0647 \u0633\u0626\u0648 \u0647\u0633\u062a\u06cc.",
        ),
        ("outline_user", "JSON \u0628\u0631\u06af\u0631\u062f\u0627\u0646."),
        ("section_user", "HTML \u0628\u0631\u06af\u0631\u062f\u0627\u0646."),
        ("seo_rules", "\u0642\u0648\u0627\u0646\u06cc\u0646."),
    ):
        PromptRepo(pb).save_version(
            project_id=project["id"], ptype=ptype, name="default", content=content
        )
    article = make_article(pb, project["id"], status="review")
    section = pb.collection("article_sections").get_first_list_item(f'article="{article["id"]}"')
    # simulate the per-section regenerate endpoint
    ArticleRepo(pb).set_status(article["id"], "generating")
    pb.collection("article_sections").update(section["id"], {"status": "pending", "content": ""})

    registry = FakeRegistry()
    registry.llm.responses = ["<p>" + ("\u06a9\u0644\u0645\u0647 " * 200).strip() + "</p>"]
    job = JobRepo(pb).create(
        project=project["id"],
        type="generate_section",
        payload={"sectionId": section["id"]},
        idempotency_key=f"gen:section:{section['id']}",
        max_attempts=3,
    )
    asyncio.run(handle_generate_section(make_ctx(pb, registry, job)))
    assert ArticleRepo(pb).get(article["id"])["status"] == "review"


# ---------------------------------------------------------------------------
# Approve gate: invalid article cannot be approved
# ---------------------------------------------------------------------------
def test_approve_requires_valid_article():
    from app.api.workspace import _live_validation

    pb = FakePocketBase(default_unique_fields())
    project = make_project(pb)
    article = make_article(pb, project["id"])
    # break the article: empty body
    pb.collection("articles").update(article["id"], {"finalHtml": "", "title": ""})
    broken = pb.collection("articles").get_one(article["id"])
    sections = SectionRepo(pb).list_for_article(article["id"])
    report = _live_validation(pb, broken, sections, "\u0633\u0626\u0648")
    assert report.ok is False
    codes = {i.code for i in report.issues}
    assert "missing_title" in codes
    assert "too_short" in codes
