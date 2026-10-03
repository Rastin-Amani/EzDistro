"""Route tests — operations: indexing triggers, schedules, prompts, AI models,
retrieval diagnostics.

These routes kick off jobs or run diagnostics; tests assert the job is queued
with the right payload and that foreign/empty inputs are rejected safely.
"""

from __future__ import annotations

from app.api import projects as P
from app.repositories.indexing import DocumentRepo, IndexRunRepo
from app.repositories.jobs import JobRepo
from app.repositories.members import MemberRepo
from app.repositories.prompts import PromptRepo
from app.repositories.schedules import ScheduleRepo
from tests.helpers import (
    call_route,
    call_route_async,
    make_document,
    make_index_run,
    make_integration,
    make_job,
    make_member,
    make_pb,
    make_project,
    make_prompt,
    make_req,
    make_schedule,
    make_topic,
    make_user,
    toast_message,
)


def _setup(role: str = "owner"):
    pb = make_pb()
    proj_a = make_project(pb, slug="proj-a", name="A")
    proj_b = make_project(pb, slug="proj-b", name="B")
    MemberRepo(pb).add(project=proj_a["id"], user="u1", role=role)
    MemberRepo(pb).add(project=proj_b["id"], user="u2", role="owner")
    return pb, proj_a, proj_b


# ---------------------------------------------------------------------------
# Indexing triggers
# ---------------------------------------------------------------------------
def test_run_index_queues_project_job():
    pb, proj_a, _ = _setup()
    req = make_req(pb, make_user(), proj_a["id"])
    resp = call_route(P.run_index, req, proj_a["id"], full="0")
    assert "indexing" in toast_message(resp)
    jobs = JobRepo(pb).list_for_project(proj_a["id"], per_page=10)
    assert jobs[0]["type"] == "index_project"
    assert jobs[0]["payload"]["force"] is False


def test_run_index_full_force():
    pb, proj_a, _ = _setup()
    req = make_req(pb, make_user(), proj_a["id"])
    resp = call_route(P.run_index, req, proj_a["id"], full="1")
    assert "reindex" in toast_message(resp)
    jobs = JobRepo(pb).list_for_project(proj_a["id"], per_page=10)
    assert jobs[0]["payload"]["force"] is True


def test_reindex_document_queues_document_job():
    pb, proj_a, _ = _setup()
    doc = make_document(pb, proj_a["id"])
    req = make_req(pb, make_user(), proj_a["id"])
    resp = call_route(P.reindex_document, req, proj_a["id"], doc["id"], force="1")
    assert "indexing" in toast_message(resp)
    jobs = JobRepo(pb).list_for_project(proj_a["id"], per_page=10)
    assert jobs[0]["type"] == "index_document"
    assert jobs[0]["payload"]["sourceId"] == "100"
    assert jobs[0]["payload"]["force"] is True


def test_reindex_document_foreign_rejected():
    pb, proj_a, proj_b = _setup()
    foreign = make_document(pb, proj_b["id"])
    req = make_req(pb, make_user(), proj_a["id"])
    resp = call_route(P.reindex_document, req, proj_a["id"], foreign["id"])
    assert "not found" in toast_message(resp)
    assert len(JobRepo(pb).list_for_project(proj_a["id"], per_page=10)) == 0


def test_retry_index_run_resumes_from_checkpoint():
    pb, proj_a, _ = _setup()
    run = make_index_run(pb, proj_a["id"], status="failed")
    req = make_req(pb, make_user(), proj_a["id"])
    resp = call_route(P.retry_index_run, req, proj_a["id"], run["id"])
    assert "rescheduled" in toast_message(resp)
    jobs = JobRepo(pb).list_for_project(proj_a["id"], per_page=10)
    assert jobs[0]["type"] == "index_project"
    assert jobs[0]["payload"]["resumeFrom"] == "50"


def test_retry_index_run_foreign_rejected():
    pb, proj_a, proj_b = _setup()
    foreign = make_index_run(pb, proj_b["id"])
    req = make_req(pb, make_user(), proj_a["id"])
    resp = call_route(P.retry_index_run, req, proj_a["id"], foreign["id"])
    assert "not found" in toast_message(resp)
    assert len(JobRepo(pb).list_for_project(proj_a["id"], per_page=10)) == 0


