"""Route tests — projects, status toggles, topics (page/create/bulk/cancel/write).

Covers the previously untested routes: create_project, toggle_project_status,
project_status_toggle, topics_page, create_topic, topics_bulk, cancel_topic,
write_topic — happy paths, validation errors, and role/tenant guards.
"""

from __future__ import annotations

from app.api import projects as P
from app.repositories.jobs import JobRepo
from app.repositories.members import MemberRepo
from app.repositories.projects import ProjectRepo
from app.repositories.topics import TopicRepo
from tests.fakes import default_unique_fields
from tests.helpers import (
    call_route,
    hx_events,
    make_job,
    make_member,
    make_pb,
    make_project,
    make_req,
    make_req_plain,
    make_topic,
    make_user,
    toast_message,
)


def _setup(role: str = "owner"):
    """Project A owned by u1; project B owned by u2 (for cross-tenant checks)."""
    pb = make_pb()
    proj_a = make_project(pb, slug="proj-a", name="A")
    proj_b = make_project(pb, slug="proj-b", name="B")
    MemberRepo(pb).add(project=proj_a["id"], user="u1", role=role)
    MemberRepo(pb).add(project=proj_b["id"], user="u2", role="owner")
    return pb, proj_a, proj_b


# ---------------------------------------------------------------------------
# create_project
# ---------------------------------------------------------------------------
def test_create_project_creates_and_redirects():
    pb = make_pb()
    req = make_req(pb, make_user(), "")
    resp = call_route(
        P.create_project, req, name="\u067e\u0631\u0648\u0698\u0647 \u0645\u0646", slug="my-proj"
    )
    assert resp.status_code == 200
    project = ProjectRepo(pb).first(filter='slug="my-proj"')
    assert project is not None
    assert project["name"] == "\u067e\u0631\u0648\u0698\u0647 \u0645\u0646"
    assert hx_events(resp)["delayed-redirect"]["url"] == f"/projects/{project['id']}"


def test_create_project_auto_slugs_when_blank():
    pb = make_pb()
    req = make_req(pb, make_user(), "")
    resp = call_route(
        P.create_project, req, name="\u0633\u0644\u0627\u0645 \u062f\u0646\u06cc\u0627", slug=""
    )
    assert resp.status_code == 200
    # non-ASCII names fall back to a timestamped ascii slug
    project = ProjectRepo(pb).first(
        filter='name="\u0633\u0644\u0627\u0645 \u062f\u0646\u06cc\u0627"'
    )
    assert project is not None
    assert project["slug"].startswith("project-")


def test_create_project_duplicate_slug_returns_error():
    pb = make_pb()
    make_project(pb, slug="dup")
    req = make_req(pb, make_user(), "")
    resp = call_route(
        P.create_project, req, name="\u062a\u06a9\u0631\u0627\u0631\u06cc", slug="dup"
    )
    assert "\u0646\u0627\u0645\u0648\u0641\u0642" in toast_message(resp)
    assert len(pb.collection("projects").get_full_list()) == 1


def test_create_project_rejects_non_hx():
    pb = make_pb()
    req = make_req_plain(pb, make_user())
    resp = call_route(P.create_project, req, name="x", slug="x")
    assert "\u0641\u0642\u0637" in toast_message(
        resp
    ) or "\u0646\u0627\u0645\u0648\u0641\u0642" in toast_message(resp)
    assert len(pb.collection("projects").get_full_list()) == 0


# ---------------------------------------------------------------------------
# Status toggle
# ---------------------------------------------------------------------------
def test_toggle_project_status_flips_active_inactive():
    pb, proj_a, _ = _setup()
    req = make_req(pb, make_user(), proj_a["id"])
    resp = call_route(P.toggle_project_status, req, proj_a["id"])
    assert ProjectRepo(pb).get(proj_a["id"])["status"] == "inactive"
    assert "\u063a\u06cc\u0631\u0641\u0639\u0627\u0644" in toast_message(resp)
    resp = call_route(P.toggle_project_status, req, proj_a["id"])
    assert ProjectRepo(pb).get(proj_a["id"])["status"] == "active"
    assert "\u0641\u0639\u0627\u0644" in toast_message(resp)


