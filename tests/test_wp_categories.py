"""Every published article carries a WordPress category.

The site's taxonomy is read, reused, and extended — and a taxonomy problem can
never fail a publish.
"""

from __future__ import annotations

import asyncio

from app.domain.publishing_category import (
    best_token_match,
    clean_category_name,
    match_by_name,
)
from app.repositories.jobs import JobRepo
from app.services.wp_categories import choose_category_name, resolve_category
from tests.fake_providers import FakePublisher
from tests.helpers import make_article, make_pb, make_project, make_topic


class _Ctx:
    """Minimal JobContext for the pure-ish category helpers."""

    def __init__(self, config, registry, warnings=None):
        self.config = config
        self.registry = registry
        self.warnings = warnings if warnings is not None else []

    def warning(self, message, extra=None):
        self.warnings.append((message, extra or {}))

    def info(self, *_args, **_kwargs):
        pass


class _Registry:
    def __init__(self, publisher):
        self.publisher = publisher

    def get_publisher_provider(self, *_args, **_kwargs):
        return self.publisher


def _config(pb, project_id):
    from app.services.settings import ProjectConfig

    return ProjectConfig.load(pb, project_id)


# ---------------------------------------------------------------------------
# Pure matching
# ---------------------------------------------------------------------------
def test_name_matching_is_case_and_space_insensitive():
    cats = [{"id": 7, "name": "Gym Management", "slug": "gym-management"}]
    assert match_by_name("  gym   MANAGEMENT ", cats)["id"] == 7
    assert match_by_name("gym-management", cats)["id"] == 7
    assert match_by_name("nope", cats) is None


def test_token_match_reuses_the_sites_own_category():
    cats = [
        {"id": 1, "name": "Uncategorized"},
        {"id": 8, "name": "Gym Management"},
        {"id": 9, "name": "Nutrition"},
    ]
    assert best_token_match("gym management software pricing", cats)["id"] == 8
    assert best_token_match("protein for beginners", cats) is None


def test_new_names_are_short_single_line_and_never_junk():
    assert clean_category_name("  'Gym Operations.' ") == "Gym Operations"
    assert clean_category_name("Gym Management Software For Small Studio Owners") == ""
    assert clean_category_name("Uncategorized") == ""
    assert clean_category_name("") == ""


# ---------------------------------------------------------------------------
# Choosing (assembly)
# ---------------------------------------------------------------------------
def test_pinned_category_wins_without_asking_anyone():
    pb = make_pb()
    project = make_project(pb)
    config = _config(pb, project["id"])
    config.settings["defaultCategory"] = "Gym Operations"
    ctx = _Ctx(config, _Registry(FakePublisher()))

    name = asyncio.run(choose_category_name(ctx, topic=None, title="T", keyword="k", summary="s"))

    assert name == "Gym Operations"


def test_existing_category_is_reused_by_the_model_pick():
    pb = make_pb()
    project = make_project(pb)
    publisher = FakePublisher()
    publisher.categories = [
        {"id": 1, "name": "Uncategorized"},
        {"id": 4, "name": "Gym Operations"},
    ]
    config = _config(pb, project["id"])
    ctx = _Ctx(config, _Registry(publisher))
    ctx.providers = type(
        "P", (), {"llm_for": lambda _self, _role: _LLM('{"category": "gym operations"}')}
    )()

    name = asyncio.run(
        choose_category_name(
            ctx, topic=None, title="Gym pricing", keyword="gym pricing", summary="s"
        )
    )

    # canonical spelling comes from the site, not the model
    assert name == "Gym Operations"


def test_without_a_model_the_site_industry_groups_the_article():
    pb = make_pb()
    project = make_project(pb)
    config = _config(pb, project["id"])
    config.settings["productProfile"] = {"profile": {"industry": "fitness management"}}
    publisher = FakePublisher()  # only "Uncategorized" exists
    ctx = _Ctx(config, _Registry(publisher))

    name = asyncio.run(choose_category_name(ctx, topic=None, title="T", keyword="k", summary="s"))

    assert name == "Fitness Management"  # machine-derived names are title-cased


