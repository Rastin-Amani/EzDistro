"""WordPress categories for generated articles.

The site owns the taxonomy; this module only reads it, reuses it, and extends it
when nothing fits. Two moments:

* **choose** (assembly) — decide a category *name*: the project's pinned
  default, else the model's pick from the site's real category list (or a short
  new name), else a deterministic word match, else the site's own industry.
* **resolve** (publishing) — turn that name into a term id: match the live list
  (case-insensitive), create the category when it is missing, and never let a
  taxonomy problem fail a publish.

Every failure here degrades to "publish without our own category": a broken
taxonomy must not cost an article, and it must not cost a category either when
the site has any sane default.
"""

from __future__ import annotations

import json
from typing import Any

import structlog

from app.domain.publishing_category import (
    best_token_match,
    clean_category_name,
    match_by_name,
)

logger = structlog.get_logger("wp.categories")


async def choose_category_name(
    ctx: Any,
    *,
    topic: dict[str, Any] | None,
    title: str,
    keyword: str,
    summary: str,
    categories: list[dict[str, Any]] | None = None,
) -> str:
    """The category name to file this article under ("" when undecidable)."""
    pinned = clean_category_name(ctx.config.settings.get("defaultCategory") or "")
    if pinned:
        return pinned

    if categories is None:
        categories = await _site_categories(ctx)
    by_model = await _model_choice(
        ctx, topic=topic, title=title, keyword=keyword, summary=summary, categories=categories
    )
    if by_model:
        return by_model

    # Deterministic: the site already has a category this article belongs to.
    match = best_token_match(f"{keyword} {title}", categories)
    if match:
        return str(match.get("name") or "")

    # Never leave an article without a group: the site's own subject is the
    # broadest true category a generated article can claim.
    profile = ctx.config.settings.get("productProfile") or {}
    industry = ""
    if isinstance(profile, dict):
        industry = str((profile.get("profile") or {}).get("industry") or "")
    return clean_category_name(_titleish(industry)) or clean_category_name(
        ctx.config.settings.get("brandName") or ""
    )


def _titleish(value: str) -> str:
    """Category-name casing for a machine-derived name ("fitness management")."""
    text = " ".join(str(value or "").split())
    return text.title() if text and text == text.lower() else text


async def resolve_category(ctx: Any, *, article: dict[str, Any], publisher: Any) -> tuple[int, str]:
    """(term id, name) for this article's category — (0, "") when unavailable."""
    name = clean_category_name(article.get("wordpressCategory") or "") or clean_category_name(
        ctx.config.settings.get("defaultCategory") or ""
    )

    try:
        categories = await publisher.list_categories()
    except Exception as exc:  # taxonomy read must never fail a publish
        ctx.warning(
            "wordpress categories unavailable",
            {"error": f"{type(exc).__name__}: {exc}"[:300]},
        )
        categories = []

    if not name:
        # Articles assembled before this feature (or under an old build) carry
        # no category: choose one now rather than publish an orphaned post.
        name = await choose_category_name(
            ctx,
            topic=_topic_of(ctx, article),
            title=str(article.get("title") or ""),
            keyword=str(article.get("slug") or ""),
            summary=str(article.get("metaDescription") or ""),
            categories=categories,
        )
    if not name:
        return int(str(article.get("wordpressCategoryId") or 0)), ""

    existing = match_by_name(name, categories)
    if existing is not None and existing.get("id"):
        return int(str(existing["id"])), str(existing.get("name") or name)

    # "Add them if needed": the article's group does not exist on the site yet.
    try:
        created = await publisher.create_category(name)
    except Exception as exc:
        ctx.warning(
            "wordpress category could not be created",
            {"name": name, "error": f"{type(exc).__name__}: {exc}"[:300]},
        )
        return 0, name
    if created and created.get("id"):
        ctx.info("wordpress category created", {"name": name, "id": created["id"]})
        return int(str(created["id"])), str(created.get("name") or name)
    return 0, name


def _topic_of(ctx: Any, article: dict[str, Any]) -> dict[str, Any] | None:
    topic_id = str(article.get("topicId") or "")
    if not topic_id:
        return None
    try:
        from app.repositories.topics import TopicRepo

        return TopicRepo(ctx.pb).get(topic_id)
    except Exception:
        return None


async def _site_categories(ctx: Any) -> list[dict[str, Any]]:
    try:
        publisher = ctx.registry.get_publisher_provider(ctx.config.project, ctx.config.settings)
        rows = await publisher.list_categories()
    except Exception as exc:
        ctx.warning(
            "wordpress categories unavailable",
            {"error": f"{type(exc).__name__}: {exc}"[:300]},
        )
        return []
    return [row for row in rows if isinstance(row, dict) and row.get("id")]


async def _model_choice(
    ctx: Any,
    *,
    topic: dict[str, Any] | None,
    title: str,
    keyword: str,
    summary: str,
    categories: list[dict[str, Any]],
) -> str:
    """Ask the model for the best category among the site's own names."""
    user_tpl = ctx.config.prompt("category_user")
    if not user_tpl:
        return ""
    try:
        from app.domain.parsing import extract_json
        from app.providers.base import GenerationParams
        from app.services.prompt_service import PromptService

        names = [str(row.get("name") or "") for row in categories]
        service = PromptService(ctx.pb)
        context = service.build_context(
            ctx.config,
            topic=topic,
            article={"title": title, "metaDescription": summary},
            extra={"categories": json.dumps(names, ensure_ascii=False)},
        )
        system_tpl = ctx.config.prompt("category_system")
        system = service.render(system_tpl, context) if system_tpl else None
        user = service.render(user_tpl, context)
        if not user.strip():
            # An unresolved template renders empty; asking a model nothing gets
            # an answer about nothing ("what would you like help with?").
            ctx.warning("wordpress category prompt rendered empty", {})
            return ""
        role = ctx.config.role_llm("meta")
        params = GenerationParams(
            temperature=0.2,
            max_tokens=int(role.get("max_tokens") or 4096),
            timeout=float(role.get("timeout") or 120.0),
        )
        raw = await ctx.providers.llm_for("meta").generate(system=system, user=user, params=params)
        data = extract_json(raw.text)
        proposed = clean_category_name(str((data or {}).get("category") or ""))
        if not proposed:
            return ""
        existing = match_by_name(proposed, categories)
        return str(existing.get("name") or "") if existing else proposed
    except Exception as exc:
        # A taxonomy guess is never worth an article.
        ctx.warning(
            "wordpress category selection skipped",
            {"error": f"{type(exc).__name__}: {exc}"[:300]},
        )
        return ""
