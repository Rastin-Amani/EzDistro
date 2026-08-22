"""Article generation engine tests — validators, assembly, chained pipeline."""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any

import pytest

from app.domain.article_html import build_article_html, normalize_article_html
from app.domain.article_validation import ArticleValidator, SectionValidator
from app.jobs.context import JobContext, ProviderStack
from app.providers.base import PermanentError, ProviderError
from app.repositories.jobs import JobEventRepo, JobRepo
from app.repositories.projects import DEFAULT_SETTINGS
from app.repositories.prompts import PromptRepo
from app.repositories.topics import TopicRepo
from app.services.settings import ProjectConfig
from app.services.writing import (
    handle_assemble_article,
    handle_generate_outline,
    handle_generate_section,
    handle_write_article,
)
from tests.fake_providers import FakeRegistry
from tests.fakes import FakePocketBase, default_unique_fields

OUTLINE_JSON = (
    '{"title": "راهنمای سئو", "slug": "rahnama-seo", "sections": ['
    '{"heading": "مقدمه", "content_brief": "نکته ۱", "internal_links": [{"title": "مقاله مرتبط", "url": "https://site.test/1", "anchor_text": "مقاله مرتبط"}]},'
    '{"heading": "تکنیکها", "content_brief": "نکته ۲"},'
    '{"heading": "نتیجهگیری", "content_brief": "جمعبندی"}]}'
)
SECTION_HTML = "<p>" + ("کلمه محتوای بخش " * 40).strip() + "</p>"