def test_toggle_project_status_requires_admin_role():
    pb, proj_a, _ = _setup(role="editor")
    req = make_req(pb, make_user(), proj_a["id"])
    resp = call_route(P.toggle_project_status, req, proj_a["id"])
    assert "\u06a9\u0627\u0641\u06cc" in toast_message(
        resp
    ) or "\u0646\u0627\u0645\u0648\u0641\u0642" in toast_message(resp)
    assert ProjectRepo(pb).get(proj_a["id"])["status"] == "active"


def test_project_status_toggle_fragment_renders():
    pb, proj_a, _ = _setup()
    req = make_req(pb, make_user(), proj_a["id"])
    resp = call_route(P.project_status_toggle, req, proj_a["id"])
    assert resp.status_code == 200
    assert "toggle" in resp.body.decode()


# ---------------------------------------------------------------------------
# Topics page
# ---------------------------------------------------------------------------
def test_topics_page_renders_with_search_and_filters():
    pb, proj_a, _ = _setup()
    make_topic(
        pb,
        proj_a["id"],
        title="\u0645\u0648\u0636\u0648\u0639 \u0627\u0648\u0644",
        keyword="\u06a9\u0644\u06cc\u062f \u0627\u0648\u0644",
    )
    make_topic(
        pb,
        proj_a["id"],
        title="\u0645\u0648\u0636\u0648\u0639 \u062f\u0648\u0645",
        status="published",
    )
    req = make_req(pb, make_user(), proj_a["id"])
    resp = call_route(P.topics_page, req, proj_a["id"])
    body = resp.body.decode()
    assert "\u0645\u0648\u0636\u0648\u0639 \u0627\u0648\u0644" in body
    assert "\u0645\u0648\u0636\u0648\u0639 \u062f\u0648\u0645" in body

    # status filter
    resp = call_route(P.topics_page, req, proj_a["id"], status="published")
    body = resp.body.decode()
    assert "\u0645\u0648\u0636\u0648\u0639 \u062f\u0648\u0645" in body
    assert "\u0645\u0648\u0636\u0648\u0639 \u0627\u0648\u0644" not in body

    # keyword search
    resp = call_route(
        P.topics_page, req, proj_a["id"], q="\u06a9\u0644\u06cc\u062f \u0627\u0648\u0644"
    )
    body = resp.body.decode()
    assert "\u0645\u0648\u0636\u0648\u0639 \u0627\u0648\u0644" in body
    assert "\u0645\u0648\u0636\u0648\u0639 \u062f\u0648\u0645" not in body


def test_topics_page_does_not_leak_other_projects():
    pb, proj_a, proj_b = _setup()
    make_topic(pb, proj_a["id"], title="\u0641\u0642\u0637 \u067e\u0631\u0648\u0698\u0647 \u0622")
    make_topic(pb, proj_b["id"], title="\u0641\u0642\u0637 \u067e\u0631\u0648\u0698\u0647 \u0628")
    req = make_req(pb, make_user(), proj_a["id"])
    resp = call_route(P.topics_page, req, proj_a["id"])
    body = resp.body.decode()
    assert "\u0641\u0642\u0637 \u067e\u0631\u0648\u0698\u0647 \u0622" in body
    assert "\u0641\u0642\u0637 \u067e\u0631\u0648\u0698\u0647 \u0628" not in body


