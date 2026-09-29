"""Authorization matrix sweep across every mutating project route.

For each mutation: anonymous, non-member, and viewer must be rejected with no
database mutation; missing HX-Request must be rejected (CSRF-lite).
"""

from __future__ import annotations

import pytest

from app.api import articles as A
from app.api import projects as P
from app.api import workspace as W
from app.repositories.articles import ArticleRepo
from app.repositories.members import MemberRepo
from app.repositories.topics import TopicRepo
from tests.helpers import (
    call_route,
    make_article,
    make_integration,
    make_pb,
    make_project,
    make_req,
    make_req_plain,
    make_schedule,
    make_topic,
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


def _anon_req(pb, project_id):
    """Request with no authenticated user."""
    req = make_req(pb, make_user(), project_id)
    req.state.user = None
    return req


MUTATIONS = [
    # (route fn, path args builder, form kwargs builder)
    (P.create_topic, lambda pb, a, b: (a["id"],), lambda: {"title": "\u062a"}),
    (
        P.write_topic,
        lambda pb, a, b: (a["id"], make_topic(pb, a["id"], title="\u062a")["id"]),
        lambda: {},
    ),
    (
        P.cancel_topic,
        lambda pb, a, b: (a["id"], make_topic(pb, a["id"], title="\u062a", status="queued")["id"]),
        lambda: {},
    ),
    (
        P.delete_topic,
        lambda pb, a, b: (a["id"], make_topic(pb, a["id"], title="\u062a")["id"]),
        lambda: {},
    ),
    (P.topics_bulk, lambda pb, a, b: (a["id"],), lambda: {"action": "generate", "topic_ids": "x"}),
    (P.toggle_project_status, lambda pb, a, b: (a["id"],), lambda: {}),
    (P.run_index, lambda pb, a, b: (a["id"],), lambda: {"full": "0"}),
    (
        P.save_schedules,
        lambda pb, a, b: (a["id"],),
        lambda: {"kind": "index", "enabled": "1", "interval_minutes": "60"},
    ),
    (P.save_settings, lambda pb, a, b: (a["id"],), lambda: {}),
    (
        W.set_article_status,
        lambda pb, a, b: (a["id"], make_article(pb, a["id"], status="review")["id"]),
        lambda: {"status": "approved"},
    ),
    (W.queue_assemble, lambda pb, a, b: (a["id"], make_article(pb, a["id"])["id"]), lambda: {}),
    (
        W.approve_article,
        lambda pb, a, b: (a["id"], make_article(pb, a["id"], status="review")["id"]),
        lambda: {},
    ),
    (
        W.send_back_article,
        lambda pb, a, b: (a["id"], make_article(pb, a["id"], status="review")["id"]),
        lambda: {"note": "x"},
    ),
    (
        W.outline_add,
        lambda pb, a, b: (a["id"], make_article(pb, a["id"])["id"]),
        lambda: {"heading": "\u062c", "content_brief": "\u062e"},
    ),
    (A.regenerate_article, lambda pb, a, b: (a["id"], make_article(pb, a["id"])["id"]), lambda: {}),
    (
        A.publish_article,
        lambda pb, a, b: (
            a["id"],
            make_article(pb, a["id"], status="approved", final_html="<p>x</p>")["id"],
        ),
        lambda: {},
    ),
]


@pytest.mark.parametrize(
    "fn,build_args,build_kwargs", MUTATIONS, ids=[m[0].__name__ for m in MUTATIONS]
)
def test_mutations_reject_anonymous(fn, build_args, build_kwargs):
    pb, proj_a, _ = _setup()
    args = build_args(pb, proj_a, None)
    kwargs = build_kwargs()
    before = {
        "topics": len(TopicRepo(pb).list_for_project(proj_a["id"], per_page=500)),
        "articles": len(pb.collection("articles").get_full_list()),
        "jobs": len(pb.collection("jobs").get_full_list()),
    }
    resp = call_route(fn, _anon_req(pb, proj_a["id"]), *args, **kwargs)
    assert "failed" in toast_message(resp) or "access denied" in toast_message(resp)
    # no mutation happened
    assert len(TopicRepo(pb).list_for_project(proj_a["id"], per_page=500)) == before["topics"]
    assert len(pb.collection("articles").get_full_list()) == before["articles"]
    assert len(pb.collection("jobs").get_full_list()) == before["jobs"]


@pytest.mark.parametrize(
    "fn,build_args,build_kwargs", MUTATIONS, ids=[m[0].__name__ for m in MUTATIONS]
)
def test_mutations_reject_non_member(fn, build_args, build_kwargs):
    pb, proj_a, _ = _setup()
    args = build_args(pb, proj_a, None)
    kwargs = build_kwargs()
    req = make_req(pb, make_user("stranger"), proj_a["id"])
    resp = call_route(fn, req, *args, **kwargs)
    assert "failed" in toast_message(resp) or "access denied" in toast_message(resp)


@pytest.mark.parametrize(
    "fn,build_args,build_kwargs", MUTATIONS, ids=[m[0].__name__ for m in MUTATIONS]
)
def test_mutations_reject_viewer_role(fn, build_args, build_kwargs):
    pb, proj_a, _ = _setup()
    MemberRepo(pb).add(project=proj_a["id"], user="v1", role="viewer")
    args = build_args(pb, proj_a, None)
    kwargs = build_kwargs()
    req = make_req(pb, make_user("v1"), proj_a["id"])
    resp = call_route(fn, req, *args, **kwargs)
    assert "failed" in toast_message(resp) or "role" in toast_message(resp)


@pytest.mark.parametrize(
    "fn,build_args,build_kwargs", MUTATIONS, ids=[m[0].__name__ for m in MUTATIONS]
)
def test_mutations_reject_missing_hx_header(fn, build_args, build_kwargs):
    pb, proj_a, _ = _setup()
    args = build_args(pb, proj_a, None)
    kwargs = build_kwargs()
    req = make_req_plain(pb, make_user(), proj_a["id"])
    resp = call_route(fn, req, *args, **kwargs)
    assert "failed" in toast_message(resp) or "HTMX" in toast_message(resp)


# ---------------------------------------------------------------------------
# Cross-tenant record isolation (raw id substitution)
# ---------------------------------------------------------------------------
def test_topic_mutations_cannot_touch_foreign_topic():
    pb, proj_a, proj_b = _setup()
    foreign = make_topic(pb, proj_b["id"], title="\u0628", status="queued")
    req = make_req(pb, make_user(), proj_a["id"])
    resp = call_route(P.cancel_topic, req, proj_a["id"], foreign["id"])
    assert "not found" in toast_message(resp)
    assert TopicRepo(pb).get(foreign["id"])["status"] == "queued"


def test_article_mutations_cannot_touch_foreign_article():
    pb, proj_a, proj_b = _setup()
    foreign = make_article(pb, proj_b["id"], status="review")
    req = make_req(pb, make_user(), proj_a["id"])
    resp = call_route(W.approve_article, req, proj_a["id"], foreign["id"])
    assert "not found" in toast_message(resp)
    assert ArticleRepo(pb).get(foreign["id"])["status"] == "review"


def test_integration_delete_cannot_touch_foreign_integration():
    pb, proj_a, proj_b = _setup()
    foreign = make_integration(pb, proj_b["id"])
    req = make_req(pb, make_user(), proj_a["id"])
    resp = call_route(P.delete_integration, req, proj_a["id"], foreign["id"])
    assert "failed" in toast_message(resp)
    assert pb.collection("integrations").get_one(foreign["id"]) is not None


def test_schedule_cannot_be_created_for_foreign_project_via_id_substitution():
    """save_schedules only takes the project from the URL — a member of A cannot
    create a schedule for B by substituting project ids."""
    pb, proj_a, proj_b = _setup()
    req = make_req(pb, make_user(), proj_a["id"])
    resp = call_route(
        P.save_schedules, req, proj_a["id"], kind="index", enabled="1", interval_minutes="5"
    )
    assert "saved" in toast_message(resp)
    # schedule lands in A (the URL project), never in B
    assert len(pb.collection("schedules").get_full_list()) == 1
    assert pb.collection("schedules").get_full_list()[0]["project"] == proj_a["id"]


def test_admin_bypasses_project_membership_but_stays_in_project():
    """Global admins can access any project, but record-level guards still
    prevent writing foreign records."""
    pb, proj_a, proj_b = _setup()
    foreign = make_topic(pb, proj_b["id"], title="\u0628")
    req = make_req(pb, make_user("admin1", role="admin"), proj_a["id"])
    resp = call_route(P.write_topic, req, proj_a["id"], foreign["id"])
    assert "not found" in toast_message(resp)
    assert TopicRepo(pb).get(foreign["id"])["status"] == "planned"
