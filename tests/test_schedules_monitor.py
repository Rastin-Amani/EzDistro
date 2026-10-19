"""Schedules monitor — the read-only /schedules page.

Render + logic tests against the in-memory fake PocketBase: state derivation
(late / due / paused / inactive), the per-kind forecast, tenant scoping, the
worker heartbeat strip and the scheduled-vs-manual activity feed.
"""

from __future__ import annotations

import datetime as dt

from app.api import schedules as S
from app.repositories.projects import ProjectRepo, ProjectSettingsRepo
from app.repositories.schedules import ScheduleRepo
from tests.helpers import (
    call_route,
    make_document,
    make_job,
    make_member,
    make_pb,
    make_project,
    make_req,
    make_topic,
    make_user,
)


def _setup(role: str = "owner"):
    pb = make_pb()
    proj = make_project(pb, slug="alpha", name="Alpha")
    make_member(pb, proj["id"], "u1", role)
    return pb, proj


def _render(pb):
    resp = call_route(S.schedules_page, make_req(pb, make_user(), ""))
    return resp, resp.body.decode()


def _heartbeat(pb, **value):
    pb.collection("app_settings").create(
        {"key": "scheduler_heartbeat", "value": {"lastPollAt": "2026-10-19 10:00:00.000Z", **value}}
    )


# ---------------------------------------------------------------------------
# Page shell
# ---------------------------------------------------------------------------
def test_no_schedules_renders_empty_state():
    pb, _ = _setup()
    resp, body = _render(pb)
    assert resp.status_code == 200
    assert resp.headers["Cache-Control"] == "no-store"
    assert "Choose what to automate" in body
    assert resp.context["total_schedules"] == 0


def test_worker_never_polled_is_called_out():
    pb, proj = _setup()
    ScheduleRepo(pb).create(project=proj["id"], name="write", kind="write", interval_minutes=60)
    _, body = _render(pb)
    assert "No automation check has been recorded yet" in body


def test_poller_heartbeat_is_shown_and_warns_when_due_but_nothing_fired():
    pb, _ = _setup()
    _heartbeat(pb, due=3, created=0, failed=1)
    _, body = _render(pb)
    assert "scheduler_heartbeat" not in body  # internal key never leaked
    assert "automatic tasks could not start at the last check" in body


# ---------------------------------------------------------------------------
# Row states
# ---------------------------------------------------------------------------
def test_write_row_forecasts_the_next_topic_and_interval():
    pb, proj = _setup()
    make_topic(pb, proj["id"], title="Best gym software", keyword="gym software")
    ScheduleRepo(pb).create(
        project=proj["id"], name="write", kind="write", interval_minutes=120, enabled=True
    )
    resp, body = _render(pb)
    assert "Write articles" in body
    assert "Next article: Best gym software" in body
    assert "every 2 h" in body
    assert "Next check:" in body
    assert resp.context["counts"]["active"] == 1


def test_write_row_without_topics_forecasts_a_skip():
    pb, proj = _setup()
    ScheduleRepo(pb).create(project=proj["id"], name="write", kind="write", interval_minutes=60)
    _, body = _render(pb)
    assert "Add ideas to the content plan before writing can start" in body


def test_publish_row_without_auto_publish_forecasts_a_skip():
    pb, proj = _setup()
    ScheduleRepo(pb).create(
        project=proj["id"], name="publish", kind="publish", interval_minutes=1440
    )
    _, body = _render(pb)
    assert "Automatic publishing is off" in body


def test_publish_row_with_auto_publish_and_ready_article():
    pb, proj = _setup()
    ProjectSettingsRepo(pb).upsert(proj["id"], {"autoPublish": {"enabled": True, "min_score": 80}})
    from tests.helpers import make_article

    article = make_article(pb, proj["id"], status="review")
    # next_to_publish applies the auto-publish score floor.
    pb.collection("articles").update(article["id"], {"seoScore": 90})
    ScheduleRepo(pb).create(
        project=proj["id"], name="publish", kind="publish", interval_minutes=1440
    )
    _, body = _render(pb)
    assert "Next to publish: Article title" in body


def test_index_row_counts_indexed_documents():
    pb, proj = _setup()
    make_document(pb, proj["id"])
    ScheduleRepo(pb).create(project=proj["id"], name="index", kind="index", interval_minutes=1440)
    _, body = _render(pb)
    assert "Refresh 1 saved website pages" in body