# ---------------------------------------------------------------------------
# create_topic
# ---------------------------------------------------------------------------
def test_create_topic_persists_full_fields():
    pb, proj_a, _ = _setup()
    req = make_req(pb, make_user(), proj_a["id"])
    resp = call_route(
        P.create_topic,
        req,
        proj_a["id"],
        title="\u0639\u0646\u0648\u0627\u0646",
        keyword="\u06a9\u0644\u06cc\u062f",
        pillar="\u0633\u062a\u0648\u0646",
        cluster="\u062e\u0648\u0634\u0647",
        type="guide",
        priority="7",
        week="12",
        url="https://x.ir/1",
    )
    assert "\u0627\u0636\u0627\u0641\u0647 \u0634\u062f" in toast_message(resp)
    topic = TopicRepo(pb).list_for_project(proj_a["id"], per_page=10)[0]
    assert topic["type"] == "guide"
    assert topic["priority"] == 7
    assert topic["week"] == 12
    assert topic["url"] == "https://x.ir/1"


def test_create_topic_requires_title():
    pb, proj_a, _ = _setup()
    req = make_req(pb, make_user(), proj_a["id"])
    resp = call_route(P.create_topic, req, proj_a["id"], title="  ")
    assert "\u0627\u0644\u0632\u0627\u0645\u06cc" in toast_message(resp)
    assert TopicRepo(pb).list_for_project(proj_a["id"], per_page=10) == []


def test_create_topic_invalid_type_rejected():
    pb, proj_a, _ = _setup()
    req = make_req(pb, make_user(), proj_a["id"])
    resp = call_route(P.create_topic, req, proj_a["id"], title="\u062a", type="bogus")
    assert "\u0646\u0627\u0645\u0648\u0641\u0642" in toast_message(
        resp
    ) or "\u0646\u0627\u0645\u0639\u062a\u0628\u0631" in toast_message(resp)
    assert TopicRepo(pb).list_for_project(proj_a["id"], per_page=10) == []


# ---------------------------------------------------------------------------
# topics_bulk
# ---------------------------------------------------------------------------
def test_topics_bulk_generates_selected():
    pb, proj_a, _ = _setup()
    t1 = make_topic(pb, proj_a["id"], title="\u06cc\u06a9")
    t2 = make_topic(pb, proj_a["id"], title="\u062f\u0648")
    make_topic(pb, proj_a["id"], title="\u0633\u0647", status="published")
    req = make_req(pb, make_user(), proj_a["id"])
    resp = call_route(
        P.topics_bulk, req, proj_a["id"], action="generate", topic_ids=f"{t1['id']},{t2['id']}"
    )
    assert "2 \u0645\u0648\u0636\u0648\u0639" in toast_message(resp)
    jobs = JobRepo(pb).list_for_project(proj_a["id"], per_page=10)
    assert len(jobs) == 2
    assert all(j["type"] == "write_article" for j in jobs)
    assert TopicRepo(pb).get(t1["id"])["status"] == "queued"


def test_topics_bulk_cancel_skips_published():
    pb, proj_a, _ = _setup()
    t1 = make_topic(pb, proj_a["id"], title="\u062f\u0631 \u0635\u0641", status="queued")
    t2 = make_topic(pb, proj_a["id"], title="\u0645\u0646\u062a\u0634\u0631", status="published")
    req = make_req(pb, make_user(), proj_a["id"])
    resp = call_route(
        P.topics_bulk, req, proj_a["id"], action="cancel", topic_ids=f"{t1['id']},{t2['id']}"
    )
    assert "1 \u0645\u0648\u0636\u0648\u0639" in toast_message(resp)
    assert TopicRepo(pb).get(t1["id"])["status"] == "cancelled"
    assert TopicRepo(pb).get(t2["id"])["status"] == "published"


def test_topics_bulk_empty_selection_errors():
    pb, proj_a, _ = _setup()
    req = make_req(pb, make_user(), proj_a["id"])
    resp = call_route(P.topics_bulk, req, proj_a["id"], action="generate", topic_ids="")
    assert "\u0627\u0646\u062a\u062e\u0627\u0628" in toast_message(resp)


