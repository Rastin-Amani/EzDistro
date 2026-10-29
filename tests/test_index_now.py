"""Index Now: key handling, queueing rules, the job handler, and the publish hook.

The rules that matter:

- nothing is submitted without a key + host + an enabled connection (a missing
  key file makes every submission silently dropped by the engines);
- a publish/update queues ONE notification per article per hour, a manual
  request always runs;
- a publish never fails because of the notification;
- the job record is the submission history.
"""

from __future__ import annotations

import asyncio
import time

from app.providers.base import PermanentError
from app.providers.indexnow import (
    DEFAULT_ENDPOINT,
    IndexNowClient,
    generate_key,
    gsc_inspect_link,
)
from app.providers.indexnow import (
    _selfcheck as indexnow_selfcheck,
)
from app.repositories.integrations import IntegrationRepo
from app.repositories.jobs import JobRepo
from app.services import search_engines
from app.services.search_engines import handle_notify_search_engines
from tests.fake_providers import FakeIndexer, FakeRegistry
from tests.test_wordpress_publishing import make_article, make_ctx, make_project, run_publish


def _enable(pb, project_id, *, key=None, host="s.test", **flags):
    cfg = {
        "host": host,
        "key": key or generate_key(),
        "endpoint": DEFAULT_ENDPOINT,
        "auto_publish": flags.get("auto_publish", True),
        "auto_update": flags.get("auto_update", True),
    }
    return IntegrationRepo(pb).create(
        project=project_id,
        category="indexing",
        provider="indexnow",
        display_name="Index Now",
        configuration=cfg,
        secrets_enc="",
        enabled=flags.get("enabled", True),
    )


def _notify_jobs(pb):
    return [
        j for j in pb.collection("jobs").get_full_list({}) if j["type"] == "notify_search_engines"
    ]


# ---------------------------------------------------------------------------
# Provider
# ---------------------------------------------------------------------------
def test_provider_selfcheck():
    indexnow_selfcheck()


def test_key_is_generated_and_hosted_at_the_site_root():
    key = generate_key()
    client = IndexNowClient(host="example.com", key=key)
    assert client.key_url == f"https://example.com/{key}.txt"
    assert len(key) == 32


def test_submission_rejects_a_url_outside_the_configured_host():
    client = IndexNowClient(host="example.com", key=generate_key())
    try:
        asyncio.run(client.submit(["https://example.com/a", "https://evil.example.net/b"]))
    except PermanentError as exc:
        assert "not on example.com" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("a foreign URL was accepted")


def test_gsc_link_points_at_url_inspection_for_the_page():
    link = gsc_inspect_link("https://example.com/blog/post", "https://example.com")
    assert link.startswith("https://search.google.com/search-console/inspect?")
    # the property is a bare domain; an https:// value sends Google to the picker
    assert "resource_id=example.com" in link
    assert "url=https%3A%2F%2Fexample.com%2Fblog%2Fpost" in link


# ---------------------------------------------------------------------------
# Queueing rules
# ---------------------------------------------------------------------------
def test_install_key_file_uploads_the_key_as_media():
    from tests.fake_providers import FakePublisher

    pub = FakePublisher()
    media = asyncio.run(search_engines.install_key_file(pub, key="abc12345"))
    assert media["url"].endswith("/abc12345.txt")
    assert pub.media[-1]["filename"] == "abc12345.txt"
    assert pub.media[-1]["size"] == len("abc12345")


def test_queue_does_nothing_without_a_connection():
    home = make_project_home()
    assert (
        search_engines.queue(
            home["pb"],
            project=home["project"],
            urls=[home["url"]],
            source=search_engines.SOURCE_PUBLISH,
        )
        is None
    )
    assert _notify_jobs(home["pb"]) == []


def make_project_home():
    from tests.fakes import FakePocketBase, default_unique_fields

    pb = FakePocketBase(default_unique_fields())
    project = make_project(pb)
    return {"pb": pb, "project": project["id"], "url": "https://s.test/?p=1"}


def test_queue_is_deduped_per_article_per_hour():
    home = make_project_home()
    _enable(home["pb"], home["project"])
    first = search_engines.queue(
        home["pb"],
        project=home["project"],
        urls=[home["url"]],
        source=search_engines.SOURCE_PUBLISH,
        article="a1",
    )
    second = search_engines.queue(
        home["pb"],
        project=home["project"],
        urls=[home["url"]],
        source=search_engines.SOURCE_PUBLISH,
        article="a1",
    )
    assert first and second and first["id"] == second["id"]
    assert len(_notify_jobs(home["pb"])) == 1


