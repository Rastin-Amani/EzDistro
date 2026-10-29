"""The Index Now page: it renders, saves, generates a key, and submits.

These pin the parts that silently break in production:
- the tab renders even before anything is configured (no 500 on the project page);
- saving derives the host from the WordPress connection when left empty;
- a key is generated once and survives later saves;
- manual submission refuses URLs that are not on the configured host;
- the key file download serves exactly the key (engines compare it byte-wise).
"""

from __future__ import annotations

from app.api import indexing as I
from app.api import projects as P
from app.repositories.integrations import IntegrationRepo
from app.repositories.jobs import JobRepo
from tests.helpers import (
    call_route,
    hx_events,
    make_member,
    make_pb,
    make_project,
    make_req,
    make_user,
    toast_message,
)


def _setup():
    pb = make_pb()
    project = make_project(pb)
    make_member(pb, project["id"], "u1", role="owner")
    IntegrationRepo(pb).create(
        project=project["id"],
        category="publisher",
        provider="wordpress",
        display_name="WP",
        configuration={"base_url": "https://blog.example.com", "username": "u"},
        secrets_enc="",
        enabled=True,
    )
    req = make_req(pb, make_user(), project["id"])
    return pb, project, req


def _render(pb, project) -> str:
    req = make_req(pb, make_user(), project["id"])
    resp = call_route(P.project_tab, req, project["id"], "index_now")
    assert resp.status_code == 200
    return resp.body.decode()


def test_tab_renders_before_configuration():
    pb, project, _ = _setup()
    body = _render(pb, project)
    assert 'id="index-now-tab"' in body
    assert "Google: not on IndexNow" in body
    assert "blog.example.com" in body  # default host from the WordPress connection


def test_saving_generates_a_key_and_keeps_it_on_the_next_save():
    pb, project, req = _setup()
    resp = call_route(
        I.save_config,
        req,
        project["id"],
        host="",
        endpoint="",
        key_location="",
        auto_publish="1",
        auto_update="1",
    )
    assert resp.status_code == 200
    config = I.load_config(pb, project["id"])
    assert config["host"] == "blog.example.com"
    assert len(config["key"]) == 32
    assert config["auto_publish"] and config["auto_update"]

    first_key = config["key"]
    call_route(
        I.save_config,
        req,
        project["id"],
        host="blog.example.com",
        endpoint="",
        key_location="",
        auto_publish="",
        auto_update="1",
    )
    again = I.load_config(pb, project["id"])
    assert again["key"] == first_key  # a save must not silently rotate the key
    assert again["auto_publish"] is False


def test_regenerating_the_key_replaces_it():
    pb, project, req = _setup()
    call_route(
        I.save_config,
        req,
        project["id"],
        host="blog.example.com",
        endpoint="",
        key_location="",
        auto_publish="1",
        auto_update="1",
    )
    old = I.load_config(pb, project["id"])["key"]
    call_route(I.regenerate_key, req, project["id"])
    assert I.load_config(pb, project["id"])["key"] != old


def test_key_file_download_serves_exactly_the_key():
    pb, project, req = _setup()
    call_route(
        I.save_config,
        req,
        project["id"],
        host="blog.example.com",
        endpoint="",
        key_location="",
        auto_publish="1",
        auto_update="1",
    )
    key = I.load_config(pb, project["id"])["key"]
    resp = I.download_key_file(req, project["id"])
    assert resp.body.decode() == key
    assert f'filename="{key}.txt"' in resp.headers["content-disposition"]


def test_manual_submission_rejects_foreign_urls():
    pb, project, req = _setup()
    call_route(
        I.save_config,
        req,
        project["id"],
        host="blog.example.com",
        endpoint="",
        key_location="",
        auto_publish="1",
        auto_update="1",
    )
    resp = call_route(I.submit_urls, req, project["id"], urls="https://elsewhere.io/post")
    assert "None of those URLs is on blog.example.com" in toast_message(resp)
    assert not [
        j for j in pb.collection("jobs").get_full_list({}) if j["type"] == "notify_search_engines"
    ]


def test_tab_renders_with_a_configured_connection_jobs_and_published_articles():
    from tests.helpers import make_article

    pb, project, req = _setup()
    call_route(
        I.save_config,
        req,
        project["id"],
        host="blog.example.com",
        endpoint="",
        key_location="",
        auto_publish="1",
        auto_update="1",
    )
    make_article(pb, project["id"], status="published", wp_id=1001)
    call_route(I.submit_urls, req, project["id"], urls="https://blog.example.com/a")

    body = _render(pb, project)
    assert "Download key file" in body
    assert "blog.example.com" in body
    assert "manual" in body  # the submission row's source
    assert "search.google.com/search-console/inspect" in body


def test_manual_submission_queues_the_job_and_follows_the_refresh_event():
    pb, project, req = _setup()
    call_route(
        I.save_config,
        req,
        project["id"],
        host="blog.example.com",
        endpoint="",
        key_location="",
        auto_publish="1",
        auto_update="1",
    )
    resp = call_route(
        I.submit_urls,
        req,
        project["id"],
        urls="blog.example.com/a\nhttps://blog.example.com/b\nhttps://evil.io/c",
    )
    assert "Submitting 2 URL(s)" in toast_message(resp)
    assert "refreshIndexNow" in hx_events(resp)
    jobs = [
        j for j in pb.collection("jobs").get_full_list({}) if j["type"] == "notify_search_engines"
    ]
    assert len(jobs) == 1
    assert jobs[0]["payload"]["urls"] == [
        "https://blog.example.com/a",
        "https://blog.example.com/b",
    ]
    assert JobRepo(pb).get(jobs[0]["id"])["status"] == "pending"
