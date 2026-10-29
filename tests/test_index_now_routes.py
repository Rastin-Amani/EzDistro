"""The Index Now page: it renders, saves, installs the key, and submits.

These pin the parts that silently break in production:
- the tab renders before anything is configured (no 500 on the project page);
- saving derives the host from the WordPress connection when left empty;
- saving INSTALLS the key through WordPress and stores its address (keyLocation),
  with a graceful fallback when the site refuses the upload;
- a key is generated once and survives later saves;
- manual submission refuses URLs that are not on the configured host;
- the key file download serves exactly the key (engines compare it byte-wise).
"""

from __future__ import annotations

import asyncio

import pytest

from app.api import indexing as I
from app.api import projects as P
from app.repositories.integrations import IntegrationRepo
from app.repositories.jobs import JobRepo
from tests.fake_providers import FakePublisher
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

MEDIA_HOST = "https://blog.example.com"


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
            configuration={"base_url": MEDIA_HOST, "username": "u"},
            secrets_enc="",
            enabled=True,
        )
    req = make_req(pb, make_user(), project["id"])
    return pb, project, req


@pytest.fixture
def wp(monkeypatch):
    """A WordPress connection whose media upload works and whose key URL resolves."""
    pub = FakePublisher()

    async def upload_media(**kwargs):
        pub.next_media_id += 1
        filename = kwargs["filename"]
        pub.media.append({"filename": filename, "size": len(kwargs["data"])})
        return {
            "id": pub.next_media_id,
            "url": f"{MEDIA_HOST}/wp-content/uploads/2026/10/{filename}",
            "mime_type": "text/plain",
        }

    pub.upload_media = upload_media  # type: ignore[method-assign]

    class Registry:
        def get_publisher_provider(self, *args, **kwargs):
            return pub

    async def verified(self):
        return self.key_url

    monkeypatch.setattr(I, "_registry", lambda request: Registry())
    # The key-file check does a real HTTP GET; the upload/URL logic is what is
    # under test here (the fetch itself is covered in test_index_now.py).
    monkeypatch.setattr(I.IndexNowClient, "verify_key", verified)
    return pub


def _save(req, project, **kwargs):
    fields = {
        "host": "",
        "endpoint": "",
        "key_location": "",
        "auto_publish": "1",
        "auto_update": "1",
    }
    fields.update(kwargs)
    return asyncio.run(I.save_config(req, project["id"], **fields))


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


def test_saving_installs_the_key_on_wordpress(wp):
    pb, project, req = _setup()
    resp = _save(req, project)

    config = I.load_config(pb, project["id"])
    assert config["host"] == "blog.example.com"
    assert len(config["key"]) == 32
    # uploaded through WordPress, and its address is what submissions carry
    assert config["key_location"] == (
        f"{MEDIA_HOST}/wp-content/uploads/2026/10/{config['key']}.txt"
    )
    assert wp.media[-1]["filename"] == f"{config['key']}.txt"
    assert "installed" in toast_message(resp).lower()


def test_install_is_skipped_when_a_location_is_given(wp):
    pb, project, req = _setup()
    _save(req, project, key_location="https://blog.example.com/key.txt")
    config = I.load_config(pb, project["id"])
    assert config["key_location"] == "https://blog.example.com/key.txt"
    assert wp.media == []


def test_save_falls_back_to_manual_when_wordpress_is_not_connected(wp):
    pb, project, req = _setup(publisher=False)
    # no WordPress connection → the host cannot be guessed, so it is typed in
    resp = _save(req, project, host="blog.example.com")
    config = I.load_config(pb, project["id"])
    assert config["key_location"] == ""
    assert len(config["key"]) == 32  # the key is still generated
    assert "manually" in toast_message(resp)


def test_save_survives_a_wordpress_that_refuses_the_upload(wp):
    wp.upload_media_error = RuntimeError("upload_filetype_disallowed")

    async def refuse(**_kwargs):
        raise wp.upload_media_error

    wp.upload_media = refuse  # type: ignore[method-assign]
    pb, project, req = _setup()
    resp = _save(req, project)

    config = I.load_config(pb, project["id"])
    assert config["key_location"] == ""
    assert "Could not upload" in toast_message(resp)


def test_saving_generates_a_key_once_and_keeps_it(wp):
    pb, project, req = _setup()
    _save(req, project)
    first = I.load_config(pb, project["id"])["key"]

    _save(req, project, host="blog.example.com", auto_publish="")
    again = I.load_config(pb, project["id"])
    assert again["key"] == first  # a save must not silently rotate the key
    assert again["auto_publish"] is False
    assert again["key_location"].endswith(f"{first}.txt")


def test_regenerating_the_key_reinstalls_the_new_file(wp):
    pb, project, req = _setup()
    _save(req, project)
    old = I.load_config(pb, project["id"])

    asyncio.run(I.regenerate_key(req, project["id"]))
    new = I.load_config(pb, project["id"])
    assert new["key"] != old["key"]
    assert new["key_location"].endswith(f"{new['key']}.txt")
    assert wp.media[-1]["filename"] == f"{new['key']}.txt"


def test_install_route_reports_failure_without_losing_the_config(wp):
    pb, project, req = _setup()
    _save(req, project, key_location="https://blog.example.com/key.txt")

    async def refuse(**_kwargs):
        raise RuntimeError("wp down")

    wp.upload_media = refuse  # type: ignore[method-assign]
    resp = asyncio.run(I.install_key_file(req, project["id"]))
    assert "Could not upload" in toast_message(resp)
    assert I.load_config(pb, project["id"])["key_location"] == "https://blog.example.com/key.txt"


def test_install_route_stores_the_verified_location(wp):
    pb, project, req = _setup()
    _save(req, project, key_location="https://blog.example.com/old.txt")
    asyncio.run(I.install_key_file(req, project["id"]))
    location = I.load_config(pb, project["id"])["key_location"]
    assert location.startswith(f"{MEDIA_HOST}/wp-content/uploads/")


def test_save_heals_a_database_that_predates_the_category(monkeypatch, wp):
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
    config = I.load_config(pb, project["id"])
    assert config["key"] and config["key_location"]
    assert "error" not in toast_message(resp).lower()


def test_key_file_download_serves_exactly_the_key(wp):
    pb, project, req = _setup()
    _save(req, project)
    key = I.load_config(pb, project["id"])["key"]
    resp = I.download_key_file(req, project["id"])
    assert resp.body.decode() == key
    assert f'filename="{key}.txt"' in resp.headers["content-disposition"]


def test_tab_renders_with_an_installed_key_jobs_and_published_articles(wp):
    pb, project, req = _setup()
    _save(req, project)
    make_article(pb, project["id"], status="published", wp_id=1001)
    call_route(I.submit_urls, req, project["id"], urls="https://blog.example.com/a")

    body = _render(pb, project)
    assert "Installed on your site" in body
    assert "/wp-content/uploads/" in body
    assert "manual" in body  # the submission row's source
    assert "search.google.com/search-console/inspect" in body


# ---------------------------------------------------------------------------
# Submission
# ---------------------------------------------------------------------------
def test_manual_submission_rejects_foreign_urls(wp):
    pb, project, req = _setup()
    _save(req, project)
    resp = call_route(I.submit_urls, req, project["id"], urls="https://elsewhere.io/post")
    assert "None of those URLs is on blog.example.com" in toast_message(resp)
    assert not [
        j for j in pb.collection("jobs").get_full_list({}) if j["type"] == "notify_search_engines"
    ]


def test_manual_submission_queues_the_job_and_follows_the_refresh_event(wp):
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
