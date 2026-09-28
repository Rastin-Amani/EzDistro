"""Authorization & project-isolation tests (route level).

These pin the production-readiness fixes:
- C1: cross-project mutations via raw record IDs are rejected
- H1: project roles are enforced (viewer read-only; owner/admin for destructive ops)
- M2: /projects is scoped to the user's memberships
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.api import articles as A
from app.api import projects as P
from app.api import workspace as W
from app.repositories.articles import ArticleRepo, SectionRepo
from app.repositories.integrations import IntegrationRepo
from app.repositories.jobs import JobRepo
from app.repositories.members import MemberRepo
from app.repositories.projects import ProjectRepo
from app.repositories.prompts import PromptRepo
from app.repositories.schedules import ScheduleRepo
from app.repositories.topics import TopicRepo
from tests.fakes import FakePocketBase, default_unique_fields
from tests.helpers import call_route, toast_message


def make_pb() -> FakePocketBase:
    pb = FakePocketBase(default_unique_fields())
    pb.collection("users").create(
        {"email": "u@x.com", "password": "x", "passwordConfirm": "x", "role": "member"}
    )
    return pb


def make_user(uid: str = "u1", role: str = "member") -> dict:
    return {"id": uid, "role": role, "email": f"{uid}@x.com"}


def make_req(pb: FakePocketBase, user: dict, project_id: str) -> SimpleNamespace:
    def url_for(name: str, **path_params):
        return "/" + name

    return SimpleNamespace(
        state=SimpleNamespace(pb=pb, user=user, req_id="r1"),
        headers={"HX-Request": "true"},
        url=SimpleNamespace(path=f"/projects/{project_id}"),
        query_params={},
        url_for=url_for,
    )


@pytest.fixture()
def setup():
    """User u1 is owner of projectA; projectB exists with data; u2 is a viewer of A."""
    pb = make_pb()
    proj_a = ProjectRepo(pb).create(name="A", slug="proj-a")
    proj_b = ProjectRepo(pb).create(name="B", slug="proj-b")
    MemberRepo(pb).add(project=proj_a["id"], user="u1", role="owner")
    MemberRepo(pb).add(project=proj_a["id"], user="u2", role="viewer")

    topic_b = TopicRepo(pb).create(
        project=proj_b["id"], title="\u0645\u0648\u0636\u0648\u0639 B", keyword="kb"
    )
    article_b = pb.collection("articles").create(
        {
            "project": proj_b["id"],
            "topicId": topic_b["id"],
            "title": "\u0645\u0642\u0627\u0644\u0647 B",
            "slug": "art-b",
            "status": "outline_ready",
        }
    )
    section_b = SectionRepo(pb).create(
        article=article_b["id"], position=0, heading="\u0628", content_brief="\u062e"
    )
    int_b = IntegrationRepo(pb).create(
        project=proj_b["id"],
        category="llm",
        provider="openai_compat",
        display_name="LLM B",
        configuration={},
        secrets_enc="",
        enabled=True,
    )
    return {
        "pb": pb,
        "proj_a": proj_a,
        "proj_b": proj_b,
        "topic_b": topic_b,
        "article_b": article_b,
        "section_b": section_b,
        "int_b": int_b,
    }


# ---------------------------------------------------------------------------
# C1 — cross-project mutations are rejected
# ---------------------------------------------------------------------------


def test_save_meta_cannot_modify_foreign_article(setup):
    req = make_req(setup["pb"], make_user(), setup["proj_a"]["id"])
    call_route(
        A.save_meta,
        req,
        setup["proj_a"]["id"],
        setup["article_b"]["id"],
        title="\u0647\u06a9",
        meta_description="",
    )
    article = ArticleRepo(setup["pb"]).get(setup["article_b"]["id"])
    assert article["title"] == "\u0645\u0642\u0627\u0644\u0647 B"  # untouched


def test_save_section_cannot_modify_foreign_section(setup):
    req = make_req(setup["pb"], make_user(), setup["proj_a"]["id"])
    call_route(
        A.save_section,
        req,
        setup["proj_a"]["id"],
        setup["article_b"]["id"],
        setup["section_b"]["id"],
        heading="\u0647\u06a9",
        content_brief="",
        content="<p>\u0647\u06a9</p>",
    )
    section = SectionRepo(setup["pb"]).get(setup["section_b"]["id"])
    assert section["heading"] == "\u0628"  # untouched


def test_delete_topic_cannot_delete_foreign_topic(setup):
    req = make_req(setup["pb"], make_user(), setup["proj_a"]["id"])
    call_route(P.delete_topic, req, setup["proj_a"]["id"], setup["topic_b"]["id"])
    assert TopicRepo(setup["pb"]).get(setup["topic_b"]["id"]) is not None  # survived


def test_delete_integration_cannot_delete_foreign_integration(setup):
    req = make_req(setup["pb"], make_user(), setup["proj_a"]["id"])
    call_route(P.delete_integration, req, setup["proj_a"]["id"], setup["int_b"]["id"])
    assert IntegrationRepo(setup["pb"]).get(setup["int_b"]["id"]) is not None


def test_toggle_integration_cannot_toggle_foreign_integration(setup):
    req = make_req(setup["pb"], make_user(), setup["proj_a"]["id"])
    call_route(P.toggle_integration, req, setup["proj_a"]["id"], setup["int_b"]["id"])
    assert IntegrationRepo(setup["pb"]).get(setup["int_b"]["id"])["enabled"] is True  # unchanged


def test_save_integration_creates_new_integration(setup):
    """New integrations must save via the form endpoint (was: TypeError on
    'displayName' — camelCase payload vs snake_case create() kwargs)."""
    req = make_req(setup["pb"], make_user(), setup["proj_a"]["id"])
    call_route(
        P.save_integration,
        req,
        setup["proj_a"]["id"],
        category="embedding",
        provider="openai_compat",
        display_name="Embedder",
        base_url="https://example.com/v1",
        model="text-embedding-3-small",
        secret="sk-embed-1234",
    )
    records = IntegrationRepo(setup["pb"]).list_for_project(setup["proj_a"]["id"], "embedding")
    assert len(records) == 1
    rec = records[0]
    assert rec["provider"] == "openai_compat"
    assert rec["displayName"] == "Embedder"
    assert rec["configuration"]["model"] == "text-embedding-3-small"
    assert rec["configuration"]["base_url"] == "https://example.com/v1"
    assert rec["enabled"] is True
    assert rec["secretsEnc"]  # encrypted at rest
    assert "sk-embed-1234" not in rec["secretsEnc"]
    assert rec["configuration"]["masked"]


def test_save_integration_normalizes_scheme_less_base_url(setup):
    """Typing a base URL without a scheme must not brick the health check."""
    req = make_req(setup["pb"], make_user(), setup["proj_a"]["id"])
    call_route(
        P.save_integration,
        req,
        setup["proj_a"]["id"],
        category="llm",
        provider="openai_compat",
        display_name="Router",
        base_url="router.example.com/v1",
        secret="sk-router-1",
    )
    records = IntegrationRepo(setup["pb"]).list_for_project(setup["proj_a"]["id"], "llm")
    assert records[0]["configuration"]["base_url"] == "https://router.example.com/v1"


def test_save_integration_update_keeps_existing_secret(setup):
    """Editing an integration without typing a new secret must keep the
    previously encrypted one (never blank it)."""
    pb = setup["pb"]
    existing = IntegrationRepo(pb).create(
        project=setup["proj_a"]["id"],
        category="llm",
        provider="openai_compat",
        display_name="LLM A",
        configuration={"base_url": "https://example.com/v1", "masked": "sk-l…234"},
        secrets_enc="gAAAAAexisting-ciphertext",
        enabled=True,
    )
    req = make_req(pb, make_user(), setup["proj_a"]["id"])
    call_route(
        P.save_integration,
        req,
        setup["proj_a"]["id"],
        record_id=existing["id"],
        category="llm",
        provider="openai_compat",
        display_name="LLM A (renamed)",
        base_url="https://example.com/v1",
        model="gpt-4o-mini",
        secret="",
    )
    rec = IntegrationRepo(pb).get(existing["id"])
    assert rec["secretsEnc"] == "gAAAAAexisting-ciphertext"
    assert rec["displayName"] == "LLM A (renamed)"
    assert rec["configuration"]["masked"] == "sk-l…234"


def test_queue_assemble_rejects_foreign_article(setup):
    req = make_req(setup["pb"], make_user(), setup["proj_a"]["id"])
    call_route(W.queue_assemble, req, setup["proj_a"]["id"], setup["article_b"]["id"])
    jobs = JobRepo(setup["pb"]).list_for_project(setup["proj_a"]["id"])
    assert all(j["type"] != "assemble_article" for j in jobs)  # no foreign job created


def test_activate_prompt_version_rejects_foreign_version(setup):
    pb = setup["pb"]
    v = PromptRepo(pb).save_version(
        project_id=setup["proj_b"]["id"], ptype="seo_rules", name="default", content="vB"
    )
    req = make_req(pb, make_user(), setup["proj_a"]["id"])
    call_route(
        P.activate_prompt_version, req, setup["proj_a"]["id"], "seo_rules", version_id=v["id"]
    )
    # version still inactive for B
    row = PromptRepo(pb).get(v["id"])
    assert row["active"] is True  # it was saved active; activating from A must not have happened
    # try activating a different B version via A — must be refused (A's active stays none)
    assert PromptRepo(pb)._active_row(setup["proj_a"]["id"], "seo_rules", "default") is None


# ---------------------------------------------------------------------------
# H1 — project roles are enforced
# ---------------------------------------------------------------------------


def test_viewer_cannot_save_settings(setup):
    from app.repositories.projects import ProjectSettingsRepo

    req = make_req(setup["pb"], make_user("u2"), setup["proj_a"]["id"])
    call_route(P.save_settings, req, setup["proj_a"]["id"], default_llm_model="gpt-5")
    settings = ProjectSettingsRepo(setup["pb"]).get_for_project(setup["proj_a"]["id"])
    assert settings.get("defaultLlmModel") != "gpt-5"  # not applied


def test_viewer_cannot_delete_project(setup):
    req = make_req(setup["pb"], make_user("u2"), setup["proj_a"]["id"])
    call_route(P.delete_project, req, setup["proj_a"]["id"])
    assert ProjectRepo(setup["pb"]).get(setup["proj_a"]["id"]) is not None


def test_viewer_cannot_publish(setup):
    # give project A an approved article
    topic_a = TopicRepo(setup["pb"]).create(
        project=setup["proj_a"]["id"], title="\u062a", keyword="k"
    )
    article_a = (
        setup["pb"]
        .collection("articles")
        .create(
            {
                "project": setup["proj_a"]["id"],
                "topicId": topic_a["id"],
                "title": "\u0627\u0644\u0641",
                "slug": "a",
                "status": "approved",
                "finalHtml": "<p>x</p>",
            }
        )
    )
    req = make_req(setup["pb"], make_user("u2"), setup["proj_a"]["id"])
    call_route(A.publish_article, req, setup["proj_a"]["id"], article_a["id"])
    jobs = JobRepo(setup["pb"]).list_for_project(setup["proj_a"]["id"])
    assert all(j["type"] != "publish_article" for j in jobs)  # no publish job for a viewer


def test_owner_can_save_settings(setup):
    from app.repositories.projects import ProjectSettingsRepo

    req = make_req(setup["pb"], make_user(), setup["proj_a"]["id"])
    call_route(P.save_settings, req, setup["proj_a"]["id"], default_llm_model="gpt-5")
    settings = ProjectSettingsRepo(setup["pb"]).get_for_project(setup["proj_a"]["id"])
    assert settings.get("defaultLlmModel") == "gpt-5"


def test_owner_can_delete_project(setup):
    req = make_req(setup["pb"], make_user(), setup["proj_a"]["id"])
    call_route(P.delete_project, req, setup["proj_a"]["id"])
    assert ProjectRepo(setup["pb"]).get(setup["proj_a"]["id"]) is None


def test_non_member_cannot_access_project(setup):
    """A user with NO membership in project A is refused a friendly error (no 500)."""
    pb = setup["pb"]
    pb.collection("users").create(
        {"email": "u3@x.com", "password": "x", "passwordConfirm": "x", "role": "member"}
    )
    req = make_req(pb, make_user("u3"), setup["proj_a"]["id"])
    resp = call_route(P.project_tab, req, setup["proj_a"]["id"], "settings")
    # the HTMX tab partial is an error toast, never a 500 white screen
    assert resp.status_code == 200
    assert "show-toast" in resp.headers.get("HX-Trigger", "")


def test_project_detail_missing_project_renders_not_found(setup):
    """Stale/deleted project id renders a friendly page instead of raising (500)."""
    req = make_req(setup["pb"], make_user(), "does-not-exist")
    resp = call_route(P.project_detail, req, "does-not-exist")
    assert resp.status_code == 200
    assert (
        "\u067e\u0631\u0648\u0698\u0647 \u06cc\u0627\u0641\u062a \u0646\u0634\u062f"
        in resp.body.decode()
    )


def test_project_detail_renders_active_tab_with_context(setup):
    """Direct load of /projects/{id} renders the default settings tab without 500."""
    req = make_req(setup["pb"], make_user(), setup["proj_a"]["id"])
    resp = call_route(P.project_detail, req, setup["proj_a"]["id"])
    assert resp.status_code == 200
    body = resp.body.decode()
    assert ">A</h1>" in body  # project name heading
    assert (
        "\u062c\u0627\u0633\u0627\u0632\u06cc \u0645\u062a\u0646 (Embedding)" in body
    )  # settings tab partial rendered inline
    assert '<nav id="project-tabs"' in body
    assert f'href="/projects/{setup["proj_a"]["id"]}?tab=settings"' in body
    assert 'data-tab="settings"' in body and 'aria-current="page"' in body
    assert 'role="tab"' not in body


def test_settings_tab_schedule_forms_are_not_nested(setup):
    """Regression: schedules must never live in nested <form>s (browsers ignore
    them, so the schedule save silently fired the outer form). Schedules are
    plain fields inside the single settings form — one form, one save."""
    pb = setup["pb"]
    proj_a = setup["proj_a"]
    req = make_req(pb, make_user(), proj_a["id"])
    resp = call_route(P.project_tab, req, proj_a["id"], "settings")
    assert resp.status_code == 200
    body = resp.body.decode()
    # exactly ONE <form> (the settings form) — no nested schedule forms
    assert body.count("<form") == 1
    assert body.count("</form>") == 1
    # schedule fields are part of that single form
    assert 'name="schedule_index_enabled"' in body
    assert 'name="schedule_index_interval"' in body
    assert 'name="schedule_write_enabled"' in body
    assert 'name="schedule_write_interval"' in body


def test_save_settings_persists_auto_publish(setup):
    """Regression: the autoPublish block must round-trip through the settings
    save AND the project_settings schema (an unknown PB field is silently
    dropped, which used to make the UI setting never persist)."""
    pb = setup["pb"]
    proj_a = setup["proj_a"]
    req = make_req(pb, make_user(), proj_a["id"])
    resp = call_route(
        P.save_settings,
        req,
        proj_a["id"],
        auto_publish_enabled="1",
        auto_publish_min_score="95",
        auto_publish_max_attempts="2",
    )
    assert "\u0630\u062e\u06cc\u0631\u0647 \u0634\u062f" in toast_message(resp)
    stored = pb.collection("project_settings").get_first_list_item(f'project="{proj_a["id"]}"')
    assert stored["autoPublish"] == {"enabled": True, "min_score": 95, "max_attempts": 2}
    # and the settings read view reflects it
    from app.repositories.projects import ProjectSettingsRepo

    merged = ProjectSettingsRepo(pb).get_for_project(proj_a["id"])
    assert merged["autoPublish"]["enabled"] is True
    assert merged["autoPublish"]["min_score"] == 95
    assert merged["autoPublish"]["max_attempts"] == 2


def test_save_settings_persists_schedules(setup):
    """Saving the settings form persists the schedule rows (same submission)."""
    pb = setup["pb"]
    proj_a = setup["proj_a"]
    req = make_req(pb, make_user(), proj_a["id"])
    resp = call_route(
        P.save_settings,
        req,
        proj_a["id"],
        schedule_index_enabled="1",
        schedule_index_interval="60",
        schedule_write_enabled="0",
        schedule_write_interval="120",
    )
    assert "\u0630\u062e\u06cc\u0631\u0647 \u0634\u062f" in toast_message(resp)
    index = ScheduleRepo(pb).first(filter=f'project="{proj_a["id"]}" && kind="index"')
    write = ScheduleRepo(pb).first(filter=f'project="{proj_a["id"]}" && kind="write"')
    assert index is not None and index["enabled"] is True and index["intervalMinutes"] == 60
    assert write is not None and write["enabled"] is False and write["intervalMinutes"] == 120
    # second save updates in place (no duplicate rows)
    call_route(
        P.save_settings,
        req,
        proj_a["id"],
        schedule_index_enabled="0",
        schedule_index_interval="720",
    )
    rows = ScheduleRepo(pb).list_for_project(proj_a["id"])
    assert len(rows) == 2
    index = ScheduleRepo(pb).first(filter=f'project="{proj_a["id"]}" && kind="index"')
    assert index["enabled"] is False and index["intervalMinutes"] == 720


def test_project_tab_prompts_empty_history_renders(setup):
    """Prompts tab renders even when the project has no prompt versions yet."""
    req = make_req(setup["pb"], make_user(), setup["proj_a"]["id"])
    resp = call_route(P.project_tab, req, setup["proj_a"]["id"], "prompts")
    assert resp.status_code == 200
    assert (
        "\u067e\u06cc\u0634\u200c\u0641\u0631\u0636 \u0633\u0631\u0627\u0633\u0631\u06cc \u0627\u0633\u062a\u0641\u0627\u062f\u0647 \u0645\u06cc\u200c\u0634\u0648\u062f"
        in resp.body.decode()
    )


# ---------------------------------------------------------------------------
# M2 — /projects is scoped to memberships
# ---------------------------------------------------------------------------


def test_projects_list_scoped_for_non_admin(setup):
    req = make_req(setup["pb"], make_user("u2"), setup["proj_a"]["id"])
    resp = call_route(P.projects_list, req)
    # u2 only belongs to project A — project B must not appear
    assert "proj-b" not in resp.body.decode()


def test_projects_list_unscoped_for_admin(setup):
    req = make_req(setup["pb"], make_user("admin1", role="admin"), setup["proj_a"]["id"])
    resp = call_route(P.projects_list, req)
    assert "proj-b" in resp.body.decode()
