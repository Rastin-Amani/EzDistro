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
    resp = call_route(P.create_project, req, name="پروژه من", slug="my-proj")
    assert resp.status_code == 200
    project = ProjectRepo(pb).first(filter='slug="my-proj"')
    assert project is not None
    assert project["name"] == "پروژه من"
    assert hx_events(resp)["delayed-redirect"]["url"] == f"/projects/{project['id']}"


def test_create_project_auto_slugs_when_blank():
    pb = make_pb()
    req = make_req(pb, make_user(), "")
    resp = call_route(P.create_project, req, name="سلام دنیا", slug="")
    assert resp.status_code == 200
    # non-ASCII names fall back to a timestamped ascii slug
    project = ProjectRepo(pb).first(filter='name="سلام دنیا"')
    assert project is not None
    assert project["slug"].startswith("project-")


def test_create_project_duplicate_slug_returns_error():
    pb = make_pb()
    make_project(pb, slug="dup")
    req = make_req(pb, make_user(), "")
    resp = call_route(P.create_project, req, name="تکراری", slug="dup")
    assert "ناموفق" in toast_message(resp)
    assert len(pb.collection("projects").get_full_list()) == 1


def test_create_project_rejects_non_hx():
    pb = make_pb()
    req = make_req_plain(pb, make_user())
    resp = call_route(P.create_project, req, name="x", slug="x")
    assert "فقط" in toast_message(resp) or "ناموفق" in toast_message(resp)
    assert len(pb.collection("projects").get_full_list()) == 0


# ---------------------------------------------------------------------------
# Status toggle
# ---------------------------------------------------------------------------
def test_toggle_project_status_flips_active_inactive():
    pb, proj_a, _ = _setup()
    req = make_req(pb, make_user(), proj_a["id"])
    resp = call_route(P.toggle_project_status, req, proj_a["id"])
    assert ProjectRepo(pb).get(proj_a["id"])["status"] == "inactive"
    assert "غیرفعال" in toast_message(resp)
    resp = call_route(P.toggle_project_status, req, proj_a["id"])
    assert ProjectRepo(pb).get(proj_a["id"])["status"] == "active"
    assert "فعال" in toast_message(resp)


def test_toggle_project_status_requires_admin_role():
    pb, proj_a, _ = _setup(role="editor")
    req = make_req(pb, make_user(), proj_a["id"])
    resp = call_route(P.toggle_project_status, req, proj_a["id"])
    assert "کافی" in toast_message(resp) or "ناموفق" in toast_message(resp)
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
    make_topic(pb, proj_a["id"], title="موضوع اول", keyword="کلید اول")
    make_topic(pb, proj_a["id"], title="موضوع دوم", status="published")
    req = make_req(pb, make_user(), proj_a["id"])
    resp = call_route(P.topics_page, req, proj_a["id"])
    body = resp.body.decode()
    assert "موضوع اول" in body
    assert "موضوع دوم" in body

    # status filter
    resp = call_route(P.topics_page, req, proj_a["id"], status="published")
    body = resp.body.decode()
    assert "موضوع دوم" in body
    assert "موضوع اول" not in body

    # keyword search
    resp = call_route(P.topics_page, req, proj_a["id"], q="کلید اول")
    body = resp.body.decode()
    assert "موضوع اول" in body
    assert "موضوع دوم" not in body


def test_topics_page_does_not_leak_other_projects():
    pb, proj_a, proj_b = _setup()
    make_topic(pb, proj_a["id"], title="فقط پروژه آ")
    make_topic(pb, proj_b["id"], title="فقط پروژه ب")
    req = make_req(pb, make_user(), proj_a["id"])
    resp = call_route(P.topics_page, req, proj_a["id"])
    body = resp.body.decode()
    assert "فقط پروژه آ" in body
    assert "فقط پروژه ب" not in body


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
        title="عنوان",
        keyword="کلید",
        pillar="ستون",
        cluster="خوشه",
        type="guide",
        priority="7",
        week="12",
        url="https://x.ir/1",
    )
    assert "اضافه شد" in toast_message(resp)
    topic = TopicRepo(pb).list_for_project(proj_a["id"], per_page=10)[0]
    assert topic["type"] == "guide"
    assert topic["priority"] == 7
    assert topic["week"] == 12
    assert topic["url"] == "https://x.ir/1"


def test_create_topic_requires_title():
    pb, proj_a, _ = _setup()
    req = make_req(pb, make_user(), proj_a["id"])
    resp = call_route(P.create_topic, req, proj_a["id"], title="  ")
    assert "الزامی" in toast_message(resp)
    assert TopicRepo(pb).list_for_project(proj_a["id"], per_page=10) == []


def test_create_topic_invalid_type_rejected():
    pb, proj_a, _ = _setup()
    req = make_req(pb, make_user(), proj_a["id"])
    resp = call_route(P.create_topic, req, proj_a["id"], title="ت", type="bogus")
    assert "ناموفق" in toast_message(resp) or "نامعتبر" in toast_message(resp)
    assert TopicRepo(pb).list_for_project(proj_a["id"], per_page=10) == []


