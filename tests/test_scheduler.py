"""Schedule poller tests."""

from __future__ import annotations

from tests.fakes import FakePocketBase, default_unique_fields


def seed(pb: FakePocketBase) -> str:
    project = pb.collection("projects").create(
        {
            "name": "\u067e\u0631\u0648\u0698\u0647",
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
            "name": "\u0634\u0627\u062e\u0635 \u0631\u0648\u0632\u0627\u0646\u0647",
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
            "name": "\u062e\u0627\u0645\u0648\u0634",
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
            "name": "\u0641\u0639\u0627\u0644 \u0648\u0644\u06cc \u067e\u0631\u0648\u0698\u0647 \u062e\u0627\u0645\u0648\u0634",
            "kind": "index",
            "enabled": True,
            "intervalMinutes": 60,
            "nextRunAt": "2020-01-01 00:00:00.000Z",
            "payload": {},
        }
    )
    assert run_schedule_poll(pb) == 0


def test_write_schedule_creates_write_job_for_next_planned_topic():
    from app.services.scheduler import run_schedule_poll

    pb = FakePocketBase(default_unique_fields())
    project_id = seed(pb)
    # two planned topics; priority decides which one is picked
    pb.collection("topics").create(
        {
            "project": project_id,
            "title": "\u06a9\u0645\u200c\u0627\u0648\u0644\u0648\u06cc\u062a",
            "status": "planned",
            "priority": 1,
            "type": "article",
        }
    )
    high = pb.collection("topics").create(
        {
            "project": project_id,
            "title": "\u067e\u0631\u0627\u0648\u0644\u0648\u06cc\u062a",
            "status": "planned",
            "priority": 9,
            "type": "article",
        }
    )
    pb.collection("schedules").create(
        {
            "project": project_id,
            "name": "\u0646\u0648\u06cc\u0633\u0646\u062f\u0647",
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
    assert job["payload"]["topicId"] == high["id"]
    assert job["entityId"] == high["id"]


def test_write_schedule_without_planned_topics_skips_job():
    from app.services.scheduler import run_schedule_poll

    pb = FakePocketBase(default_unique_fields())
    project_id = seed(pb)
    sched = pb.collection("schedules").create(
        {
            "project": project_id,
            "name": "\u0646\u0648\u06cc\u0633\u0646\u062f\u0647",
            "kind": "write",
            "enabled": True,
            "intervalMinutes": 120,
            "nextRunAt": "2020-01-01 00:00:00.000Z",
            "payload": {},
        }
    )
    assert run_schedule_poll(pb) == 0
    assert len(pb.collection("jobs").get_full_list()) == 0
    # the schedule still advanced so the poller doesn't re-fire immediately
    after = pb.collection("schedules").get_one(sched["id"])
    assert after["nextRunAt"] != "2020-01-01 00:00:00.000Z"
