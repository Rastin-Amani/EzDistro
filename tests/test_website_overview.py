from app.api.dashboard import dashboard
from app.api.projects import project_detail
from app.repositories.jobs import JobRepo
from tests.helpers import (
    call_route,
    make_article,
    make_member,
    make_pb,
    make_project,
    make_req,
    make_topic,
    make_user,
)


def test_overview_counts_real_content_and_excludes_other_websites():
    pb = make_pb()
    site = make_project(pb, name="Visible website")
    other = make_project(pb, slug="private", name="Private website")
    make_member(pb, site["id"])
    make_topic(pb, site["id"])
    make_article(pb, site["id"], status="review")
    make_article(pb, other["id"], status="published")
    req = make_req(pb, make_user(), site["id"])
    response = call_route(dashboard, req)
    summary = response.context["project_overviews"]
    assert len(summary) == 1
    assert summary[0]["planned"] == 1
    assert summary[0]["review"] == 1
    assert summary[0]["published"] == 0
    assert "Private website" not in response.body.decode()
    detail = call_route(project_detail, req, site["id"])
    assert detail.context["active_tab"] == "overview"
    assert "Website overview" in detail.body.decode()
    pb.collection("jobs").create(
        {"project": other["id"], "type": "write_article", "status": "running"}
    )
    rows, count = JobRepo(pb).search(project_scope=[site["id"]])
    assert rows == [] and count == 0
    rows, count = JobRepo(pb).search(project_scope=[])
    assert rows == [] and count == 0
