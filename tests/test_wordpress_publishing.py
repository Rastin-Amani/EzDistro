"""WordPress publishing provider tests: categories/tags, pagination,
update-instead-of-create, request ids, response status, unpublish, retry."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest

from app.jobs.context import JobContext, ProviderStack
from app.providers.base import ProviderError
from app.providers.publish.wordpress import WordPressPublisher
from app.repositories.jobs import JobEventRepo, JobRepo
from app.repositories.projects import DEFAULT_SETTINGS
from app.repositories.publishing_runs import PublishingRunRepo
from app.services.publishing_service import handle_publish_article
from app.services.settings import ProjectConfig
from tests.fake_providers import FakePublisher, FakeRegistry
from tests.fakes import FakePocketBase, default_unique_fields


def transport_for(handler):
    return httpx.MockTransport(handler)


# ---------------------------------------------------------------------------
# Provider: categories / tags / unpublish / status codes
# ---------------------------------------------------------------------------
def test_provider_categories_and_tags_paginated():
    def handler(request: httpx.Request) -> httpx.Response:
        endpoint = request.url.path.rsplit("/", 1)[-1]
        page = int(request.url.params.get("page", 1))
        items = [
            {
                "id": i + (page - 1) * 2,
                "name": f"{endpoint}-{i + (page - 1) * 2}",
                "slug": f"s{i}",
                "count": 1,
            }
            for i in range(2)
        ]
        return httpx.Response(200, json=items)

    pub = WordPressPublisher(
        base_url="https://s.test", username="u", password="p", transport=transport_for(handler)
    )
    categories = asyncio.run(pub.list_categories())
    tags = asyncio.run(pub.list_tags())
    assert len(categories) == 2
    assert categories[0]["name"] == "categories-0"
    assert tags[0]["name"] == "tags-0"
    assert all(isinstance(c, dict) and "id" in c for c in categories)
    asyncio.run(pub.aclose())


def test_provider_unpublish_sets_private():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen["status"] = body.get("status")
        return httpx.Response(
            200, json={"id": 77, "link": "https://s.test/?p=77", "status": body.get("status")}
        )

    pub = WordPressPublisher(
        base_url="https://s.test", username="u", password="p", transport=transport_for(handler)
    )
    result = asyncio.run(pub.unpublish_post(77))
    assert seen["status"] == "private"
    assert result.post_id == 77
    assert result.status_code == 200
    asyncio.run(pub.aclose())


def test_provider_paginated_fetch_stops_at_end():
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        page = int(request.url.params.get("page", 1))
        if page > 2:
            return httpx.Response(400, json={"code": "rest_post_invalid_page_number"})
        return httpx.Response(
            200,
            json=[
                {
                    "id": page,
                    "title": {"rendered": f"p{page}"},
                    "content": {"rendered": "<p>x</p>"},
                    "link": f"https://s.test/{page}",
                    "status": "publish",
                    "modified": "2026-01-01",
                }
            ]
            * 2,
        )

    pub = WordPressPublisher(
        base_url="https://s.test", username="u", password="p", transport=transport_for(handler)
    )
    posts = asyncio.run(pub.list_posts(per_page=2))
    assert len(posts) == 4  # 2 pages × 2
    assert posts[0].id == 1
    asyncio.run(pub.aclose())


# ---------------------------------------------------------------------------
# Publishing service: update-instead-of-create + audit fields
# ---------------------------------------------------------------------------
def make_project(pb: FakePocketBase) -> dict[str, Any]:
    project = pb.collection("projects").create(
        {"name": "پ", "slug": "p1", "language": "fa", "status": "active", "timezone": "Asia/Tehran"}
    )
    pb.collection("project_settings").create({"project": project["id"], **DEFAULT_SETTINGS})
    return project


def make_ctx(pb: FakePocketBase, registry: FakeRegistry, job: dict[str, Any]) -> JobContext:
    config = ProjectConfig.load(pb, job["project"])
    return JobContext(
        pb=pb,
        job=job,
        config=config,
        providers=ProviderStack(registry=registry, config=config),
        registry=registry,
        events=JobEventRepo(pb),
        set_progress=lambda jid, pct, **kw: None,
        request_cancel=lambda jid: False,
    )


def make_article(
    pb: FakePocketBase, project_id: str, *, wp_id: int | None = None
) -> dict[str, Any]:
    topic = pb.collection("topics").create(
        {"project": project_id, "title": "ت", "keyword": "ک", "status": "published"}
    )
    article = pb.collection("articles").create(
        {
            "project": project_id,
            "topicId": topic["id"],
            "title": "عنوان",
            "slug": "onvan",
            "status": "approved",
            "finalHtml": "<h1>عنوان</h1><p>محتوا</p>",
            "metaDescription": "م",
            "outlineVersion": 1,
        }
    )
    if wp_id:
        pb.collection("articles").update(
            article["id"],
            {
                "wordpressPostId": wp_id,
                "wordpressUrl": f"https://s.test/?p={wp_id}",
                "status": "published",
            },
        )
    return pb.collection("articles").get_one(article["id"])


def run_publish(
    pb: FakePocketBase,
    registry: FakeRegistry,
    article_id: str,
    action: str = "publish",
    key: str = "k",
) -> dict[str, Any]:
    job = JobRepo(pb).create(
        project=article_project(pb, article_id),
        type="publish_article",
        payload={"articleId": article_id, "action": action},
        idempotency_key=key,
        max_attempts=3,
    )
    return asyncio.run(handle_publish_article(make_ctx(pb, registry, job)))


def article_project(pb: FakePocketBase, article_id: str) -> str:
    return pb.collection("articles").get_one(article_id)["project"]


def test_create_when_no_wp_id_then_update_when_exists():
    pb = FakePocketBase(default_unique_fields())
    project = make_project(pb)
    article = make_article(pb, project["id"])
    registry = FakeRegistry()

    # no stored WP id → CREATE
    result = run_publish(pb, registry, article["id"], key="c1")
    assert result["updated"] is False
    assert len(registry.publisher.created) == 1
    assert len(registry.publisher.updated) == 0
    wp_id = result["postId"]

    # stored WP id → UPDATE, never a duplicate create
    result2 = run_publish(pb, registry, article["id"], action="update", key="u1")
    assert result2["updated"] is True
    assert len(registry.publisher.created) == 1  # still exactly one CREATE
    assert len(registry.publisher.updated) == 1
    assert registry.publisher.updated[0]["post_id"] == wp_id

    # each attempt recorded with request id + response status + attempt number
    runs = PublishingRunRepo(pb).list_for_article(article["id"])
    assert len(runs) == 2
    for run in runs:
        assert run["requestId"]
        assert run["wordpressPostId"] == wp_id
        assert run["responseMetadata"]["responseStatus"] == 200
    attempts = sorted(r["attempt"] for r in runs)
    assert attempts == [1, 2]  # attempt numbers increment across runs


def test_publish_records_failure_in_runs():
    pb = FakePocketBase(default_unique_fields())
    project = make_project(pb)
    article = make_article(pb, project["id"])
    registry = FakeRegistry()

    async def boom(*args, **kwargs):
        raise ProviderError("wp down", retryable=True)

    registry.publisher.create_post = boom  # type: ignore[method-assign]
    with pytest.raises(ProviderError):
        run_publish(pb, registry, article["id"], key="f1")

    runs = PublishingRunRepo(pb).list_for_article(article["id"])
    assert len(runs) == 1
    assert runs[0]["status"] == "failed"
    assert runs[0]["requestId"]
    assert "wp down" in runs[0]["error"]["message"]
    assert pb.collection("articles").get_one(article["id"])["status"] == "failed"


def test_unpublish_sets_private_and_records_run():
    pb = FakePocketBase(default_unique_fields())
    project = make_project(pb)
    article = make_article(pb, project["id"], wp_id=99)
    registry = FakeRegistry()

    result = run_publish(pb, registry, article["id"], action="unpublish", key="un1")
    assert result["unpublished"] is True
    assert registry.publisher.updated[-1]["status"] == "private"

    runs = PublishingRunRepo(pb).list_for_article(article["id"])
    assert runs[0]["mode"] == "unpublish"
    assert runs[0]["status"] == "unpublished"


def test_publish_gate_refuses_non_approved():
    pb = FakePocketBase(default_unique_fields())
    project = make_project(pb)
    article = make_article(pb, project["id"])
    pb.collection("articles").update(article["id"], {"status": "review"})
    registry = FakeRegistry()
    with pytest.raises(ProviderError) as excinfo:
        run_publish(pb, registry, article["id"], key="gate1")
    assert excinfo.value.details.get("required_state") == "approved"
    assert registry.publisher.created == []
