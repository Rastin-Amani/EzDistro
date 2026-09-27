"""Auto-publish pipeline tests: schedule-triggered articles publish straight
to WordPress when the SEO score is high enough; otherwise they rewrite until
the score passes or max attempts are exhausted (then left for review)."""

from __future__ import annotations

import asyncio
from typing import Any

from app.jobs.context import JobContext, ProviderStack
from app.repositories.jobs import JobEventRepo, JobRepo
from app.repositories.projects import DEFAULT_SETTINGS
from app.repositories.prompts import PromptRepo
from app.repositories.topics import TopicRepo
from app.services.settings import ProjectConfig
from app.services.writing import (
    handle_assemble_article,
    handle_generate_section,
    handle_write_article,
)
from tests.fake_providers import FakeRegistry
from tests.fakes import FakePocketBase, default_unique_fields

OUTLINE_JSON = (
    '{"title": "\u0631\u0627\u0647\u0646\u0645\u0627\u06cc \u0633\u0626\u0648", "slug": "rahnama-seo", "sections": ['
    '{"heading": "\u0645\u0642\u062f\u0645\u0647", "content_brief": "\u0646\u06a9\u062a\u0647 \u06f1"},'
    '{"heading": "\u062a\u06a9\u0646\u06cc\u06a9\u0647\u0627", "content_brief": "\u0646\u06a9\u062a\u0647 \u06f2"},'
    '{"heading": "\u0646\u062a\u06cc\u062c\u0647\u06af\u06cc\u0631\u06cc", "content_brief": "\u062c\u0645\u0639\u0628\u0646\u062f\u06cc"}]}'
)
SECTION_HTML = (
    "<p>"
    + (
        "\u06a9\u0644\u0645\u0647 \u0645\u062d\u062a\u0648\u0627\u06cc \u0628\u062e\u0634 " * 40
    ).strip()
    + "</p>"
)


def _project(pb: FakePocketBase, *, auto: dict[str, Any] | None = None) -> dict[str, Any]:
    project = pb.collection("projects").create(
        {
            "name": "\u067e\u0631\u0648\u0698\u0647",
            "slug": "proj-a",
            "language": "fa",
            "status": "active",
            "timezone": "Asia/Tehran",
        }
    )
    settings = {**DEFAULT_SETTINGS}
    if auto is not None:
        settings["autoPublish"] = {**settings["autoPublish"], **auto}
    pb.collection("project_settings").create({"project": project["id"], **settings})
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


def _ctx(pb: FakePocketBase, registry: FakeRegistry, job: dict[str, Any]) -> JobContext:
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


def _write_and_assemble(
    pb: FakePocketBase,
    registry: FakeRegistry,
    topic_id: str,
    project_id: str,
    *,
    trigger: str = "schedule",
    auto_attempts: int = 1,
) -> dict[str, Any]:
    import time

    job = JobRepo(pb).create(
        project=project_id,
        type="write_article",
        payload={
            "topicId": topic_id,
            "trigger": trigger,
            "autoAttempts": auto_attempts,
        },
        idempotency_key=f"w:{topic_id}:{time.time()}",
        max_attempts=3,
    )
    asyncio.run(handle_write_article(_ctx(pb, registry, job)))

    # run all section jobs, then the assembler
    for sj in pb.collection("jobs").get_full_list({"filter": 'type="generate_section"'}):
        asyncio.run(handle_generate_section(_ctx(pb, registry, sj)))
    aj = pb.collection("jobs").get_first_list_item('type="assemble_article"')
    return asyncio.run(handle_assemble_article(_ctx(pb, registry, aj)))


def _jobs_of(pb: FakePocketBase, job_type: str) -> list[dict[str, Any]]:
    return pb.collection("jobs").get_full_list({"filter": f'type="{job_type}"'})


def test_auto_publish_publishes_when_score_ok():
    pb = FakePocketBase(default_unique_fields())
    project = _project(pb, auto={"enabled": True, "min_score": 90, "max_attempts": 3})
    topic = TopicRepo(pb).create(
        project=project["id"], title="\u0633\u0626\u0648", keyword="\u0633\u0626\u0648"
    )
    registry = FakeRegistry()
    registry.llm.responses = [OUTLINE_JSON] + [SECTION_HTML] * 3

    result = _write_and_assemble(pb, registry, topic["id"], project["id"])
    assert result["seoScore"] >= 90

    # the score passed → a publish job was queued and the article approved
    publishes = _jobs_of(pb, "publish_article")
    assert len(publishes) == 1
    assert publishes[0]["payload"]["action"] == "publish"
    article = pb.collection("articles").get_first_list_item('topicId="' + topic["id"] + '"')
    assert article["status"] == "approved"
    # no rewrite job was queued
    assert len(_jobs_of(pb, "write_article")) == 1  # only the original


