"""The Index Now page: it renders, generates/replaces the key, verifies a URL, submits.

EzDistro never writes to the site: the key file is placed by the owner and the
app verifies the URL it will be read from. These tests pin that contract:

- the tab renders before anything is configured (no 500 on the project page);
- saving derives the host from the WordPress connection when left empty;
- saving generates a key once and never rotates it silently;
- "New key" replaces it and clears the old file URL;
- verification checks the domain root AND the configured URL;
- manual submission refuses URLs that are not on the configured host;
- the key download serves exactly the key (engines compare it byte-wise).
"""

from __future__ import annotations

import asyncio

from app.api import indexing as I
from app.api import projects as P
from app.providers.indexnow import KeyCheck
from app.repositories.integrations import IntegrationRepo
from app.repositories.jobs import JobRepo
from tests.helpers import (
    call_route,
    hx_events,
    make_article,
    make_member,
    make_pb,
    make_project,
    make_req,
    make_user,
    toast_message,
)

SITE = "https://blog.example.com"


def _setup(*, publisher: bool = True):
    pb = make_pb()
    project = make_project(pb)
    make_member(pb, project["id"], "u1", role="owner")
    if publisher:
        IntegrationRepo(pb).create(
            project=project["id"],
            category="publisher",
            provider="wordpress",
            display_name="WP",
            configuration={"base_url": SITE, "username": "u"},
            secrets_enc="",
            enabled=True,
        )
    req = make_req(pb, make_user(), project["id"])
    return pb, project, req


def _save(req, project, **kwargs):
    fields = {
        "host": "",
        "endpoint": "",
        "key_location": "",
        "auto_publish": "1",
        "auto_update": "1",
    }
    fields.update(kwargs)
    return I.save_config(req, project["id"], **fields)


def _render(pb, project) -> str:
    req = make_req(pb, make_user(), project["id"])
    resp = call_route(P.project_tab, req, project["id"], "index_now")
    assert resp.status_code == 200
    return resp.body.decode()


# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------
def test_tab_renders_before_configuration():
    pb, project, _ = _setup()
    body = _render(pb, project)
    assert 'id="index-now-tab"' in body
    assert "Google: not on IndexNow" in body
    assert "blog.example.com" in body  # default host from the WordPress connection


def test_saving_generates_a_key_without_touching_the_site():
    pb, project, req = _setup()
    resp = _save(req, project)

    config = I.load_config(pb, project["id"])
    assert config["host"] == "blog.example.com"
    assert len(config["key"]) == 32
    assert config["key_location"] == ""  # never uploaded, never guessed
    assert config["auto_publish"] and config["auto_update"]
    assert "Key generated" in toast_message(resp)


def test_saving_never_rotates_existing_key():
    pb, project, req = _setup()
    _save(req, project)
    first = I.load_config(pb, project["id"])["key"]

    _save(req, project, host="blog.example.com", auto_publish="")
    again = I.load_config(pb, project["id"])
    assert again["key"] == first
    assert again["auto_publish"] is False


def test_saving_stores_a_key_file_url_and_flags_a_host_mismatch():
    pb, project, req = _setup()
    _save(req, project, key_location="https://cdn.example.net/key.txt")
    assert I.load_config(pb, project["id"])["key_location"] == "https://cdn.example.net/key.txt"

    body = _render(pb, project)
    assert "The key file URL and the website host disagree" in body
    assert "cdn.example.net" in body


def test_save_requires_a_host_when_no_wordpress_connection_exists():
    pb, project, req = _setup(publisher=False)
    resp = _save(req, project)
    assert "Enter the website host" in toast_message(resp)
    assert I.load_config(pb, project["id"]) == {}

    _save(req, project, host="blog.example.com")
    assert I.load_config(pb, project["id"])["host"] == "blog.example.com"


def test_new_key_replaces_it_and_clears_the_old_file_url():
    pb, project, req = _setup()
    _save(req, project, key_location="https://blog.example.com/old.txt")
    old = I.load_config(pb, project["id"])

    I.regenerate_key(req, project["id"])
    new = I.load_config(pb, project["id"])
    assert new["key"] != old["key"]
    assert new["key_location"] == ""  # the old URL serves the old key
    assert new["host"] == old["host"]