def test_topics_bulk_ignores_foreign_topic_ids():
    pb, proj_a, proj_b = _setup()
    foreign = make_topic(pb, proj_b["id"], title="\u0628")
    req = make_req(pb, make_user(), proj_a["id"])
    resp = call_route(P.topics_bulk, req, proj_a["id"], action="generate", topic_ids=foreign["id"])
    assert "0 \u0645\u0648\u0636\u0648\u0639" in toast_message(resp)
    assert TopicRepo(pb).get(foreign["id"])["status"] == "planned"


# ---------------------------------------------------------------------------
# cancel_topic / write_topic
# ---------------------------------------------------------------------------
def test_cancel_topic_sets_cancelled():
    pb, proj_a, _ = _setup()
    t = make_topic(pb, proj_a["id"], title="\u062a", status="queued")
    req = make_req(pb, make_user(), proj_a["id"])
    resp = call_route(P.cancel_topic, req, proj_a["id"], t["id"])
    assert "\u0644\u063a\u0648 \u0634\u062f" in toast_message(resp)
    assert TopicRepo(pb).get(t["id"])["status"] == "cancelled"


def test_cancel_topic_foreign_rejected():
    pb, proj_a, proj_b = _setup()
    foreign = make_topic(pb, proj_b["id"], title="\u0628", status="queued")
    req = make_req(pb, make_user(), proj_a["id"])
    resp = call_route(P.cancel_topic, req, proj_a["id"], foreign["id"])
    assert "\u06cc\u0627\u0641\u062a \u0646\u0634\u062f" in toast_message(resp)
    assert TopicRepo(pb).get(foreign["id"])["status"] == "queued"


def test_write_topic_queues_job():
    pb, proj_a, _ = _setup()
    t = make_topic(pb, proj_a["id"], title="\u062a", priority=5)
    req = make_req(pb, make_user(), proj_a["id"])
    resp = call_route(P.write_topic, req, proj_a["id"], t["id"])
    assert "\u0622\u063a\u0627\u0632 \u0634\u062f" in toast_message(resp)
    assert TopicRepo(pb).get(t["id"])["status"] == "queued"
    jobs = JobRepo(pb).list_for_project(proj_a["id"], per_page=10)
    assert len(jobs) == 1
    assert jobs[0]["payload"] == {"topicId": t["id"]}
    assert jobs[0]["priority"] == 5


def test_write_topic_foreign_rejected():
    pb, proj_a, proj_b = _setup()
    foreign = make_topic(pb, proj_b["id"], title="\u0628")
    req = make_req(pb, make_user(), proj_a["id"])
    resp = call_route(P.write_topic, req, proj_a["id"], foreign["id"])
    assert "\u06cc\u0627\u0641\u062a \u0646\u0634\u062f" in toast_message(resp)
    assert len(JobRepo(pb).list_for_project(proj_a["id"], per_page=10)) == 0


def test_write_topic_rejects_non_member():
    pb, proj_a, _ = _setup()
    t = make_topic(pb, proj_a["id"], title="\u062a")
    req = make_req(pb, make_user("stranger"), proj_a["id"])
    resp = call_route(P.write_topic, req, proj_a["id"], t["id"])
    assert "\u0646\u0627\u0645\u0648\u0641\u0642" in toast_message(
        resp
    ) or "\u062f\u0633\u062a\u0631\u0633\u06cc" in toast_message(resp)
    assert TopicRepo(pb).get(t["id"])["status"] == "planned"


def test_write_topic_rejects_viewer_role():
    pb, proj_a, _ = _setup()
    MemberRepo(pb).add(project=proj_a["id"], user="v1", role="viewer")
    t = make_topic(pb, proj_a["id"], title="\u062a")
    req = make_req(pb, make_user("v1"), proj_a["id"])
    resp = call_route(P.write_topic, req, proj_a["id"], t["id"])
    assert "\u0646\u0627\u0645\u0648\u0641\u0642" in toast_message(
        resp
    ) or "\u06a9\u0627\u0641\u06cc" in toast_message(resp)
    assert TopicRepo(pb).get(t["id"])["status"] == "planned"
