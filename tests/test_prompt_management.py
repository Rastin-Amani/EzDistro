"""Prompt management tests: safe rendering, versioning (activate/rollback/
duplicate), interactive tester, and data-driven writer behaviour."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from app.domain.prompt_render import (
    PromptRenderError,
    format_internal_links,
    render_prompt,
    used_variables,
    validate_prompt,
)
from app.jobs.context import JobContext, ProviderStack
from app.repositories.jobs import JobEventRepo, JobRepo
from app.repositories.projects import DEFAULT_SETTINGS
from app.repositories.prompts import PromptRepo
from app.repositories.topics import TopicRepo
from app.services.prompt_service import PromptService
from app.services.settings import ProjectConfig
from app.services.writing import handle_write_article
from tests.fake_providers import FakeRegistry
from tests.fakes import FakePocketBase, default_unique_fields


def make_pb() -> FakePocketBase:
    return FakePocketBase(default_unique_fields())


def make_project(pb: FakePocketBase) -> dict[str, Any]:
    project = pb.collection("projects").create(
        {
            "name": "پروژه سئو",
            "slug": "seo-proj",
            "language": "fa",
            "status": "active",
            "timezone": "Asia/Tehran",
        }
    )
    pb.collection("project_settings").create({"project": project["id"], **DEFAULT_SETTINGS})
    return project


# ---------------------------------------------------------------------------
# Safe rendering
# ---------------------------------------------------------------------------
def test_render_substitutes_known_variables():
    out = render_prompt(
        "مقاله: {{ article.title }} — کلمه کلیدی: {{ topic.keyword }}",
        {"article.title": "عنوان", "topic.keyword": "سئو"},
    )
    assert out == "مقاله: عنوان — کلمه کلیدی: سئو"


def test_render_unknown_variable_fails():
    with pytest.raises(PromptRenderError) as excinfo:
        render_prompt("{{ nope.var }}", {})
    assert "nope.var" in excinfo.value.unknown

    # validation helper (save-time gate)
    assert validate_prompt("{{ topic.title }} {{ seo_rules }}") == []
    assert validate_prompt("{{ evil.code }}") == ["evil.code"]


def test_render_unclosed_token_fails():
    with pytest.raises(PromptRenderError):
        render_prompt("{{ topic.title", {})


def test_render_missing_known_variable_renders_empty():
    out = render_prompt("x={{ article.meta_description }}y", {})
    assert out == "x=y"  # the "=" is literal content; the token renders empty


def test_render_is_pure_substitution_no_code_execution():
    # Jinja/Python constructs must stay as literal text
    payload = {"topic.title": "ت"}
    out = render_prompt("{{ topic.title }} {% if true %}x{% endif %} {{ 1+1 }}", payload)
    assert "{% if true %}x{% endif %}" in out
    assert "{{ 1+1 }}" in out
    assert out.startswith("ت")


def test_used_variables_and_link_format():
    assert used_variables("a {{ topic.title }} b {{ topic.keyword }}") == [
        "topic.title",
        "topic.keyword",
    ]
    links = [{"title": "الف", "url": "https://x.com/1"}, {"title": "ب", "url": "https://x.com/2"}]
    formatted = format_internal_links(links)
    assert "- الف (https://x.com/1)" in formatted
    assert format_internal_links([]) == ""


# ---------------------------------------------------------------------------
# Versioning: activate / rollback / duplicate
# ---------------------------------------------------------------------------
def test_versioning_lifecycle():
    pb = make_pb()
    project = make_project(pb)
    service = PromptService(pb)

    v1 = service.save(project["id"], "seo_rules", "قوانین نسخه ۱", author="user-a")
    v2 = service.save(project["id"], "seo_rules", "قوانین نسخه ۲", author="user-b")
    assert v2["version"] == 2
    # re-fetch: v1 was deactivated when v2 became active
    rows = PromptRepo(pb).history(project["id"], "seo_rules")
    by_version = {r["version"]: r for r in rows}
    assert by_version[1]["active"] is False
    assert by_version[2]["active"] is True

    # rollback: activate v1
    service.activate(project["id"], "seo_rules", v1["id"])
    rows = PromptRepo(pb).history(project["id"], "seo_rules")
    by_version = {r["version"]: r for r in rows}
    assert by_version[1]["active"] is True
    assert by_version[2]["active"] is False
    assert service.resolve_active(project["id"], "seo_rules")["version"] == 1

    # duplicate: new inactive version from v2
    dup = service.duplicate(project["id"], "seo_rules", v2["id"], author="user-a")
    assert dup["version"] == 3
    assert dup["active"] is False
    assert dup["content"] == "قوانین نسخه ۲"
    assert (dup.get("variables") or {}).get("duplicatedFrom") == v2["id"]
    # active unchanged after duplicate
    assert service.resolve_active(project["id"], "seo_rules")["version"] == 1


def test_save_rejects_unknown_variables_before_persisting():
    pb = make_pb()
    project = make_project(pb)
    service = PromptService(pb)
    with pytest.raises(PromptRenderError):
        service.save(project["id"], "seo_rules", "{{ bogus.var }}")
    # nothing persisted
    assert PromptRepo(pb).history(project["id"], "seo_rules") == []


def test_save_records_used_variables():
    pb = make_pb()
    project = make_project(pb)
    service = PromptService(pb)
    saved = service.save(
        project["id"], "outline_user", "{{ topic.title }} و {{ seo_rules }}", author="a"
    )
    assert saved["variables"]["used"] == ["topic.title", "seo_rules"]


# ---------------------------------------------------------------------------
# PromptService context
# ---------------------------------------------------------------------------
def test_build_context_and_render():
    pb = make_pb()
    project = make_project(pb)
    for ptype, content in (
        ("seo_rules", "کلمه کلیدی: {{ topic.keyword }}"),
        ("internal_linking", "قوانین لینک"),
    ):
        PromptRepo(pb).save_version(
            project_id=project["id"], ptype=ptype, name="default", content=content
        )
    config = ProjectConfig.load(pb, project["id"])
    service = PromptService(pb)

    context = service.build_context(
        config,
        topic={"title": "راهنمای سئو", "keyword": "سئو", "pillar": "پ", "cluster": "خ"},
        retrieval_context="متن مرتبط...",
        internal_links=[{"title": "لینک", "url": "https://x.com/1"}],
    )
    assert context["topic.title"] == "راهنمای سئو"
    assert "https://x.com/1" in context["internal_links"]
    assert "کلمه کلیدی: سئو" in context["seo_rules"]  # nested rules resolved

    rendered = service.render("{{ project.name }} — {{ topic.title }} — {{ seo_rules }}", context)
    assert rendered == "پروژه سئو — راهنمای سئو — کلمه کلیدی: سئو"


# ---------------------------------------------------------------------------
# Interactive tester (never touches stored versions)
# ---------------------------------------------------------------------------
def test_tester_renders_runs_and_validates():
    pb = make_pb()
    project = make_project(pb)
    topic = TopicRepo(pb).create(project=project["id"], title="سئو", keyword="سئو")
    config = ProjectConfig.load(pb, project["id"])
    registry = FakeRegistry()
    registry.llm.responses = [
        '{"title": "ت", "slug": "t", "sections": [{"heading": "الف", "content_brief": "ب"}, {"heading": "ج", "content_brief": "د"}]}'
    ]
    service = PromptService(pb, registry)

    result = asyncio.run(
        service.test(
            config,
            ptype="outline_user",
            content="موضوع: {{ topic.title }} — JSON بده.",
            topic=topic,
            model_role="outline",
        )
    )
    assert result["rendered"] == "موضوع: سئو — JSON بده."
    assert result["model"] == "fake-model"
    assert result["latency_ms"] >= 0
    assert result["usage"]["completion_tokens"] == 5
    assert result["validation"]["ok"] is True
    assert result["validation"]["sections"] == 2

    # stored versions untouched by the test
    assert PromptRepo(pb).history(project["id"], "outline_user") == []


def test_tester_section_validation():
    pb = make_pb()
    project = make_project(pb)
    config = ProjectConfig.load(pb, project["id"])
    registry = FakeRegistry()
    registry.llm.responses = ["<p>کوتاه</p>"]
    service = PromptService(pb, registry)

    result = asyncio.run(
        service.test(config, ptype="section_user", content="بنویس.", model_role="section")
    )
    assert result["validation"]["ok"] is False
    assert any(i["code"] == "too_short" for i in result["validation"]["issues"])


# ---------------------------------------------------------------------------
# Writer: prompts are data
# ---------------------------------------------------------------------------
def test_writer_renders_variable_driven_outline_prompt():
    pb = make_pb()
    project = make_project(pb)
    for ptype, content in (
        ("brand_voice", "تو نویسنده سئو هستی."),
        (
            "outline_user",
            "JSON برگردان برای موضوع «{{ topic.title }}» با کلیدواژه «{{ topic.keyword }}».",
        ),
        ("section_user", "HTML برگردان."),
        ("seo_rules", "قوانین."),
    ):
        PromptRepo(pb).save_version(
            project_id=project["id"], ptype=ptype, name="default", content=content
        )

    topic = TopicRepo(pb).create(
        project=project["id"], title="راهنمای سئو", keyword="سئو", pillar="پ"
    )
    registry = FakeRegistry()
    registry.llm.responses = [
        '{"title": "ت", "slug": "t", "sections": [{"heading": "الف", "content_brief": "ب"}, {"heading": "ج", "content_brief": "د"}]}'
    ]
    job = JobRepo(pb).create(
        project=project["id"],
        type="write_article",
        payload={"topicId": topic["id"]},
        idempotency_key="pw-1",
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

    outline_call = registry.llm.calls[0]["user"]
    assert "موضوع «راهنمای سئو» با کلیدواژه «سئو»" in outline_call
    assert "{{ topic.title }}" not in outline_call  # rendered, not literal


def test_writer_unknown_variable_fails_permanently():
    pb = make_pb()
    project = make_project(pb)
    for ptype, content in (
        ("outline_user", "JSON برگردان. {{ unknown.var }}"),
        ("section_user", "HTML برگردان."),
    ):
        PromptRepo(pb).save_version(
            project_id=project["id"], ptype=ptype, name="default", content=content
        )

    topic = TopicRepo(pb).create(project=project["id"], title="ت", keyword="ک")
    registry = FakeRegistry()
    job = JobRepo(pb).create(
        project=project["id"],
        type="write_article",
        payload={"topicId": topic["id"]},
        idempotency_key="pw-2",
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
    from app.providers.base import ProviderError

    with pytest.raises(ProviderError) as excinfo:
        asyncio.run(handle_write_article(ctx))
    assert excinfo.value.retryable is False  # configuration error, no retry
    assert excinfo.value.details.get("unknown") == ["unknown.var"]
    assert pb.collection("topics").get_one(topic["id"])["status"] == "failed"
