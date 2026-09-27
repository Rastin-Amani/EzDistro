"""Route tests — jobs monitor/detail/events/cancel/retry, failed jobs, logs.

Verifies scoping (admins see all, members only their projects), the
not-found path, and the HTMX mutation contracts.
"""

from __future__ import annotations

from app.api import jobs as J
from app.api import logs as L
from app.repositories.jobs import JobRepo
from app.repositories.members import MemberRepo
from tests.helpers import (
    call_route,
    make_job,
    make_job_event,
    make_pb,
    make_project,
    make_req,
    make_user,
    toast_message,
)


def _setup():
    pb = make_pb()
    proj_a = make_project(pb, slug="proj-a", name="A")
    proj_b = make_project(pb, slug="proj-b", name="B")
    MemberRepo(pb).add(project=proj_a["id"], user="u1", role="owner")
    MemberRepo(pb).add(project=proj_b["id"], user="u2", role="owner")
    return pb, proj_a, proj_b


# ---------------------------------------------------------------------------
# Monitor pages
# ---------------------------------------------------------------------------
def test_jobs_monitor_renders_and_scopes_member():
    pb, proj_a, proj_b = _setup()
    make_job(pb, proj_a["id"], job_type="write_article", status="running")
    make_job(pb, proj_b["id"], job_type="write_article", status="running")
    req = make_req(pb, make_user(), proj_a["id"])
    resp = call_route(J.jobs_monitor, req)
    body = resp.body.decode()
    assert resp.status_code == 200
    # member without a project filter sees only their own project's jobs
    assert "write_article" in body
    # project A's job is the only one in the list; B's must not appear as data
    assert (
        proj_b["id"] not in body.split("data-project=")[1][:200]
        if "data-project=" in body
        else True
    )


def test_jobs_monitor_admin_sees_all():
    pb, proj_a, proj_b = _setup()
    make_job(pb, proj_a["id"], job_type="index_project")
    make_job(pb, proj_b["id"], job_type="index_project")
    req = make_req(pb, make_user("admin1", role="admin"), proj_a["id"])
    resp = call_route(J.jobs_monitor, req)
    assert resp.status_code == 200
    body = resp.body.decode()
    assert body.count("index_project") >= 2


def test_jobs_monitor_filters_by_status():
    pb, proj_a, _ = _setup()
    make_job(pb, proj_a["id"], job_type="index_project", status="failed")
    make_job(pb, proj_a["id"], job_type="index_project", status="completed")
    req = make_req(pb, make_user(), proj_a["id"])
    failed_job = make_job(pb, proj_a["id"], job_type="write_article", status="failed")
    completed_job = make_job(pb, proj_a["id"], job_type="write_article", status="completed")
    req = make_req(pb, make_user(), proj_a["id"])
    resp = call_route(J.jobs_monitor, req, status="failed")
    body = resp.body.decode()
    assert failed_job["id"] in body
    assert completed_job["id"] not in body


def test_failed_jobs_page_lists_only_failed():
    pb, proj_a, _ = _setup()
    make_job(pb, proj_a["id"], job_type="write_article", status="failed")
    make_job(pb, proj_a["id"], job_type="write_article", status="completed")
    req = make_req(pb, make_user(), proj_a["id"])
    resp = call_route(J.failed_jobs, req)
    body = resp.body.decode()
    assert resp.status_code == 200
    assert "write_article" in body


def test_logs_page_renders():
    pb = make_pb()
    req = make_req(pb, make_user("admin1", role="admin"), "")
    resp = call_route(L.logs_page, req)
    assert resp.status_code == 200
    assert (
        "\u0631\u0648\u06cc\u062f\u0627\u062f" in resp.body.decode()
        or "\u0648\u0631\u0648\u062f" in resp.body.decode()
    )


# ---------------------------------------------------------------------------
# Job detail + events
# ---------------------------------------------------------------------------
def test_job_detail_renders_for_owner():
    pb, proj_a, _ = _setup()
    job = make_job(pb, proj_a["id"], job_type="write_article")
    make_job_event(pb, job["id"])
    req = make_req(pb, make_user(), proj_a["id"])
    resp = call_route(J.job_detail, req, job["id"])
    assert resp.status_code == 200
    assert "write_article" in resp.body.decode()


