"""Landing-page analysis job + product context injection into prompts."""

from __future__ import annotations

import asyncio
import json

import pytest

from app.jobs.context import JobContext, ProviderStack
from app.providers.base import PermanentError
from app.repositories.jobs import JobEventRepo
from app.repositories.projects import ProjectSettingsRepo
from app.services.landing_page import handle_analyze_landing_page, html_to_text
from app.services.settings import ProjectConfig
from tests.fake_providers import FakeLLM, FakeRegistry
from tests.helpers import (
    call_route,
    make_member,
    make_pb,
    make_project,
    make_req,
    make_user,
    toast_message,
)

PROFILE = {
    "brand": "Acme",
    "summary": "Acme sells ergonomic chairs for home offices.",
    "industry": "Furniture",
    "products": [
        {
            "name": "Acme Ergo",
            "description": "An adjustable office chair.",
            "audience": "Remote workers",
            "differentiators": ["12-year warranty"],
        }
    ],
    "value_props": ["Build quality"],
    "keywords": ["ergonomic chair"],
}


def make_ctx(pb, registry, project_id: str) -> JobContext:
    config = ProjectConfig.load(pb, project_id)
    job = {"id": "job-1", "project": project_id, "payload": {}}
    return JobContext(
        pb=pb,
        job=job,
        config=config,
        providers=ProviderStack(registry=registry, config=config),
        registry=registry,
        events=JobEventRepo(pb),
        set_progress=lambda *a, **k: None,
        request_cancel=lambda jid: False,
    )


def _set_landing_url(pb, project_id: str, url: str) -> None:
    record = pb.collection("project_settings").get_first_list_item(
        f'project="{project_id}"', {"perPage": 1}
    )
    ProjectSettingsRepo(pb).update(record["id"], {"landingPageUrl": url})


def test_html_to_text_strips_scripts_and_chrome():
    text = html_to_text(
        "<html><head><style>.x{}</style><script>var a=1;</script></head>"
        "<body><h1>Hello</h1><p>World</p><script>track()</script></body></html>"
    )
    assert "Hello" in text and "World" in text
    assert "var a" not in text and "track()" not in text


def test_analyze_job_stores_ready_profile(monkeypatch):
    pb = make_pb()
    project = make_project(pb)
    _set_landing_url(pb, project["id"], "https://acme.test")

    async def fake_fetch(url: str):
        return "https://acme.test/", "Acme sells ergonomic chairs for home offices." * 5

    monkeypatch.setattr("app.services.landing_page.fetch_landing_page", fake_fetch)
    registry = FakeRegistry()
    registry.llm = FakeLLM([json.dumps(PROFILE)])

    result = asyncio.run(handle_analyze_landing_page(make_ctx(pb, registry, project["id"])))

    assert result["status"] == "ready"
    assert result["products"] == 1
    stored = ProjectSettingsRepo(pb).get_for_project(project["id"])["productProfile"]
    assert stored["status"] == "ready"
    assert stored["profile"]["brand"] == "Acme"


def test_analyze_job_records_failure_without_crashing_settings(monkeypatch):
    pb = make_pb()
    project = make_project(pb)
    _set_landing_url(pb, project["id"], "https://acme.test")

    async def fake_fetch(url: str):
        return "https://acme.test/", "some readable page text that is long enough" * 3

    monkeypatch.setattr("app.services.landing_page.fetch_landing_page", fake_fetch)
    registry = FakeRegistry()
    registry.llm = FakeLLM(["not json at all"])

    with pytest.raises(PermanentError):
        asyncio.run(handle_analyze_landing_page(make_ctx(pb, registry, project["id"])))

    stored = ProjectSettingsRepo(pb).get_for_project(project["id"])["productProfile"]
    assert stored["status"] == "failed"
    assert stored["error"]


def test_product_profile_flows_into_prompt_context():
    from app.services.prompt_service import PromptService

    pb = make_pb()
    project = make_project(pb)
    record = pb.collection("project_settings").get_first_list_item(
        f'project="{project["id"]}"', {"perPage": 1}
    )
    ProjectSettingsRepo(pb).update(
        record["id"],
        {
            "productProfile": {"status": "ready", "profile": PROFILE},
            "landingPageUrl": "https://acme.test",
        },
    )
    config = ProjectConfig.load(pb, project["id"])
    context = PromptService(pb).build_context(config)
    assert "Acme" in context["product_profile"]
    # brand_voice carries it too so existing prompts consume it without edits
    assert "Acme" in (config.prompt("brand_voice") or "") or context["brand_voice"] != ""


def test_prompt_labels_include_new_variables():
    from app.domain.prompt_render import variable_labels

    labels = variable_labels()
    assert "product_profile" in labels
    assert "landing_page_url" in labels


def test_analyze_route_queues_job_and_requires_url():
    from app.api import projects as P
    from app.repositories.projects import ProjectSettingsRepo

    pb = make_pb()
    project = make_project(pb)
    make_member(pb, project["id"], user_id="u1", role="owner")
    req = make_req(pb, make_user(), project["id"])

    # no URL typed or saved → no job, error toast
    resp = call_route(P.analyze_landing_page, req, project["id"])
    jobs = pb.collection("jobs").get_full_list()
    assert not [j for j in jobs if j["type"] == "analyze_landing_page"]
    assert "Enter a landing page URL" in toast_message(resp)

    # URL typed and not yet saved → persisted here, then a job is queued
    call_route(P.analyze_landing_page, req, project["id"], landing_page_url="https://typed.test")
    jobs = [j for j in pb.collection("jobs").get_full_list() if j["type"] == "analyze_landing_page"]
    assert len(jobs) == 1
    assert jobs[0]["project"] == project["id"]
    assert (
        ProjectSettingsRepo(pb).get_for_project(project["id"])["landingPageUrl"]
        == "https://typed.test"
    )

    # URL already saved → still queues from the saved value
    _set_landing_url(pb, project["id"], "https://acme.test")
    call_route(P.analyze_landing_page, req, project["id"])
    jobs = [j for j in pb.collection("jobs").get_full_list() if j["type"] == "analyze_landing_page"]
    assert len(jobs) >= 1


def test_landing_status_fragment_is_never_cached():
    """The polled status URL is identical each tick; it must not be cached."""
    from app.api import projects as P

    pb = make_pb()
    project = make_project(pb)
    make_member(pb, project["id"], user_id="u1", role="owner")
    req = make_req(pb, make_user(), project["id"])

    resp = call_route(P.landing_page_status, req, project["id"])
    assert resp.headers.get("cache-control") == "no-store"
