"""Schedule poller tests."""

from __future__ import annotations

from tests.fakes import FakePocketBase, default_unique_fields


def seed(pb: FakePocketBase) -> str:
    project = pb.collection("projects").create(
        {
            "name": "Project",
            "slug": "sched-proj",
            "language": "en",
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
            "name": "Daily metric",
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
            "name": "off",
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
            "name": "enabled but project off",
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
            "title": "low-priority",
            "status": "planned",
            "priority": 1,
            "type": "article",
        }
    )
    high = pb.collection("topics").create(
        {
            "project": project_id,
            "title": "high-priority",
            "status": "planned",
            "priority": 9,
            "type": "article",
        }
    )
    pb.collection("schedules").create(
        {
            "project": project_id,
            "name": "writer",
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
            "name": "writer",
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


# ---------------------------------------------------------------------------
# publish schedule — drains the ready queue, one article per interval
# ---------------------------------------------------------------------------
def _publish_setup(pb, project_id, *, auto_enabled=True, min_score=90):
    from app.repositories.projects import DEFAULT_SETTINGS

    settings = dict(DEFAULT_SETTINGS)
    settings["autoPublish"] = {"enabled": auto_enabled, "min_score": min_score, "max_attempts": 3}
    pb.collection("project_settings").create({"project": project_id, **settings})
    pb.collection("schedules").create(
        {
            "project": project_id,
            "name": "publisher",
            "kind": "publish",
            "enabled": True,
            "intervalMinutes": 480,
            "nextRunAt": "2020-01-01 00:00:00.000Z",
            "payload": {},
        }
    )


def _article(pb, project_id, title, *, status="review", score=95, created=None):
    row = pb.collection("articles").create(
        {
            "project": project_id,
            "topicId": "",
            "title": title,
            "slug": title.lower(),
            "status": status,
            "seoScore": score,
        }
    )
    if created:
        pb.collection("articles").update(row["id"], {"created": created})
    return row


def test_publish_schedule_queues_the_oldest_ready_article():
    from app.services.scheduler import run_schedule_poll

    pb = FakePocketBase(default_unique_fields())
    project_id = seed(pb)
    _publish_setup(pb, project_id)
    newer = _article(pb, project_id, "newer")
    older = _article(pb, project_id, "older", created="2026-01-01 00:00:00.000Z")

    assert run_schedule_poll(pb) == 1

    job = pb.collection("jobs").get_first_list_item('type="publish_article"')
    assert job["payload"]["articleId"] == older["id"]
    assert job["payload"]["trigger"] == "schedule"
    assert job["entityId"] == older["id"]
    # picking it is the approval: the publish handler refuses a raw `review`
    assert pb.collection("articles").get_one(older["id"])["status"] == "approved"
    assert pb.collection("articles").get_one(newer["id"])["status"] == "review"


def test_publish_schedule_skips_below_the_score_floor_and_when_disabled():
    from app.services.scheduler import run_schedule_poll

    pb = FakePocketBase(default_unique_fields())
    project_id = seed(pb)
    _publish_setup(pb, project_id)
    _article(pb, project_id, "weak", score=40)
    assert run_schedule_poll(pb) == 0
    assert pb.collection("jobs").get_full_list() == []

    # nothing ready at all
    pb = FakePocketBase(default_unique_fields())
    project_id = seed(pb)
    _publish_setup(pb, project_id)
    assert run_schedule_poll(pb) == 0

    # auto-publish switched off → the schedule must not publish anything
    pb = FakePocketBase(default_unique_fields())
    project_id = seed(pb)
    _publish_setup(pb, project_id, auto_enabled=False)
    _article(pb, project_id, "ready")
    assert run_schedule_poll(pb) == 0
