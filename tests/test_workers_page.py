"""Worker dashboard tests: heartbeat registry upsert + /workers page rendering
(active/stale/offline derivation, restart-needed badge, empty state)."""

from __future__ import annotations

import datetime as dt

from app.api import workers as W
from app.repositories.jobs import now_utc, pb_dt
from app.repositories.worker_heartbeats import WorkerHeartbeatRepo
from tests.helpers import (
    call_route,
    make_member,
    make_pb,
    make_project,
    make_req,
    make_user,
)


def _setup():
    pb = make_pb()
    project = make_project(pb)
    make_member(pb, project["id"], "u1")
    return pb, project


def test_heartbeat_upsert_creates_then_updates_single_row():
    from tests.fakes import FakePocketBase, default_unique_fields

    pb = FakePocketBase(default_unique_fields())
    repo = WorkerHeartbeatRepo(pb)
    repo.upsert("w1", {"hostname": "a", "pid": 1, "version": "0.1.0", "runningJobs": 0})
    repo.upsert("w1", {"hostname": "a", "pid": 1, "version": "0.1.0", "runningJobs": 3})
    rows = repo.list_recent()
    assert len(rows) == 1
    assert rows[0]["workerId"] == "w1"
    assert rows[0]["runningJobs"] == 3


def test_workers_page_renders_statuses_and_restart_badge(monkeypatch):
    from tests.fakes import FakePocketBase, default_unique_fields

    pb = FakePocketBase(default_unique_fields())
    project = make_project(pb)
    make_member(pb, project["id"], "u1")
    repo = WorkerHeartbeatRepo(pb)
    # active (10s ago), stale (2min ago), offline (3h ago) + old version
    repo.upsert(
        "w-active",
        {
            "hostname": "srv1",
            "pid": 1,
            "version": "0.1.0",
            "startedAt": pb_dt(now_utc()),
            "lastHeartbeatAt": pb_dt(now_utc() - dt.timedelta(seconds=10)),
            "runningJobs": 2,
            "completedJobs": 9,
            "failedJobs": 1,
            "maxConcurrentJobs": 4,
            "scheduleLastPollAt": "",
            "scheduleDue": 0,
            "scheduleCreated": 0,
            "scheduleFailed": 0,
        },
    )
    repo.upsert(
        "w-stale",
        {
            "hostname": "srv2",
            "pid": 2,
            "version": "0.0.9",
            "startedAt": pb_dt(now_utc()),
            "lastHeartbeatAt": pb_dt(now_utc() - dt.timedelta(minutes=2)),
            "runningJobs": 0,
            "completedJobs": 2,
            "failedJobs": 0,
            "maxConcurrentJobs": 2,
            "scheduleLastPollAt": "",
            "scheduleDue": 0,
            "scheduleCreated": 0,
            "scheduleFailed": 0,
        },
    )
    repo.upsert(
        "w-old",
        {
            "hostname": "srv9",
            "pid": 9,
            "version": "0.1.0",
            "startedAt": pb_dt(now_utc() - dt.timedelta(days=1)),
            "lastHeartbeatAt": pb_dt(now_utc() - dt.timedelta(hours=3)),
            "runningJobs": 0,
            "completedJobs": 1,
            "failedJobs": 0,
            "maxConcurrentJobs": 4,
            "scheduleLastPollAt": "",
            "scheduleDue": 0,
            "scheduleCreated": 0,
            "scheduleFailed": 0,
        },
    )
    monkeypatch.setattr("app.config.settings.app_version", "0.1.0")

    req = make_req(pb, make_user(), project["id"])
    resp = call_route(W.workers_page, req)
    assert resp.status_code == 200
    body = resp.body.decode()
    assert "w-active" in body and "w-stale" in body and "w-old" in body
    assert "\u0641\u0639\u0627\u0644" in body and "\u0622\u0641\u0644\u0627\u06cc\u0646" in body
    # w-stale runs an old version → restart-needed badge
    assert (
        "\u0646\u06cc\u0627\u0632 \u0628\u0647 \u0631\u0627\u0647\u200c\u0627\u0646\u062f\u0627\u0632\u06cc \u0645\u062c\u062f\u062f"
        in body
    )
    assert "make worker" in body
    assert "\u06a9\u067e\u06cc" in body


def test_workers_page_empty_state():
    from tests.fakes import FakePocketBase, default_unique_fields

    pb = FakePocketBase(default_unique_fields())
    project = make_project(pb)
    make_member(pb, project["id"], "u1")
    req = make_req(pb, make_user(), project["id"])
    resp = call_route(W.workers_page, req)
    assert resp.status_code == 200
    body = resp.body.decode()
    assert (
        "\u0647\u06cc\u0686 \u06a9\u0627\u0631\u06af\u0631\u06cc \u062f\u0631 \u062d\u0627\u0644 \u0627\u062c\u0631\u0627 \u0646\u06cc\u0633\u062a"
        in body
    )
    assert "make worker" in body