def test_queue_respects_the_auto_toggles():
    home = make_project_home()
    _enable(home["pb"], home["project"], auto_update=False)
    assert (
        search_engines.queue(
            home["pb"],
            project=home["project"],
            urls=[home["url"]],
            source=search_engines.SOURCE_UPDATE,
            article="a1",
        )
        is None
    )
    assert (
        search_engines.queue(
            home["pb"],
            project=home["project"],
            urls=[home["url"]],
            source=search_engines.SOURCE_PUBLISH,
            article="a1",
        )
        is not None
    )


def test_manual_always_runs_even_with_auto_off():
    home = make_project_home()
    _enable(home["pb"], home["project"], auto_publish=False, auto_update=False)
    assert (
        search_engines.queue(
            home["pb"],
            project=home["project"],
            urls=[home["url"]],
            source=search_engines.SOURCE_MANUAL,
        )
        is not None
    )


def test_disabled_connection_submits_nothing():
    home = make_project_home()
    _enable(home["pb"], home["project"], enabled=False)
    assert (
        search_engines.queue(
            home["pb"],
            project=home["project"],
            urls=[home["url"]],
            source=search_engines.SOURCE_MANUAL,
        )
        is None
    )


# ---------------------------------------------------------------------------
# Job handler
# ---------------------------------------------------------------------------
def test_handler_submits_and_records_the_receipt():
    home = make_project_home()
    row = _enable(home["pb"], home["project"])
    registry = FakeRegistry()
    job = JobRepo(home["pb"]).create(
        project=home["project"],
        type="notify_search_engines",
        payload={"urls": [home["url"]], "source": "manual"},
        idempotency_key=f"manual:{time.time()}",
    )
    result = asyncio.run(handle_notify_search_engines(make_ctx(home["pb"], registry, job)))

    assert result["accepted"] and result["statusCode"] == 202
    assert registry.indexing.submissions == [[home["url"]]]
    assert registry.indexing.closed is True  # the client is always closed
    assert IntegrationRepo(home["pb"]).get(row["id"])["healthStatus"] == "healthy"


def test_handler_skips_without_a_connection():
    home = make_project_home()
    registry = FakeRegistry()
    job = JobRepo(home["pb"]).create(
        project=home["project"],
        type="notify_search_engines",
        payload={"urls": [home["url"]], "source": "manual"},
        idempotency_key=f"manual:{time.time()}",
    )
    result = asyncio.run(handle_notify_search_engines(make_ctx(home["pb"], registry, job)))
    assert result["skipped"] is True
    assert registry.indexing.submissions == []


def test_handler_marks_the_connection_unhealthy_when_the_key_is_rejected():
    home = make_project_home()
    row = _enable(home["pb"], home["project"])
    registry = FakeRegistry()
    registry.indexing = FakeIndexer(status_code=403, accepted=False)

    async def reject(urls):
        raise PermanentError("IndexNow: the key was rejected")

    registry.indexing.submit = reject  # type: ignore[method-assign]
    job = JobRepo(home["pb"]).create(
        project=home["project"],
        type="notify_search_engines",
        payload={"urls": [home["url"]], "source": "manual"},
        idempotency_key=f"manual:{time.time()}",
    )
    try:
        asyncio.run(handle_notify_search_engines(make_ctx(home["pb"], registry, job)))
    except PermanentError:
        pass
    else:  # pragma: no cover
        raise AssertionError("a rejected submission did not fail the job")
    assert IntegrationRepo(home["pb"]).get(row["id"])["healthStatus"] == "unhealthy"


# ---------------------------------------------------------------------------
# The publish hook
# ---------------------------------------------------------------------------
def test_publish_queues_the_notification():
    from tests.fakes import FakePocketBase, default_unique_fields

    pb = FakePocketBase(default_unique_fields())
    project = make_project(pb)
    _enable(pb, project["id"])
    article = make_article(pb, project["id"])
    result = run_publish(pb, FakeRegistry(), article["id"])
    jobs = _notify_jobs(pb)
    assert len(jobs) == 1
    assert jobs[0]["payload"]["urls"] == [result["url"]]
    assert jobs[0]["payload"]["source"] == search_engines.SOURCE_PUBLISH


def test_publish_still_succeeds_when_notification_bookkeeping_breaks(monkeypatch):
    from tests.fakes import FakePocketBase, default_unique_fields

    pb = FakePocketBase(default_unique_fields())
    project = make_project(pb)
    _enable(pb, project["id"])
    article = make_article(pb, project["id"])

    def boom(*_args, **_kwargs):
        raise RuntimeError("pb hiccup")

    monkeypatch.setattr(search_engines, "queue", boom)
    result = run_publish(pb, FakeRegistry(), article["id"])
    assert result["postId"]
