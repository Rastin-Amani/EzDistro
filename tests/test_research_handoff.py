"""Handoff tests: an accepted opportunity becomes a planned topic, nothing more."""

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


def test_accept_creates_a_planned_topic_and_no_article():
    pb = FakePocketBase(default_unique_fields())
    project = make_project(pb)

    result = accept_opportunity(pb, idea=_idea(project["id"]), config={"project": project["id"]})

    assert result["topic"]["status"] == "planned"
    assert result["article"] is None
    # Nothing enters Articles until the writer actually starts the topic.
    assert pb.collection("articles").get_full_list() == []
    assert pb.collection("jobs").get_full_list() == []


def test_accept_does_not_queue_a_write_even_without_a_schedule():
    """Approval is a content-plan decision; the schedule/user owns writing."""
    pb = FakePocketBase(default_unique_fields())
    project = make_project(pb)

    accept_opportunity(pb, idea=_idea(project["id"]), config={"project": project["id"]})

    assert pb.collection("jobs").get_full_list() == []
    assert pb.collection("topics").get_full_list()[0]["status"] == "planned"


def test_update_action_links_the_existing_article_and_creates_no_topic():
    pb = FakePocketBase(default_unique_fields())
    project = make_project(pb)
    topic = pb.collection("topics").create(
        {"project": project["id"], "title": "Existing", "status": "published"}
    )
    article = pb.collection("articles").create(
        {
            "project": project["id"],
            "topicId": topic["id"],
            "title": "Existing",
            "slug": "existing",
            "status": "published",
        }
    )

    idea = {
        **_idea(project["id"]),
        "action": "update",
        "existingArticle": article["id"],
    }
    result = accept_opportunity(pb, idea=idea, config={"project": project["id"]})

    assert result["article"]["id"] == article["id"]
    assert result["topic"] is None
    assert len(pb.collection("topics").get_full_list()) == 1
    assert len(pb.collection("articles").get_full_list()) == 1
