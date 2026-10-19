"""Handoff tests: an accepted opportunity respects the write schedule."""

from __future__ import annotations

from app.services.research_handoff import accept_opportunity
from tests.fakes import FakePocketBase, default_unique_fields
from tests.helpers import make_project


def _idea(project_id: str) -> dict:
    return {
        "project": project_id,
        "suggestedTitle": "How to run a gym",
        "primaryKeyword": "run a gym",
        "action": "generate",
    }


def test_accept_queues_write_job_when_no_schedule():
    pb = FakePocketBase(default_unique_fields())
    project = make_project(pb)

    accept_opportunity(pb, idea=_idea(project["id"]), config={"project": project["id"]})

    jobs = pb.collection("jobs").get_full_list()
    assert [j["type"] for j in jobs] == ["write_article"]
    topics = pb.collection("topics").get_full_list()
    assert topics[0]["status"] == "planned"


def test_accept_leaves_write_to_the_schedule_when_one_is_enabled():
    pb = FakePocketBase(default_unique_fields())
    project = make_project(pb)
    pb.collection("schedules").create(
        {
            "project": project["id"],
            "name": "write",
            "kind": "write",
            "enabled": True,
            "intervalMinutes": 480,
            "nextRunAt": "2020-01-01 00:00:00.000Z",
            "payload": {},
        }
    )

    accept_opportunity(pb, idea=_idea(project["id"]), config={"project": project["id"]})

    # topic + article exist, but nothing is queued: the poller owns the interval.
    assert pb.collection("jobs").get_full_list() == []
    assert len(pb.collection("articles").get_full_list()) == 1
    assert pb.collection("topics").get_full_list()[0]["status"] == "planned"