def test_paused_schedule_shows_paused_and_no_forecast():
    pb, proj = _setup()
    ScheduleRepo(pb).create(
        project=proj["id"], name="write", kind="write", interval_minutes=60, enabled=False
    )
    resp, body = _render(pb)
    assert "Paused" in body
    assert resp.context["counts"]["paused"] == 1
    assert resp.context["counts"]["active"] == 0


def test_inactive_project_is_not_reported_as_active():
    pb, proj = _setup()
    ScheduleRepo(pb).create(project=proj["id"], name="write", kind="write", interval_minutes=60)
    ProjectRepo(pb).set_status(proj["id"], "inactive")
    resp, body = _render(pb)
    assert "Website paused" in body
    assert resp.context["counts"]["active"] == 0
    assert resp.context["counts"]["inactive"] == 1


def test_run_far_past_its_next_run_is_late():
    pb, proj = _setup()
    sched = ScheduleRepo(pb).create(
        project=proj["id"], name="write", kind="write", interval_minutes=60
    )
    past = dt.datetime.now(dt.UTC) - dt.timedelta(hours=3)
    ScheduleRepo(pb).update(sched["id"], {"nextRunAt": past.strftime("%Y-%m-%d %H:%M:%S.000Z")})
    resp, body = _render(pb)
    assert "Overdue" in body
    assert resp.context["counts"]["late"] == 1


# ---------------------------------------------------------------------------
# Tenant isolation
# ---------------------------------------------------------------------------
def test_other_projects_schedules_are_never_shown():
    pb, mine = _setup()
    theirs = make_project(pb, slug="beta", name="Beta")
    make_member(pb, theirs["id"], "u2", "owner")
    ScheduleRepo(pb).create(project=theirs["id"], name="write", kind="write", interval_minutes=60)
    make_job(pb, theirs["id"], job_type="write_article")
    _, body = _render(pb)
    assert "Beta" not in body
    assert "Choose what to automate" in body


def test_user_without_memberships_sees_nothing():
    pb, proj = _setup()
    ScheduleRepo(pb).create(project=proj["id"], name="write", kind="write", interval_minutes=60)
    make_member(pb, proj["id"], "u1", "viewer")
    resp = call_route(S.schedules_page, make_req(pb, make_user("u9"), ""))
    assert "Choose what to automate" in resp.body.decode()


# ---------------------------------------------------------------------------
# Activity feed
# ---------------------------------------------------------------------------
def test_activity_marks_scheduled_runs_and_manual_ones():
    pb, proj = _setup()
    ScheduleRepo(pb).create(project=proj["id"], name="write", kind="write", interval_minutes=60)
    make_job(pb, proj["id"], job_type="write_article", payload={"trigger": "schedule"})
    make_job(pb, proj["id"], job_type="index_project", payload={"trigger": "manual"})
    response, _ = _render(pb)
    assert {job["scheduled"] for job in response.context["activity"]} == {True, False}


def test_activity_hides_job_types_schedules_never_create():
    pb, proj = _setup()
    ScheduleRepo(pb).create(project=proj["id"], name="write", kind="write", interval_minutes=60)
    make_job(pb, proj["id"], job_type="research_run")
    _, body = _render(pb)
    assert "research_run" not in body


def test_admin_sees_every_project():
    pb, mine = _setup()
    theirs = make_project(pb, slug="beta", name="Beta")
    ScheduleRepo(pb).create(project=theirs["id"], name="write", kind="write", interval_minutes=60)
    resp = call_route(S.schedules_page, make_req(pb, make_user("admin", role="admin"), ""))
    assert "Beta" in resp.body.decode()


# ---------------------------------------------------------------------------
# Cost
# ---------------------------------------------------------------------------
def test_reads_scale_with_projects_not_with_schedules():
    """Structural guard (no timings): four wave-one reads, then a bounded
    readiness lookup per PROJECT — never one per schedule row."""
    pb = make_pb()
    for i in range(3):
        proj = make_project(pb, slug=f"p{i}", name=f"P{i}")
        make_member(pb, proj["id"], "u1", "owner")
        for kind in ("index", "write", "publish"):
            ScheduleRepo(pb).create(project=proj["id"], name=kind, kind=kind, interval_minutes=60)
    pb.storage.query_count = 0
    call_route(S.schedules_page, make_req(pb, make_user(), ""))
    # schedules + projects + heartbeat + activity, then (write, index, settings,
    # article) for each of the 3 projects.
    assert pb.storage.query_count <= 4 + 4 * 3
