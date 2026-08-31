"""Image planning job ("plan_article_images").

LLM-driven planning of which images an article needs: exactly one cover plus
at most `imageMaxInteriorImages` purposeful interior images (density driven by
word count / section count; decorative or repetitive images are forbidden by
the seeded prompt and the plan validator).

The visual prompt language is decoupled from the article locale: alt/caption
come back in the article language, visual prompts in `imagePromptLanguage`.
"""

from __future__ import annotations

import time
from typing import Any

from app.domain.images import (
    ArticleImagePlan,
    build_negative_prompt,
    build_visual_prompt,
    dims_for_aspect,
    style_fingerprint_source,
    validate_plan,
)
from app.domain.parsing import extract_json
from app.jobs.context import JobContext
from app.jobs.handlers import register_job
from app.providers.base import GenerationParams, ProviderError
from app.repositories.articles import ArticleRepo, SectionRepo
from app.repositories.topics import TopicRepo

# recommended interior count by article word count (deterministic density cap
# passed into the prompt; the plan validator enforces the hard config cap)
_DENSITY = ((400, 0), (900, 1), (1800, 2), (3200, 3))


def recommended_interiors(word_count: int, config_cap: int) -> int:
    for threshold, count in _DENSITY:
        if word_count < threshold:
            return min(count, config_cap)
    return min(4, config_cap)


@register_job("plan_article_images")
async def handle_plan_article_images(ctx: JobContext) -> dict[str, Any]:
    from app.services.prompt_service import PromptService

    article_id = (ctx.payload().get("articleId") or "").strip()
    if not article_id:
        raise ValueError("plan_article_images payload is missing articleId")

    articles = ArticleRepo(ctx.pb)
    article = articles.get(article_id)
    if not article:
        raise ValueError(f"article not found: {article_id}")
    if not (article.get("finalHtml") or article.get("generatedContent")):
        raise ProviderError(
            "مقاله هنوز محتوایی ندارد — ابتدا نوشتن مقاله را کامل کنید",
            retryable=False,
        )

    topic = TopicRepo(ctx.pb).get(article.get("topicId") or "") or {}
    sections = SectionRepo(ctx.pb).list_for_article(article_id)
    imgs = ctx.config.images

    words = int(article.get("wordCount") or 0)
    cap = recommended_interiors(words, imgs["max_interior_images"])

    context = {
        "article.title": article.get("title") or "",
        "article.slug": article.get("slug") or "",
        "topic.keyword": topic.get("keyword") or "",
        "language": ctx.config.language,
        "prompt_language": imgs["prompt_language"],
        "sections": "\n".join(
            f"- section-{i}: {s.get('heading') or ''}" for i, s in enumerate(sections)
        ),
        "style_profile": imgs["style"] or "—",
        "max_interior_images": cap,
    }

    service = PromptService(ctx.pb, ctx.registry)
    user_tpl = ctx.config.prompt("image_plan_user")
    if not user_tpl:
        raise ProviderError(
            "پرامپت image_plan_user تنظیم نشده است — بوت‌استرپ را اجرا کنید",
            retryable=False,
        )
    user = service.render(user_tpl, context)
    system_raw = ctx.config.prompt("image_plan_system")
    system = service.render(system_raw, context) if system_raw else None

    role_cfg = ctx.config.role_llm("outline")
    params = GenerationParams(
        temperature=float(role_cfg.get("temperature") or 0.6),
        max_tokens=int(role_cfg.get("max_tokens") or 3000),
        timeout=float(role_cfg.get("timeout") or 120.0),
    )

    ctx.stage_started("planning", "در حال طراحی برنامه تصاویر…")
    started = time.monotonic()
    llm = ctx.providers.llm_for("outline")
    raw = await llm.generate(system=system, user=user, params=params)

    previous = int(article.get("imagePlanVersion") or 0)
    version = previous + 1
    try:
        plan = ArticleImagePlan.parse_llm(
            extract_json(raw.text), version=version, style=imgs["style"] or {}
        )
        validate_plan(plan, max_interior=imgs["max_interior_images"])
    except ValueError as exc:
        raise ProviderError(f"برنامه تصاویر نامعتبر بود: {exc}", retryable=False) from exc

    # resolved dimensions per image (persisted into the plan snapshot)
    for spec in plan.images:
        min_width = imgs["cover_min_width"] if spec.role == "cover" else 0
        aspect = spec.aspect_ratio or (
            imgs["cover_aspect_ratio"] if spec.role == "cover" else imgs["interior_aspect_ratio"]
        )
        spec.aspect_ratio = aspect
        spec.suggested_size = list(dims_for_aspect(aspect, role=spec.role, min_width=min_width))
        spec.negative_prompt = build_negative_prompt(imgs["style"], spec.negative_prompt)
        spec.prompt = build_visual_prompt(spec.prompt, imgs["style"])

    articles.set_image_plan(article_id, plan.to_dict(), version)
    elapsed = int((time.monotonic() - started) * 1000)
    ctx.stage_completed(
        "planning",
        "برنامه تصاویر آماده شد",
        {"cover": 1, "interiors": len(plan.interiors()), "version": version},
    )
    ctx.event(
        "image_plan_created",
        "برنامه تصاویر ساخته شد",
        {
            "version": version,
            "interiors": len(plan.interiors()),
            "latencyMs": elapsed,
            "llmRetries": raw.retries,
        },
    )
    return {
        "articleId": article_id,
        "version": version,
        "cover": 1,
        "interiors": len(plan.interiors()),
        "styleHashSource": style_fingerprint_source(imgs["style"]),
    }