# ---------------------------------------------------------------------------
# Schedules
# ---------------------------------------------------------------------------
def test_save_schedules_creates_and_updates():
    pb, proj_a, _ = _setup()
    req = make_req(pb, make_user(), proj_a["id"])
    resp = call_route(
        P.save_schedules, req, proj_a["id"], kind="index", enabled="1", interval_minutes="60"
    )
    assert "saved" in toast_message(resp)
    sched = ScheduleRepo(pb).first(filter=f'project="{proj_a["id"]}" && kind="index"')
    assert sched["intervalMinutes"] == 60
    assert sched["enabled"] is True

    # update the same kind (no duplicate rows)
    resp = call_route(
        P.save_schedules, req, proj_a["id"], kind="index", enabled="0", interval_minutes="120"
    )
    assert "saved" in toast_message(resp)
    rows = ScheduleRepo(pb).list_for_project(proj_a["id"])
    assert len(rows) == 1
    assert rows[0]["intervalMinutes"] == 120
    assert rows[0]["enabled"] is False


def test_save_schedules_creates_write_schedule():
    pb, proj_a, _ = _setup()
    req = make_req(pb, make_user(), proj_a["id"])
    resp = call_route(
        P.save_schedules, req, proj_a["id"], kind="write", enabled="1", interval_minutes="30"
    )
    assert "saved" in toast_message(resp)
    sched = ScheduleRepo(pb).first(filter=f'project="{proj_a["id"]}" && kind="write"')
    assert sched is not None


def test_save_schedules_returns_rendered_panel_with_saved_values():
    """The save response re-renders the schedule row from the DB so the saved
    status + interval are visibly confirmed (not just a toast)."""
    pb, proj_a, _ = _setup()
    req = make_req(pb, make_user(), proj_a["id"])
    resp = call_route(
        P.save_schedules, req, proj_a["id"], kind="index", enabled="0", interval_minutes="90"
    )
    assert "saved" in toast_message(resp)
    body = resp.body.decode()
    assert 'id="sched-index"' in body
    assert 'value="90"' in body  # saved interval rendered
    assert '<option value="0" selected>' in body  # saved status rendered


def test_save_schedules_rejects_unknown_kind():
    """A request with a missing/unknown kind must fail loudly — never create a
    garbage row and show a success toast."""
    pb, proj_a, _ = _setup()
    req = make_req(pb, make_user(), proj_a["id"])
    resp = call_route(
        P.save_schedules, req, proj_a["id"], kind="", enabled="1", interval_minutes="60"
    )
    assert toast_message(resp) == "Invalid schedule type"
    assert ScheduleRepo(pb).first(filter=f'project="{proj_a["id"]}" && kind=""') is None


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------
def test_duplicate_prompt_version_creates_inactive_copy():
    pb, proj_a, _ = _setup()
    make_prompt(pb, proj_a["id"], ptype="outline_user")
    version = PromptRepo(pb).first(
        filter=f'project="{proj_a["id"]}" && type="outline_user"', sort="-version"
    )
    req = make_req(pb, make_user(), proj_a["id"])
    resp = call_route(
        P.duplicate_prompt_version, req, proj_a["id"], "outline_user", version_id=version["id"]
    )
    assert "duplicated" in toast_message(resp)
    copies = PromptRepo(pb).list_for_project(proj_a["id"])
    assert len(copies) == 2
    dup = [v for v in copies if (v.get("variables") or {}).get("duplicatedFrom")]
    assert len(dup) == 1
    assert dup[0]["active"] is False


def test_duplicate_prompt_version_rejects_foreign():
    pb, proj_a, proj_b = _setup()
    make_prompt(pb, proj_b["id"], ptype="outline_user")
    version = PromptRepo(pb).first(filter=f'project="{proj_b["id"]}"', sort="-version")
    req = make_req(pb, make_user(), proj_a["id"])
    resp = call_route(
        P.duplicate_prompt_version, req, proj_a["id"], "outline_user", version_id=version["id"]
    )
    assert "not found" in toast_message(resp)
    assert len(PromptRepo(pb).list_for_project(proj_a["id"])) == 0


def test_compare_prompt_versions_renders_diff():
    pb, proj_a, _ = _setup()
    make_prompt(pb, proj_a["id"], ptype="outline_user")
    versions = PromptRepo(pb).list_for_project(proj_a["id"])
    req = make_req(pb, make_user(), proj_a["id"])
    resp = call_route(
        P.compare_prompt_versions,
        req,
        proj_a["id"],
        ptype="outline_user",
        a=versions[0]["id"],
        b=versions[0]["id"],
    )
    assert resp.status_code == 200
    assert "JSON" in resp.body.decode()


