"""Inbound internal link building: the pure insertion + the job handler."""

from __future__ import annotations

import asyncio
import re
from typing import Any

from app.jobs.context import JobContext, ProviderStack
from app.providers.base import VectorPoint, WPPost
from app.repositories.articles import ArticleRepo
from app.repositories.jobs import JobEventRepo, JobRepo
from app.repositories.topics import TopicRepo
from app.services.link_building import handle_build_internal_links, insert_contextual_link
from app.services.settings import ProjectConfig
from tests.fake_providers import FakeRegistry
from tests.fakes import FakePocketBase, default_unique_fields
from tests.helpers import make_project

TARGET = "https://gyme.cloud/gym-management-software"
PHRASE = "gym management software"


# ---------------------------------------------------------------------------
# insert_contextual_link — must only ever ADD an anchor
# ---------------------------------------------------------------------------
def test_insert_contextual_link_preserves_visible_text():
    html = f"<p>Intro.</p><p>We compare {PHRASE} tools.</p>"
    out = insert_contextual_link(html, TARGET, [PHRASE])
    assert out is not None
    assert f'<a href="{TARGET}">{PHRASE}</a>' in out
    # every character of text survives; only markup was added
    assert re.sub(r"<[^>]+>", "", out) == re.sub(r"<[^>]+>", "", html)
    assert out.count("<a ") == 1


def test_insert_contextual_link_never_nests_an_anchor():
    html = f'<p><a href="https://other.test">{PHRASE}</a> is popular.</p>'
    assert insert_contextual_link(html, TARGET, [PHRASE]) is None


def test_insert_contextual_link_needs_a_whole_word_in_a_text_node():
    assert insert_contextual_link("<p>nothing here</p>", TARGET, [PHRASE]) is None
    # inside a tag/attribute is not a text node
    html = f'<p><img src="/{PHRASE}.png" alt="x" /> text</p>'
    assert insert_contextual_link(html, TARGET, [PHRASE]) is None
    # a longer phrase wins over a substring of it
    html = f"<p>the {PHRASE} guide</p>"
    out = insert_contextual_link(html, TARGET, ["software", PHRASE])
    assert out is not None and f">{PHRASE}</a>" in out


# ---------------------------------------------------------------------------
# handler
# ---------------------------------------------------------------------------
def _ctx(pb: FakePocketBase, registry: FakeRegistry, job: dict[str, Any]) -> JobContext:
    config = ProjectConfig.load(pb, job["project"])
    return JobContext(
        pb=pb,
        job=job,
        config=config,
        providers=ProviderStack(registry=registry, config=config),
        registry=registry,
        events=JobEventRepo(pb),
        set_progress=lambda *a, **k: None,
        request_cancel=lambda jid: False,
    )


def _setup() -> tuple[FakePocketBase, dict[str, Any], FakeRegistry, dict[str, Any]]:
    pb = FakePocketBase(default_unique_fields())
    project = make_project(pb)
    # every seeded page is "similar enough" for the test
    settings = pb.collection("project_settings").get_first_list_item(
        filter=f'project="{project["id"]}"'
    )
    pb.collection("project_settings").update(settings["id"], {"similarityThreshold": 0.0})

    topic = TopicRepo(pb).create(
        project=project["id"], title="Gym management software", keyword=PHRASE
    )
    article = ArticleRepo(pb).create(
        project=project["id"],
        topic_id=topic["id"],
        title="Gym Management Software: A Buyer's Guide",
        slug="gym-management-software",
    )
    ArticleRepo(pb).update(
        article["id"],
        {"status": "published", "wordpressUrl": TARGET, "wordpressId": 42},
    )
    registry = FakeRegistry()
    registry.vector.points["p:7:0"] = VectorPoint(
        id="p:7:0",
        vector=[0.5] * 8,
        payload={
            "project_id": project["id"],
            "document_id": "doc-7",
            "source_id": "7",
            "source_url": "https://site.test/old-page",
            "title": "Old page",
            "chunk_index": 0,
            "content_hash": "h",
            "chunk_text": "old page",
        },
    )
    registry.publisher.posts = [
        WPPost(
            id=7,
            title="Old page",
            content_html=f"<p>We compare {PHRASE} tools.</p>",
            link="https://site.test/old-page",
            status="publish",
        )
    ]
    job = JobRepo(pb).create(
        project=project["id"],
        type="build_internal_links",
        payload={"articleId": article["id"]},
        idempotency_key="links:1",
        max_attempts=3,
        entity_type="article",
        entity_id=article["id"],
    )
    return pb, project, registry, job


def test_handler_links_a_related_page_and_refreshes_the_corpus():
    pb, project, registry, job = _setup()
    article_id = job["payload"]["articleId"]

    result = asyncio.run(handle_build_internal_links(_ctx(pb, registry, job)))

    assert result["linked"] == 1
    updated = registry.publisher.updated[0]
    assert updated["post_id"] == 7
    assert f'<a href="{TARGET}">{PHRASE}</a>' in updated["html"]
    # the page we edited is re-indexed so the corpus matches what is live
    reindex = [j for j in pb.collection("jobs").get_full_list() if j["type"] == "index_document"]
    assert [j["payload"]["sourceId"] for j in reindex] == ["7"]
    assert reindex[0]["entityId"] == article_id
    assert project["id"] == pb.collection("articles").get_one(article_id)["project"]


def test_handler_skips_pages_that_already_link_to_the_target():
    pb, project, registry, job = _setup()
    registry.publisher.posts[0] = WPPost(
        id=7,
        title="Old page",
        content_html=f'<p>We compare <a href="{TARGET}">{PHRASE}</a> tools.</p>',
        link="https://site.test/old-page",
        status="publish",
    )

    result = asyncio.run(handle_build_internal_links(_ctx(pb, registry, job)))

    assert result["linked"] == 0
    assert registry.publisher.updated == []


def test_handler_never_touches_a_page_that_is_not_live():
    pb, project, registry, job = _setup()
    registry.publisher.posts[0] = WPPost(
        id=7,
        title="Old page",
        content_html=f"<p>We compare {PHRASE} tools.</p>",
        link="https://site.test/old-page",
        status="draft",
    )

    result = asyncio.run(handle_build_internal_links(_ctx(pb, registry, job)))

    assert result["linked"] == 0
    assert registry.publisher.updated == []


def test_handler_does_nothing_for_an_unpublished_article():
    pb, project, registry, job = _setup()
    ArticleRepo(pb).update(job["payload"]["articleId"], {"status": "review"})

    result = asyncio.run(handle_build_internal_links(_ctx(pb, registry, job)))

    assert result == {"linked": 0, "skipped": "not published"}
    assert registry.publisher.updated == []


def test_link_phrases_fall_back_to_shorter_runs_of_a_long_keyword():
    from app.services.link_building import _link_phrases

    phrases = _link_phrases(
        "how to automate content creation with ai",
        "How to Automate Content Creation With AI: A Workflow",
    )
    assert phrases[0] == "how to automate content creation with ai"
    assert "automate content creation" in phrases

    html = "<p>Teams can automate content creation without a newsroom.</p>"
    out = insert_contextual_link(html, TARGET, phrases)
    assert out is not None
    assert f'<a href="{TARGET}">automate content creation</a>' in out