# ---------------------------------------------------------------------------
# Resolving (publishing)
# ---------------------------------------------------------------------------
def test_resolve_creates_the_missing_category_once():
    pb = make_pb()
    project = make_project(pb)
    publisher = FakePublisher()
    ctx = _Ctx(_config(pb, project["id"]), _Registry(publisher))
    article = {"wordpressCategory": "Gym Operations"}

    first = asyncio.run(resolve_category(ctx, article=article, publisher=publisher))
    second = asyncio.run(resolve_category(ctx, article=article, publisher=publisher))

    assert first[0] and first[1] == "Gym Operations"
    assert second[0] == first[0]  # matched by name, not created twice
    assert publisher.created_categories == ["Gym Operations"]


def test_an_article_without_a_category_gets_one_at_publish():
    """Articles assembled before this feature must not be published orphaned."""
    pb = make_pb()
    project = make_project(pb)
    topic = make_topic(pb, project["id"], status="review", keyword="gym pricing")
    article = make_article(pb, project["id"], status="review", topic_id=topic["id"])
    config = _config(pb, project["id"])
    config.settings["defaultCategory"] = "Gym Operations"
    publisher = FakePublisher()
    ctx = _Ctx(config, _Registry(publisher))

    category_id, name = asyncio.run(resolve_category(ctx, article=article, publisher=publisher))

    assert name == "Gym Operations"
    assert category_id == publisher.categories[-1]["id"]  # created on the site


def test_resolve_never_raises_when_the_taxonomy_is_broken():
    pb = make_pb()
    project = make_project(pb)
    publisher = FakePublisher()
    publisher.category_error = RuntimeError("wp down")
    ctx = _Ctx(_config(pb, project["id"]), _Registry(publisher))

    category_id, name = asyncio.run(
        resolve_category(ctx, article={"wordpressCategory": "Gym Operations"}, publisher=publisher)
    )

    assert category_id == 0
    assert name == "Gym Operations"
    assert ctx.warnings  # surfaced, not silent


# ---------------------------------------------------------------------------
# The whole publish path
# ---------------------------------------------------------------------------
def test_publish_sends_the_category_and_stores_it():
    from app.services.publishing_service import handle_publish_article
    from tests.fake_providers import FakeRegistry
    from tests.test_wordpress_publishing import make_article as make_approved_article
    from tests.test_wordpress_publishing import make_ctx

    pb = make_pb()
    project = make_project(pb)
    article = make_approved_article(pb, project["id"])
    registry = FakeRegistry()
    registry.publisher.categories = [{"id": 3, "name": "Gym Operations"}]
    pb.collection("articles").update(article["id"], {"wordpressCategory": "Gym Operations"})
    job = JobRepo(pb).create(
        project=project["id"],
        type="publish_article",
        payload={"articleId": article["id"], "action": "publish"},
        idempotency_key="pub:cat:1",
        entity_type="article",
        entity_id=article["id"],
    )

    asyncio.run(handle_publish_article(make_ctx(pb, registry, job)))

    created = registry.publisher.created[-1]
    assert created["categories"] == [3]
    stored = pb.collection("articles").get_one(article["id"])
    assert stored["wordpressCategoryId"] == 3
    assert stored["wordpressCategory"] == "Gym Operations"


def test_the_category_prompts_render_with_the_site_categories():
    """Prompt templates are DATA: an unknown variable would render empty."""
    from app.domain.prompt_render import render_prompt, validate_prompt
    from app.scripts.bootstrap_pb import DEFAULT_PROMPTS

    for ptype in ("category_system", "category_user"):
        assert validate_prompt(DEFAULT_PROMPTS[ptype]) == []

    rendered = render_prompt(
        DEFAULT_PROMPTS["category_user"],
        {
            "article.title": "Gym pricing",
            "topic.keyword": "gym pricing",
            "article.meta_description": "What gyms pay.",
            "categories": '["Gym Operations"]',
        },
    )
    assert "Gym Operations" in rendered and "gym pricing" in rendered.lower()


class _LLM:
    def __init__(self, reply: str) -> None:
        self.reply = reply

    async def generate(self, **_kwargs):
        class _R:
            text = self.reply

        return _R()