def test_auto_publish_rewrites_when_score_too_low():
    pb = FakePocketBase(default_unique_fields())
    # min_score above the possible maximum → the score can never pass
    project = _project(pb, auto={"enabled": True, "min_score": 101, "max_attempts": 3})
    topic = TopicRepo(pb).create(
        project=project["id"], title="\u0633\u0626\u0648", keyword="\u0633\u0626\u0648"
    )
    registry = FakeRegistry()
    registry.llm.responses = [OUTLINE_JSON] + [SECTION_HTML] * 3

    result = _write_and_assemble(pb, registry, topic["id"], project["id"])
    assert result["seoScore"] < 101

    # a rewrite (regenerate) job with autoAttempts=2 was queued instead
    rewrites = [j for j in _jobs_of(pb, "write_article") if j["payload"].get("regenerate")]
    assert len(rewrites) == 1
    assert rewrites[0]["payload"]["trigger"] == "schedule"
    assert rewrites[0]["payload"]["autoAttempts"] == 2
    assert rewrites[0]["payload"]["topicId"] == topic["id"]
    # article is back in generation, no publish job yet
    article = pb.collection("articles").get_first_list_item('topicId="' + topic["id"] + '"')
    assert article["status"] == "generating"
    assert len(_jobs_of(pb, "publish_article")) == 0


def test_auto_publish_gives_up_after_max_attempts():
    pb = FakePocketBase(default_unique_fields())
    project = _project(pb, auto={"enabled": True, "min_score": 101, "max_attempts": 3})
    topic = TopicRepo(pb).create(
        project=project["id"], title="\u0633\u0626\u0648", keyword="\u0633\u0626\u0648"
    )
    registry = FakeRegistry()
    registry.llm.responses = [OUTLINE_JSON] + [SECTION_HTML] * 3

    # already on the final attempt → no more rewrites, left for review
    result = _write_and_assemble(pb, registry, topic["id"], project["id"], auto_attempts=3)
    assert result["seoScore"] < 101
    assert len(_jobs_of(pb, "publish_article")) == 0
    rewrites = [j for j in _jobs_of(pb, "write_article") if j["payload"].get("regenerate")]
    assert rewrites == []
    article = pb.collection("articles").get_first_list_item('topicId="' + topic["id"] + '"')
    assert article["status"] == "review"


def test_auto_publish_ignored_for_manual_trigger():
    pb = FakePocketBase(default_unique_fields())
    project = _project(pb, auto={"enabled": True, "min_score": 90, "max_attempts": 3})
    topic = TopicRepo(pb).create(
        project=project["id"], title="\u0633\u0626\u0648", keyword="\u0633\u0626\u0648"
    )
    registry = FakeRegistry()
    registry.llm.responses = [OUTLINE_JSON] + [SECTION_HTML] * 3

    # a manual write (no schedule trigger) never auto-publishes
    result = _write_and_assemble(pb, registry, topic["id"], project["id"], trigger="manual")
    assert result["seoScore"] >= 90
    assert len(_jobs_of(pb, "publish_article")) == 0
    article = pb.collection("articles").get_first_list_item('topicId="' + topic["id"] + '"')
    assert article["status"] == "review"


def test_auto_publish_disabled_leaves_review():
    pb = FakePocketBase(default_unique_fields())
    project = _project(pb)  # autoPublish default: disabled
    topic = TopicRepo(pb).create(
        project=project["id"], title="\u0633\u0626\u0648", keyword="\u0633\u0626\u0648"
    )
    registry = FakeRegistry()
    registry.llm.responses = [OUTLINE_JSON] + [SECTION_HTML] * 3

    result = _write_and_assemble(pb, registry, topic["id"], project["id"])
    assert result["seoScore"] >= 90
    assert len(_jobs_of(pb, "publish_article")) == 0
    article = pb.collection("articles").get_first_list_item('topicId="' + topic["id"] + '"')
    assert article["status"] == "review"