# ---------------------------------------------------------------------------
# topics_bulk
# ---------------------------------------------------------------------------
def test_topics_bulk_generates_selected():
    pb, proj_a, _ = _setup()
    t1 = make_topic(pb, proj_a["id"], title="یک")
    t2 = make_topic(pb, proj_a["id"], title="دو")
    make_topic(pb, proj_a["id"], title="سه", status="published")
    req = make_req(pb, make_user(), proj_a["id"])
    resp = call_route(
        P.topics_bulk, req, proj_a["id"], action="generate", topic_ids=f"{t1['id']},{t2['id']}"
    )
    assert "2 موضوع" in toast_message(resp)
    jobs = JobRepo(pb).list_for_project(proj_a["id"], per_page=10)
    assert len(jobs) == 2
    assert all(j["type"] == "write_article" for j in jobs)
    assert TopicRepo(pb).get(t1["id"])["status"] == "queued"


def test_topics_bulk_cancel_skips_published():
    pb, proj_a, _ = _setup()
    t1 = make_topic(pb, proj_a["id"], title="در صف", status="queued")
    t2 = make_topic(pb, proj_a["id"], title="منتشر", status="published")
    req = make_req(pb, make_user(), proj_a["id"])
    resp = call_route(
        P.topics_bulk, req, proj_a["id"], action="cancel", topic_ids=f"{t1['id']},{t2['id']}"
    )
    assert "1 موضوع" in toast_message(resp)
    assert TopicRepo(pb).get(t1["id"])["status"] == "cancelled"
    assert TopicRepo(pb).get(t2["id"])["status"] == "published"


def test_topics_bulk_empty_selection_errors():
    pb, proj_a, _ = _setup()
    req = make_req(pb, make_user(), proj_a["id"])
    resp = call_route(P.topics_bulk, req, proj_a["id"], action="generate", topic_ids="")
    assert "انتخاب" in toast_message(resp)


def test_topics_bulk_ignores_foreign_topic_ids():
    pb, proj_a, proj_b = _setup()
    foreign = make_topic(pb, proj_b["id"], title="ب")
    req = make_req(pb, make_user(), proj_a["id"])
    resp = call_route(P.topics_bulk, req, proj_a["id"], action="generate", topic_ids=foreign["id"])
    assert "0 موضوع" in toast_message(resp)
    assert TopicRepo(pb).get(foreign["id"])["status"] == "planned"


# ---------------------------------------------------------------------------
# cancel_topic / write_topic
# ---------------------------------------------------------------------------
def test_cancel_topic_sets_cancelled():
    pb, proj_a, _ = _setup()
    t = make_topic(pb, proj_a["id"], title="ت", status="queued")
    req = make_req(pb, make_user(), proj_a["id"])
    resp = call_route(P.cancel_topic, req, proj_a["id"], t["id"])
    assert "لغو شد" in toast_message(resp)
    assert TopicRepo(pb).get(t["id"])["status"] == "cancelled"


def test_cancel_topic_foreign_rejected():
    pb, proj_a, proj_b = _setup()
    foreign = make_topic(pb, proj_b["id"], title="ب", status="queued")
    req = make_req(pb, make_user(), proj_a["id"])
    resp = call_route(P.cancel_topic, req, proj_a["id"], foreign["id"])
    assert "یافت نشد" in toast_message(resp)
    assert TopicRepo(pb).get(foreign["id"])["status"] == "queued"


def test_write_topic_queues_job():
    pb, proj_a, _ = _setup()
    t = make_topic(pb, proj_a["id"], title="ت", priority=5)
    req = make_req(pb, make_user(), proj_a["id"])
    resp = call_route(P.write_topic, req, proj_a["id"], t["id"])
    assert "آغاز شد" in toast_message(resp)
    assert TopicRepo(pb).get(t["id"])["status"] == "queued"
    jobs = JobRepo(pb).list_for_project(proj_a["id"], per_page=10)
    assert len(jobs) == 1
    assert jobs[0]["payload"] == {"topicId": t["id"]}
    assert jobs[0]["priority"] == 5


def test_write_topic_foreign_rejected():
    pb, proj_a, proj_b = _setup()
    foreign = make_topic(pb, proj_b["id"], title="ب")
    req = make_req(pb, make_user(), proj_a["id"])
    resp = call_route(P.write_topic, req, proj_a["id"], foreign["id"])
    assert "یافت نشد" in toast_message(resp)
    assert len(JobRepo(pb).list_for_project(proj_a["id"], per_page=10)) == 0


def test_write_topic_rejects_non_member():
    pb, proj_a, _ = _setup()
    t = make_topic(pb, proj_a["id"], title="ت")
    req = make_req(pb, make_user("stranger"), proj_a["id"])
    resp = call_route(P.write_topic, req, proj_a["id"], t["id"])
    assert "ناموفق" in toast_message(resp) or "دسترسی" in toast_message(resp)
    assert TopicRepo(pb).get(t["id"])["status"] == "planned"


def test_write_topic_rejects_viewer_role():
    pb, proj_a, _ = _setup()
    MemberRepo(pb).add(project=proj_a["id"], user="v1", role="viewer")
    t = make_topic(pb, proj_a["id"], title="ت")
    req = make_req(pb, make_user("v1"), proj_a["id"])
    resp = call_route(P.write_topic, req, proj_a["id"], t["id"])
    assert "ناموفق" in toast_message(resp) or "کافی" in toast_message(resp)
    assert TopicRepo(pb).get(t["id"])["status"] == "planned"