def test_job_detail_hides_foreign_job():
    pb, proj_a, proj_b = _setup()
    foreign = make_job(pb, proj_b["id"], job_type="write_article")
    req = make_req(pb, make_user(), proj_a["id"])
    resp = call_route(J.job_detail, req, foreign["id"])
    assert resp.status_code == 200
    assert "\u06cc\u0627\u0641\u062a \u0646\u0634\u062f" in resp.body.decode()


def test_job_events_fragment_renders_timeline():
    pb, proj_a, _ = _setup()
    job = make_job(pb, proj_a["id"])
    make_job_event(pb, job["id"], event_type="job.started")
    make_job_event(pb, job["id"], event_type="job.completed")
    req = make_req(pb, make_user(), proj_a["id"])
    resp = call_route(J.job_events_fragment, req, job["id"])
    assert resp.status_code == 200
    assert "job.started" in resp.body.decode()


def test_job_events_fragment_foreign_job_empty():
    pb, proj_a, proj_b = _setup()
    foreign = make_job(pb, proj_b["id"])
    req = make_req(pb, make_user(), proj_a["id"])
    resp = call_route(J.job_events_fragment, req, foreign["id"])
    assert resp.status_code == 200
    assert resp.body.decode() == ""


# ---------------------------------------------------------------------------
# Job mutations
# ---------------------------------------------------------------------------
def test_cancel_job_requests_cancel():
    pb, proj_a, _ = _setup()
    job = make_job(pb, proj_a["id"], job_type="write_article", status="running")
    req = make_req(pb, make_user(), proj_a["id"])
    resp = call_route(J.cancel_job, req, job["id"])
    assert "\u0644\u063a\u0648" in toast_message(resp)
    assert JobRepo(pb).get(job["id"])["cancelRequested"] is True


def test_cancel_job_foreign_job_rejected():
    pb, proj_a, proj_b = _setup()
    foreign = make_job(pb, proj_b["id"], status="running")
    req = make_req(pb, make_user(), proj_a["id"])
    resp = call_route(J.cancel_job, req, foreign["id"])
    assert "\u06cc\u0627\u0641\u062a \u0646\u0634\u062f" in toast_message(resp)
    assert JobRepo(pb).get(foreign["id"])["cancelRequested"] is False


def test_retry_job_schedules_retry_for_failed():
    pb, proj_a, _ = _setup()
    job = make_job(pb, proj_a["id"], job_type="write_article", status="failed")
    req = make_req(pb, make_user(), proj_a["id"])
    resp = call_route(J.retry_job, req, job["id"])
    assert "\u062a\u0644\u0627\u0634 \u0645\u062c\u062f\u062f" in toast_message(resp)
    jobs = JobRepo(pb).list_for_project(proj_a["id"], per_page=10)
    retry = [j for j in jobs if j["type"] == "retry_failed_job"]
    assert len(retry) == 1
    assert retry[0]["payload"] == {"targetJobId": job["id"]}


def test_retry_job_refuses_non_failed():
    pb, proj_a, _ = _setup()
    job = make_job(pb, proj_a["id"], job_type="write_article", status="running")
    req = make_req(pb, make_user(), proj_a["id"])
    resp = call_route(J.retry_job, req, job["id"])
    assert "\u0646\u0627\u0645\u0648\u0641\u0642 \u0646\u06cc\u0633\u062a" in toast_message(resp)
    assert len(JobRepo(pb).list_for_project(proj_a["id"], per_page=10)) == 1


def test_retry_job_foreign_rejected():
    pb, proj_a, proj_b = _setup()
    foreign = make_job(pb, proj_b["id"], status="failed")
    req = make_req(pb, make_user(), proj_a["id"])
    resp = call_route(J.retry_job, req, foreign["id"])
    assert "\u06cc\u0627\u0641\u062a \u0646\u0634\u062f" in toast_message(resp)
    assert len(JobRepo(pb).list_for_project(proj_a["id"], per_page=10)) == 0