def make_project(pb: FakePocketBase) -> dict[str, Any]:
    project = pb.collection("projects").create(
        {
            "name": "پ",
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
        ("seo_rules", "قوانین."),
    ):
        PromptRepo(pb).save_version(
            project_id=project["id"], ptype=ptype, name="default", content=content
        )
    return project


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


def make_job(
    pb: FakePocketBase, project_id: str, type: str, payload: dict, key: str
) -> dict[str, Any]:
    return JobRepo(pb).create(
        project=project_id,
        type=type,
        payload=payload,
        idempotency_key=key,
        max_attempts=3,
        entity_type="section" if type == "generate_section" else "article",
        entity_id=payload.get("sectionId") or payload.get("articleId") or project_id,
    )


def run_write(pb: FakePocketBase, registry: FakeRegistry, topic_id: str) -> dict[str, Any]:
    import time

    job = make_job(
        pb,
        project_of(pb, topic_id),
        "write_article",
        {"topicId": topic_id},
        f"w:{topic_id}:{time.time()}",
    )
    return asyncio.run(handle_write_article(make_ctx(pb, registry, job)))


def project_of(pb: FakePocketBase, topic_id: str) -> str:
    topic = TopicRepo(pb).get(topic_id)
    return topic["project"]


# ---------------------------------------------------------------------------
# SectionValidator
# ---------------------------------------------------------------------------
def test_section_validator_rejects_dangerous_output():
    issues = SectionValidator().validate("<p>متن</p><script>alert(1)</script>")
    codes = {i.code for i in issues}
    assert "script" in codes

    issues = SectionValidator().validate('<p onclick="x()">متن</p>')
    assert "event_handler" in {i.code for i in issues}

    issues = SectionValidator().validate("<h1>تیتر</h1><p>متن</p>")
    assert "h1_in_section" in {i.code for i in issues}

    issues = SectionValidator().validate("<p>```json\n{}\n```</p>")
    assert "markdown_fence" in {i.code for i in issues}

    issues = SectionValidator().validate("<p>کوتاه</p>")
    assert "too_short" in {i.code for i in issues}

    assert SectionValidator().validate("")[0].code == "empty"


def test_section_validator_accepts_clean_output():
    html = "<p>" + ("کلمه محتوا " * 15).strip() + "</p>"
    assert SectionValidator().validate(html) == []


def test_section_validator_detects_unbalanced_tags():
    html = "<p>متن <strong>بولد</p>"
    issues = SectionValidator().validate(html)
    assert any(i.code == "html_structure" for i in issues)


# ---------------------------------------------------------------------------
# ArticleValidator
# ---------------------------------------------------------------------------
def test_article_validator_checks():
    validator = ArticleValidator(min_words=100)
    good_sections = [
        {"heading": "الف", "content": "<p>" + ("کلمه " * 60).strip() + "</p>"},
        {"heading": "ب", "content": "<p>" + ("کلمه " * 60).strip() + "</p>"},
    ]
    report = validator.validate(
        title="عنوان",
        slug="onvan",
        outline={"sections": []},
        sections=good_sections,
        html=build_article_html(title="عنوان", slug="onvan", sections=good_sections),
    )
    assert report.ok is True

    bad = validator.validate(
        title="",
        slug="",
        outline={"sections": []},
        sections=[{"heading": "الف", "content": ""}],
        html="<p>کوتاه</p>",
    )
    assert bad.ok is False
    codes = {i.code for i in bad.issues}
    assert "missing_title" in codes
    assert "missing_slug" in codes
    assert "empty_section" in codes
    assert "too_short" in codes


def test_article_validator_missing_intended_link():
    validator = ArticleValidator(min_words=50)
    sections = [{"heading": "الف", "content": "<p>" + ("کلمه " * 40).strip() + "</p>"}]
    outline = {
        "sections": [
            {
                "internal_links": [
                    {"title": "لینک", "url": "https://site.test/9", "anchor_text": "لینک"}
                ]
            }
        ]
    }
    html = build_article_html(title="ت", slug="t", sections=sections)  # link NOT included
    report = validator.validate(title="ت", slug="t", outline=outline, sections=sections, html=html)
    assert any(i.code == "missing_internal_link" for i in report.issues)


# ---------------------------------------------------------------------------
# Assembly — deterministic
# ---------------------------------------------------------------------------
def test_assembly_order_dedupe_links_no_separators():
    html = build_article_html(
        title="عنوان",
        slug="onvan",
        sections=[
            {"heading": "تکرار", "content": "<p>اولین</p>"},
            {"heading": "تکرار", "content": "<p>دومین</p>"},
        ],
        internal_links=[{"title": "لینک", "url": "https://site.test/1", "anchor_text": "لینک"}],
    )
    assert html.index("<h2>تکرار</h2>") < html.index("<h2>تکرار (2)</h2>")
    assert 'href="https://site.test/1"' in html
    assert "<br>" not in html and "<hr>" not in html
    assert "مطالب مرتبط" in html


def test_assembly_normalizes_fences_and_whitespace():
    raw = "<p>متن   اول</p>\n\n\n\n<p>دوم</p>"
    assert "   " not in normalize_article_html(raw)
    assert "\n\n\n\n" not in normalize_article_html(raw)


def test_write_article_accepts_queued_topic():
    """Regression: the write endpoints set topics to `queued` before dispatching
    write_article, so the handler must accept it as a start state."""
    pb = FakePocketBase(default_unique_fields())
    project = make_project(pb)
    topic = TopicRepo(pb).create(project=project["id"], title="ت", keyword="سئو")
    TopicRepo(pb).set_status(topic["id"], "queued")
    registry = FakeRegistry()
    registry.llm.responses = [OUTLINE_JSON] + [SECTION_HTML] * 3

    result = run_write(pb, registry, topic["id"])

    # The queued topic entered the write pipeline (outline generated, sections queued)
    # instead of being rejected as "unexpected state: queued".
    article = pb.collection("articles").get_one(result["articleId"])
    assert article["status"] == "generating"
    assert TopicRepo(pb).get(topic["id"])["status"] == "writing"
    assert pb.collection("jobs").get_first_list_item('type="generate_section"')


# ---------------------------------------------------------------------------
# Chained pipeline — independent sections, wait, validation
# ---------------------------------------------------------------------------
def test_section_failure_does_not_affect_other_sections():
    pb = FakePocketBase(default_unique_fields())
    project = make_project(pb)
    topic = TopicRepo(pb).create(project=project["id"], title="ت", keyword="سئو")
    registry = FakeRegistry()
    # outline + section 1 ok + section 2 garbage (validation-failing) + section 3 ok
    registry.llm.responses = [OUTLINE_JSON, SECTION_HTML, "<p>خالی</p>", SECTION_HTML]

    result = run_write(pb, registry, topic["id"])
    section_jobs = pb.collection("jobs").get_full_list(
        {"filter": 'type="generate_section"', "sort": "created"}
    )
    assert len(section_jobs) == 3

    # execute all section jobs — the middle one fails independently
    for job in section_jobs:
        with contextlib.suppress(ProviderError):
            asyncio.run(handle_generate_section(make_ctx(pb, registry, job)))

    sections = pb.collection("article_sections").get_full_list({"sort": "position"})
    assert sections[0]["status"] == "done"
    assert sections[1]["status"] == "failed"  # isolated failure
    assert sections[2]["status"] == "done"  # later sections unaffected

    # assembler refuses to assemble while a section failed (deterministic)
    assemble_job = pb.collection("jobs").get_first_list_item('type="assemble_article"')
    with pytest.raises(ProviderError):
        asyncio.run(handle_assemble_article(make_ctx(pb, registry, assemble_job)))
    assert pb.collection("articles").get_one(result["articleId"])["status"] == "failed"

    # fixing the failed section (independent retry) lets assembly proceed
    registry.llm.responses.append(SECTION_HTML)
    asyncio.run(handle_generate_section(make_ctx(pb, registry, section_jobs[1])))
    asyncio.run(handle_assemble_article(make_ctx(pb, registry, assemble_job)))
    article = pb.collection("articles").get_one(result["articleId"])
    assert article["status"] == "review"
    assert article["validation"]["ok"] is True


def test_assembler_waits_for_pending_sections():
    pb = FakePocketBase(default_unique_fields())
    project = make_project(pb)
    topic = TopicRepo(pb).create(project=project["id"], title="ت", keyword="سئو")
    registry = FakeRegistry()
    registry.llm.responses = [OUTLINE_JSON] + [SECTION_HTML] * 3

    result = run_write(pb, registry, topic["id"])
    assemble_job = pb.collection("jobs").get_first_list_item('type="assemble_article"')

    # sections still pending → assemble raises a TRANSIENT error (retry scheduled)
    with pytest.raises(ProviderError) as excinfo:
        asyncio.run(handle_assemble_article(make_ctx(pb, registry, assemble_job)))
    assert excinfo.value.retryable is True
    assert excinfo.value.details.get("retry_after_seconds") == 20

    # after sections complete, assemble succeeds
    for job in pb.collection("jobs").get_full_list({"filter": 'type="generate_section"'}):
        asyncio.run(handle_generate_section(make_ctx(pb, registry, job)))
    asyncio.run(handle_assemble_article(make_ctx(pb, registry, assemble_job)))
    assert pb.collection("articles").get_one(result["articleId"])["status"] == "review"


def test_sections_record_prompt_version():
    pb = FakePocketBase(default_unique_fields())
    project = make_project(pb)
    topic = TopicRepo(pb).create(project=project["id"], title="ت", keyword="سئو")
    registry = FakeRegistry()
    registry.llm.responses = [OUTLINE_JSON] + [SECTION_HTML] * 3

    run_write(pb, registry, topic["id"])
    for job in pb.collection("jobs").get_full_list({"filter": 'type="generate_section"'}):
        asyncio.run(handle_generate_section(make_ctx(pb, registry, job)))

    sections = pb.collection("article_sections").get_full_list({"sort": "position"})
    assert all(s["promptVersion"] >= 1 for s in sections)  # active section_user prompt v1
    assert all(s["provider"] == "fake" and s["model"] == "fake-model" for s in sections)
    assert all(s["generationAttempts"] == 1 for s in sections)


def test_validate_publish_only_when_requested():
    """The pipeline never auto-publishes; publish is a separate explicit job."""
    pb = FakePocketBase(default_unique_fields())
    project = make_project(pb)
    topic = TopicRepo(pb).create(project=project["id"], title="ت", keyword="سئو")
    registry = FakeRegistry()
    registry.llm.responses = [OUTLINE_JSON] + [SECTION_HTML] * 3

    run_write(pb, registry, topic["id"])
    assert pb.collection("jobs").get_full_list({"filter": 'type="publish_article"'}) == []