def test_save_heals_a_database_that_predates_the_category(monkeypatch):
    """`category: Invalid value: indexing.` must not be a dead end."""
    from pocketbase.errors import ClientResponseError

    pb, project, req = _setup()
    original = IntegrationRepo.create
    calls = {"n": 0}

    def flaky(self, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise ClientResponseError(
                "Bad Request",
                status=400,
                data={
                    "data": {
                        "category": {
                            "code": "validation_invalid_value",
                            "message": "Invalid value: indexing.",
                        }
                    }
                },
            )
        return original(self, **kwargs)

    healed: list[bool] = []
    monkeypatch.setattr(IntegrationRepo, "create", flaky)
    monkeypatch.setattr(I, "_heal_category", lambda pb: healed.append(True))

    resp = _save(req, project)
    assert healed == [True]  # the reconciler ran once
    assert I.load_config(pb, project["id"])["key"]
    assert "error" not in toast_message(resp).lower()


# ---------------------------------------------------------------------------
# Key file
# ---------------------------------------------------------------------------
def test_key_file_download_serves_exactly_the_key():
    pb, project, req = _setup()
    _save(req, project)
    key = I.load_config(pb, project["id"])["key"]
    resp = I.download_key_file(req, project["id"])
    assert resp.body.decode() == key
    assert f'filename="{key}.txt"' in resp.headers["content-disposition"]


def test_verify_reports_which_url_serves_the_key(monkeypatch):
    pb, project, req = _setup()
    _save(req, project)
    config = I.load_config(pb, project["id"])
    root = f"https://{config['host']}/{config['key']}.txt"
    checked: list[str] = []

    class StubClient:
        root_key_url = root
        key_location = ""
        key_url = root

        async def check_key(self, url):
            checked.append(url)
            return KeyCheck(url, True, 200, "ok")

        async def aclose(self):
            pass

    class Registry:
        def get_indexing_provider(self, *args, **kwargs):
            return StubClient()

    monkeypatch.setattr(I, "_registry", lambda request: Registry())
    resp = asyncio.run(I.verify_key(req, project["id"]))
    assert checked == [root]
    assert f"Key file verified at {root}" in toast_message(resp)


def test_verify_names_both_urls_when_none_serves_the_key(monkeypatch):
    pb, project, req = _setup()
    _save(req, project, key_location="https://blog.example.com/blog/key.txt")

    class StubClient:
        root_key_url = "https://blog.example.com/<key>.txt"
        key_location = "https://blog.example.com/blog/key.txt"
        key_url = key_location

        async def check_key(self, url):
            if url.endswith("/blog/key.txt"):
                return KeyCheck(url, False, 301, "redirects to /wp-login.php")
            return KeyCheck(url, False, 404, "HTTP 404")

        async def aclose(self):
            pass

    class Registry:
        def get_indexing_provider(self, *args, **kwargs):
            return StubClient()

    monkeypatch.setattr(I, "_registry", lambda request: Registry())
    resp = asyncio.run(I.verify_key(req, project["id"]))
    message = toast_message(resp)
    assert "No key file is reachable" in message
    assert "HTTP 404" in message and "redirects to /wp-login.php" in message


def test_tab_renders_with_key_jobs_and_published_articles():
    pb, project, req = _setup()
    _save(req, project)
    make_article(pb, project["id"], status="published", wp_id=1001)
    call_route(I.submit_urls, req, project["id"], urls="https://blog.example.com/a")

    body = _render(pb, project)
    assert "Put this file on your site yourself" in body
    assert "manual" in body  # the submission row's source
    assert "search.google.com/search-console/inspect" in body


# ---------------------------------------------------------------------------
# Submission
# ---------------------------------------------------------------------------
def test_manual_submission_rejects_foreign_urls():
    pb, project, req = _setup()
    _save(req, project)
    resp = call_route(I.submit_urls, req, project["id"], urls="https://elsewhere.io/post")
    assert "None of those URLs is on blog.example.com" in toast_message(resp)
    assert not [
        j for j in pb.collection("jobs").get_full_list({}) if j["type"] == "notify_search_engines"
    ]


def test_manual_submission_queues_the_job_and_follows_the_refresh_event():
    pb, project, req = _setup()
    _save(req, project)
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
