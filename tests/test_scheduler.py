"""Schedule poller tests."""

from __future__ import annotations

from tests.fakes import FakePocketBase, default_unique_fields


def seed(pb: FakePocketBase) -> str:
    project = pb.collection("projects").create(
        {
            "name": "پروژه",
            "slug": "sched-proj",
            "language": "fa",
            "status": "active",
            "timezone": "Asia/Tehran",
        }
    )
    return project["id"]


def test_due_schedule_creates_index_job_once_per_window():
    from app.services.scheduler import run_schedule_poll

    pb = FakePocketBase(default_unique_fields())
    project_id = seed(pb)
    sched = pb.collection("schedules").create(
        {
            "project": project_id,
            "name": "شاخص روزانه",
            "kind": "index",
            "enabled": True,
            "intervalMinutes": 60,
            "nextRunAt": "2020-01-01 00:00:00.000Z",
            "payload": {},
        }
    )

    assert run_schedule_poll(pb) == 1
    jobs = pb.collection("jobs").get_full_list()
    assert len(jobs) == 1
    assert jobs[0]["type"] == "index_project"
    assert jobs[0]["payload"]["trigger"] == "schedule"

    # second poll in the same window → no duplicate (idempotency key)
    assert run_schedule_poll(pb) == 0
    assert len(pb.collection("jobs").get_full_list()) == 1

    # nextRunAt advanced
    after = pb.collection("schedules").get_one(sched["id"])
    assert after["nextRunAt"] != "2020-01-01 00:00:00.000Z"
    assert after["lastRunAt"]


def test_disabled_and_inactive_project_schedules_skipped():
    from app.services.scheduler import run_schedule_poll

    pb = FakePocketBase(default_unique_fields())
    project_id = seed(pb)
    pb.collection("schedules").create(
        {
            "project": project_id,
            "name": "خاموش",
            "kind": "write",
            "enabled": False,
            "intervalMinutes": 60,
            "nextRunAt": "2020-01-01 00:00:00.000Z",
            "payload": {},
        }
    )
    assert run_schedule_poll(pb) == 0

    # archived project
    pb.collection("projects").update(project_id, {"status": "archived"})
    pb.collection("schedules").create(
        {
            "project": project_id,
            "name": "فعال ولی پروژه خاموش",
            "kind": "index",
            "enabled": True,
            "intervalMinutes": 60,
            "nextRunAt": "2020-01-01 00:00:00.000Z",
            "payload": {},
        }
    )
    assert run_schedule_poll(pb) == 0


def test_write_schedule_creates_write_job():
    from app.services.scheduler import run_schedule_poll

    pb = FakePocketBase(default_unique_fields())
    project_id = seed(pb)
    pb.collection("schedules").create(
        {
            "project": project_id,
            "name": "نویسنده",
            "kind": "write",
            "enabled": True,
            "intervalMinutes": 120,
            "nextRunAt": "2020-01-01 00:00:00.000Z",
            "payload": {},
        }
    )
    assert run_schedule_poll(pb) == 1
    job = pb.collection("jobs").get_first_list_item('type="write_article"')
    assert job["type"] == "write_article"