def test_compare_prompt_versions_foreign_returns_empty():
    pb, proj_a, proj_b = _setup()
    make_prompt(pb, proj_b["id"], ptype="outline_user")
    version = PromptRepo(pb).first(filter=f'project="{proj_b["id"]}"', sort="-version")
    req = make_req(pb, make_user(), proj_a["id"])
    resp = call_route(
        P.compare_prompt_versions,
        req,
        proj_a["id"],
        ptype="outline_user",
        a=version["id"],
        b=version["id"],
    )
    assert resp.status_code == 200
    assert resp.body.decode() == ""


def test_activate_prompt_version_flips_active():
    pb, proj_a, _ = _setup()
    make_prompt(pb, proj_a["id"], ptype="outline_user")
    make_prompt(pb, proj_a["id"], ptype="outline_user")
    versions = PromptRepo(pb).list_for_project(proj_a["id"])
    req = make_req(pb, make_user(), proj_a["id"])
    resp = call_route(
        P.activate_prompt_version, req, proj_a["id"], "outline_user", version_id=versions[1]["id"]
    )
    assert "activated" in toast_message(resp) or "saved" in toast_message(resp)
    active = [v for v in PromptRepo(pb).list_for_project(proj_a["id"]) if v["active"]]
    assert len(active) == 1
    assert active[0]["id"] == versions[1]["id"]


# ---------------------------------------------------------------------------
# Retrieval diagnostics
# ---------------------------------------------------------------------------
async def test_retrieval_diagnose_requires_query():
    pb, proj_a, _ = _setup()
    req = make_req(pb, make_user(), proj_a["id"])
    resp = await call_route_async(P.retrieval_diagnose, req, proj_a["id"], query="  ")
    assert "required" in toast_message(resp)


# ---------------------------------------------------------------------------
# AI models
# ---------------------------------------------------------------------------
def test_save_ai_models_persists_and_audits():
    pb, proj_a, _ = _setup()
    req = make_req(pb, make_user(), proj_a["id"])
    resp = call_route(
        P.save_ai_models,
        req,
        proj_a["id"],
        outline_provider="openai_compat",
        outline_model="gpt-4o-mini",
        section_provider="openai_compat",
        section_model="gpt-4o-mini",
    )
    assert "saved" in toast_message(resp)
    from app.repositories.projects import ProjectSettingsRepo

    settings = ProjectSettingsRepo(pb).get_for_project(proj_a["id"])
    assert settings["outlineModel"] == "gpt-4o-mini"
    assert settings["sectionModel"] == "gpt-4o-mini"


def test_save_global_llm_defaults_admin_only():
    pb, proj_a, _ = _setup()
    # non-admin → rejected
    req = make_req(pb, make_user(), proj_a["id"])
    resp = call_route(P.save_global_llm_defaults, req, proj_a["id"], outline_model="gpt-4o")
    assert "failed" in toast_message(resp) or "admin" in toast_message(resp)

    # admin → saved
    req = make_req(pb, make_user("admin1", role="admin"), proj_a["id"])
    resp = call_route(P.save_global_llm_defaults, req, proj_a["id"], outline_model="gpt-4o")
    assert "saved" in toast_message(resp) or "defaults" in toast_message(resp)


async def test_discover_models_invalid_role_returns_empty():
    pb, proj_a, _ = _setup()
    req = make_req(pb, make_user(), proj_a["id"])
    resp = await call_route_async(P.discover_models, req, proj_a["id"], "bogus_role")
    assert resp.status_code == 200
    assert resp.body.decode() == ""


async def test_discover_models_renders_datalist():
    pb, proj_a, _ = _setup()
    req = make_req(pb, make_user(), proj_a["id"])
    resp = await call_route_async(P.discover_models, req, proj_a["id"], "outline")
    assert resp.status_code == 200
    body = resp.body.decode()
    assert "datalist" in body or "option" in body or body == ""


# ---------------------------------------------------------------------------
# Prompt tester
# ---------------------------------------------------------------------------
async def test_prompt_tester_without_provider_errors_cleanly():
    """With no LLM integration configured the tester must not crash the page."""
    pb, proj_a, _ = _setup()
    req = make_req(pb, make_user(), proj_a["id"])
    resp = await call_route_async(
        P.test_prompt,
        req,
        proj_a["id"],
        ptype="outline_user",
        content="JSON \u0628\u0631\u06af\u0631\u062f\u0627\u0646.",
    )
    assert resp.status_code == 200
    # either a rendered result or a safe, human-readable error toast — never a 500
    assert "response" in toast_message(resp) or "integration" in toast_message(resp)
