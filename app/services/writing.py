"""Article generation engine — deterministic workflow around the LLM.

Pipeline (docs/ARCHITECTURE.md §10):

1. load topic           6. generate outline         11. validate section output
2. load project config  7. validate strict schema   12. persist each section immediately
3. load prompt versions 8. save immutable outline   13. retry failed sections independently
4. retrieve knowledge   9. create section records   14. assemble final article
5. build context       10. generate sections        15. run validation → review state

Each section is an INDEPENDENT job (`generate_section`) — a failure in section
8 leaves 1-7 completed and is retried on its own. The assembler job waits for
all sections (deterministic retry-after polling) and fails explicitly if any
section failed. The engine is provider-agnostic (all providers via registry).
"""

from __future__ import annotations

import contextlib
import time
from typing import Any

from app.domain.article_html import (
    normalize_article_html,
    normalize_outline,
)
from app.domain.article_validation import ArticleValidator, SectionValidator
from app.domain.parsing import extract_json
from app.domain.sanitize import sanitize_html
from app.domain.seo_score import seo_score
from app.jobs.context import JobCancelled, JobContext
from app.jobs.handlers import register_job
from app.providers.base import GenerationParams, ProviderError, TransientError
from app.repositories.articles import ArticleRepo, SectionRepo
from app.repositories.jobs import JobRepo
from app.repositories.prompts import PromptRepo
from app.repositories.topics import TopicRepo
from app.schemas.llm import SectionContent


# ---------------------------------------------------------------------------
# write_article — orchestrator
# ---------------------------------------------------------------------------
@register_job("write_article")
async def handle_write_article(ctx: JobContext) -> dict[str, Any]:
    payload = ctx.payload()
    topic_id = payload.get("topicId") or ""
    if not topic_id:
        raise ValueError("write_article payload is missing topicId")

    topics = TopicRepo(ctx.pb)
    articles = ArticleRepo(ctx.pb)
    sections = SectionRepo(ctx.pb)

    topic = topics.get(topic_id)
    if not topic:
        raise ValueError(f"topic not found: {topic_id}")
    regenerating = bool(payload.get("regenerate"))
    # An explicit regeneration may start from any post-write state (the article
    # already exists and its content is being replaced). Everything else that is
    # already inside the write pipeline is skipped to avoid duplicate writes.
    in_write_pipeline = topic.get("status") in (
        "queued",
        "planned",
        "failed",
        "cancelled",
        "outline_ready",
    )
    post_write_states = ("review", "approved", "sent_back", "published", "completed")
    if not in_write_pipeline and not (regenerating and topic.get("status") in post_write_states):
        if topic.get("status") == "planning":
            # Crash-recovery: a previous attempt died mid-outline and left the
            # topic in "planning" (nobody else can be writing it — this job
            # holds the lease). Reset and resume; outline generation is
            # idempotent (resumes the saved outline if one exists).
            ctx.info(
                "resuming topic left in 'planning' by a crashed attempt",
                {"topic": topic_id},
            )
            topics.set_status(topic_id, "planned")
        else:
            ctx.info(
                "topic already in the write pipeline — skipping",
                {"topic": topic_id, "status": topic.get("status")},
            )
            existing = articles.by_topic(topic_id)
            if existing:
                return {"articleId": existing["id"], "alreadyWritten": True}
            raise ValueError(f"topic {topic_id} is in unexpected state: {topic.get('status')}")

    try:
        return await _write(ctx, topics, articles, sections, topic, topic_id)
    except JobCancelled:
        raise
    except Exception:
        # Deterministic failure state: topic (and article if created) → failed.
        with contextlib.suppress(Exception):
            topics.set_status(topic_id, "failed")
        article = articles.by_topic(topic_id)
        if article and article.get("status") not in ("published", "review"):
            with contextlib.suppress(Exception):
                articles.set_status(article["id"], "failed")
        raise


async def _write(
    ctx: JobContext,
    topics: TopicRepo,
    articles: ArticleRepo,
    sections: SectionRepo,
    topic: dict[str, Any],
    topic_id: str,
) -> dict[str, Any]:
    article = articles.by_topic(topic_id)
    outline_version = 0
    payload = ctx.payload()
    regenerate = bool(payload.get("regenerate"))

    # NEVER destroy the previous version: checkpoint before regenerating.
    if regenerate and article and article.get("finalHtml"):
        from app.services.revisions import RevisionService

        RevisionService(ctx.pb).snapshot(
            article, "checkpoint", note="before regeneration", created_by="worker"
        )
        ctx.info("checkpoint saved before regeneration", {"article": article["id"]})

    # 6-8. outline (or resume an already-saved immutable outline — except on regenerate)
    if (
        not regenerate
        and article
        and int(article.get("outlineVersion") or 0) > 0
        and article.get("outline")
    ):
        outline = article["outline"]
        outline_version = int(article["outlineVersion"])
        ctx.info(
            "resuming with saved outline", {"article": article["id"], "version": outline_version}
        )
    else:
        ctx.stage_started("outline", "در حال تولید رئوس مطالب")
        ctx.progress(5, stage="outline", message="در حال تولید رئوس مطالب…")
        topics.set_status(topic_id, "planning")
        outline = await _generate_outline(ctx, topic)
        if article is None:
            article = articles.create(
                project=ctx.project_id,
                topic_id=topic_id,
                title=outline["title"],
                slug=outline["slug"],
            )
        outline_version = int(article.get("outlineVersion") or 0) + 1
        articles.set_outline_ready(
            article["id"], outline_version, outline, outline["title"], outline["slug"]
        )
        topics.link_article(topic_id, article["id"])
        topics.set_status(topic_id, "outline_ready")
        ctx.stage_completed("outline", "رئوس مطالب آماده شد")

    article_id = article["id"]

    # 9. create/refresh section records from the immutable outline
    _persist_sections(ctx, sections, article_id, outline)
    topics.set_status(topic_id, "writing")
    articles.set_status(article_id, "generating")

    # 10-13. ONE independent job per section + one assembler job.
    rows = sections.list_for_article(article_id)
    jobs = JobRepo(ctx.pb)
    section_jobs: list[str] = []
    max_attempts = int(ctx.config.retry_policy["max_attempts"])
    priority = int(topic.get("priority") or 0)
    for row in rows:
        job = jobs.create(
            project=ctx.project_id,
            type="generate_section",
            payload={"sectionId": row["id"]},
            idempotency_key=f"generate:section:{row['id']}",
            max_attempts=max_attempts,
            parent=ctx.job_id,
            entity_type="section",
            entity_id=row["id"],
            priority=priority,
        )
        section_jobs.append(job["id"])

    assemble = jobs.create(
        project=ctx.project_id,
        type="assemble_article",
        payload={
            "articleId": article_id,
            "sectionIds": [r["id"] for r in rows],
            # carry the write trigger + auto-publish attempt counter through
            # the chain so assembly can decide publish-vs-rewrite on its own
            "trigger": payload.get("trigger") or "",
            "autoAttempts": int(payload.get("autoAttempts") or 1),
        },
        idempotency_key=f"assemble:article:{article_id}",
        max_attempts=max(10, 3 * len(rows) + 30),  # generous: waits for sections
        parent=ctx.job_id,
        entity_type="article",
        entity_id=article_id,
    )

    ctx.info(
        "section jobs queued",
        {
            "article": article_id,
            "sections": len(rows),
            "sectionJobs": len(section_jobs),
            "assembleJob": assemble["id"],
        },
    )
    ctx.stage_completed("outline", "تولید مقاله آغاز شد")
    ctx.progress(100, stage="done", message="وظایف بخش‌ها ساخته شدند")
    return {
        "articleId": article_id,
        "outlineVersion": outline_version,
        "sectionJobs": section_jobs,
        "assembleJobId": assemble["id"],
    }


# ---------------------------------------------------------------------------
# generate_outline — granular outline-only job
# ---------------------------------------------------------------------------
@register_job("generate_outline")
async def handle_generate_outline(ctx: JobContext) -> dict[str, Any]:
    topic_id = ctx.payload().get("topicId") or ""
    if not topic_id:
        raise ValueError("generate_outline payload is missing topicId")
    topics = TopicRepo(ctx.pb)
    articles = ArticleRepo(ctx.pb)
    sections = SectionRepo(ctx.pb)
    topic = topics.get(topic_id)
    if not topic:
        raise ValueError(f"topic not found: {topic_id}")

    topics.set_status(topic_id, "planning")
    outline = await _generate_outline(ctx, topic)
    article = articles.by_topic(topic_id)
    if article is None:
        article = articles.create(
            project=ctx.project_id, topic_id=topic_id, title=outline["title"], slug=outline["slug"]
        )
    version = int(article.get("outlineVersion") or 0) + 1
    articles.set_outline_ready(article["id"], version, outline, outline["title"], outline["slug"])
    topics.link_article(topic_id, article["id"])
    _persist_sections(ctx, sections, article["id"], outline)
    topics.set_status(topic_id, "outline_ready")
    ctx.progress(100, stage="done", message="رئوس مطالب آماده شد")
    return {"topicId": topic_id, "articleId": article["id"], "outlineVersion": version}


# ---------------------------------------------------------------------------
# generate_section — one independent job per section
# ---------------------------------------------------------------------------
@register_job("generate_section")
async def handle_generate_section(ctx: JobContext) -> dict[str, Any]:
    section_id = ctx.payload().get("sectionId") or ""
    if not section_id:
        raise ValueError("generate_section payload is missing sectionId")

    sections = SectionRepo(ctx.pb)
    articles = ArticleRepo(ctx.pb)
    section = sections.get(section_id)
    if not section:
        raise ValueError(f"section not found: {section_id}")
    article = articles.get(section.get("article") or "")
    if not article:
        raise ValueError("section has no article")

    outline = article.get("outline") or {}
    position = int(section.get("position") or 0)
    plan = _section_plan_at(outline, position, section)

    ctx.progress(
        10, stage="generating_section", message=f"تولید بخش: {section.get('heading') or ''}"
    )
    sections.mark_generating(section_id)

    result = await _generate_section(ctx, article, outline, plan)
    html = sanitize_html(normalize_article_html(result["html"]))

    # 11. validate section output — never silently accept garbage
    issues = SectionValidator().validate(html)
    if issues:
        error = {
            "type": "SectionValidationError",
            "message": "; ".join(i.message for i in issues),
            "issues": [i.to_dict() for i in issues],
        }
        sections.mark_failed(section_id, error)
        raise ProviderError("section failed validation", retryable=False, details=error)

    # 12. persist immediately
    sections.mark_done(
        section_id,
        html,
        provider=result["provider"],
        model=result["model"],
        token_usage=result["token_usage"],
        latency_ms=result["latency_ms"],
        prompt_version=result["prompt_version"],
    )
    # A per-section regeneration (workspace editor) leaves the article in
    # "generating"; with no assembler waiting, restore it to "review" so it
    # never gets stuck. During the normal pipeline the assemble job exists and
    # keeps the "generating" state until assembly completes.
    if article.get("status") == "generating":
        waiting = JobRepo(ctx.pb).first(
            filter=f'type="assemble_article" && payload.articleId="{article.get("id")}" && (status="pending" || status="retrying" || status="running")'
        )
        if not waiting:
            articles.set_status(article.get("id"), "review")
    ctx.progress(100, stage="done", message="بخش تولید شد")
    ctx.info("section generated", {"section": section_id, "words": result.get("words", 0)})
    return {"sectionId": section_id, "promptVersion": result["prompt_version"]}


# ---------------------------------------------------------------------------
# assemble_article — waits for sections, assembles, validates, → review
# ---------------------------------------------------------------------------
@register_job("assemble_article")
async def handle_assemble_article(ctx: JobContext) -> dict[str, Any]:
    article_id = ctx.payload().get("articleId") or ""
    if not article_id:
        raise ValueError("assemble_article payload is missing articleId")

    articles = ArticleRepo(ctx.pb)
    sections = SectionRepo(ctx.pb)
    article = articles.get(article_id)
    if not article:
        raise ValueError(f"article not found: {article_id}")

    rows = sections.list_for_article(article_id)
    if not rows:
        raise ValueError("article has no sections")

    # deterministic wait: not ready → transient retry (engine honors retry-after)
    pending = [r for r in rows if r.get("status") in ("pending", "generating")]
    failed = [r for r in rows if r.get("status") == "failed"]
    if pending:
        raise TransientError(
            f"waiting for {len(pending)} sections to finish",
            {"retry_after_seconds": 20, "pending": [r["id"] for r in pending]},
        )
    if failed:
        failed_ids = [r["id"] for r in failed]
        articles.set_status(article_id, "failed")
        raise ProviderError(
            f"cannot assemble: {len(failed)} section(s) failed: {', '.join(failed_ids)}",
            retryable=False,
            details={"failed_sections": failed_ids},
        )

    ctx.stage_started("assembling", "در حال ساخت مقاله نهایی")
    ctx.progress(40, stage="assembling", message="در حال ساخت مقاله نهایی…")

    outline = article.get("outline") or {}
    ordered = [r for r in rows if r.get("status") == "done"]
    ordered.sort(key=lambda r: int(r.get("position") or 0))

    # 14. deterministic assembly with SEO enforcement (guarantees the score
    # floor: keyword in title/H1, URL slug, first paragraph, and an H2 heading).
    from app.domain.chunker import word_count_from_html
    from app.domain.seo_enforce import enforce_article_html

    topic = TopicRepo(ctx.pb).get(article.get("topicId") or "")
    keyword = str(topic.get("keyword") or "") if topic else ""
    enforced = enforce_article_html(
        title=article.get("title") or "",
        slug=article.get("slug") or "",
        sections=[
            {"heading": r.get("heading") or "", "content": r.get("content") or ""} for r in ordered
        ],
        internal_links=_collect_links(outline),
        keyword=keyword,
        # article_sections content was sanitized when each section completed
        sanitize=False,
    )
    html = enforced["html"]
    fixed_title = enforced["title"]
    fixed_slug = enforced["slug"]
    if fixed_title != article.get("title") or fixed_slug != article.get("slug"):
        articles.update(article_id, {"title": fixed_title, "slug": fixed_slug})

    meta_description = article.get("metaDescription") or ""
    if not meta_description.strip():
        meta_description = fixed_title

    # 15. whole-article validation
    min_words = int(ctx.config.generation.get("min_article_words") or 300)
    report = ArticleValidator(min_words=min_words, keyword=keyword).validate(
        title=fixed_title,
        slug=fixed_slug,
        outline=outline,
        sections=ordered,
        html=html,
    )
    articles.set_validation(article_id, report.to_dict())
    if not report.ok:
        articles.set_status(article_id, "failed")
        raise ProviderError(
            "article validation failed",
            retryable=False,
            details={"issues": [i.to_dict() for i in report.issues], "stats": report.stats},
        )

    score = seo_score(
        title=fixed_title,
        meta_description=meta_description,
        html=html,
        keyword=keyword,
    )
    if score < 90:
        ctx.warning(
            "seo score below acceptable floor even after enforcement",
            {"score": score, "article": article_id},
        )
    articles.set_final_content(
        article_id, html, word_count_from_html(html), score, meta_description
    )
    # Track the generated version: snapshot + record (previous versions in history
    # are never destroyed — the pre-assemble state was snapshotted by the writer).
    from app.services.revisions import RevisionService

    revision = RevisionService(ctx.pb).snapshot(
        articles.get(article_id) or article,
        "generated",
        note="assembled article",
        created_by="worker",
    )
    articles.record_generated(article_id, html, int(revision.get("revision") or 0))
    if topic:
        TopicRepo(ctx.pb).set_status(topic["id"], "review")

    # Images are part of the article pipeline: plan (and then generate) them
    # automatically as soon as content exists. The plan job is cheap (one LLM
    # call); generation jobs are chained from the plan itself. Gated on the
    # image-plan prompt actually being resolvable — projects without image
    # setup (prompt seeded) behave exactly as before.
    if PromptRepo(ctx.pb).resolve(ctx.project_id, "image_plan_user"):
        JobRepo(ctx.pb).create(
            project=ctx.project_id,
            type="plan_article_images",
            payload={"articleId": article_id},
            idempotency_key=f"image:plan:{article_id}:auto:{int(time.time())}",
            max_attempts=3,
            entity_type="article",
            entity_id=article_id,
        )

    # Auto-publish: schedule-generated articles skip review entirely. When the
    # SEO score reaches the threshold → publish straight to WordPress; when it
    # doesn't → rewrite (regenerate) and try again, up to max_attempts.
    auto = ctx.config.auto_publish
    if auto["enabled"] and str(ctx.payload().get("trigger") or "") == "schedule":
        if score >= int(auto["min_score"]):
            articles.set_status(article_id, "approved")
            JobRepo(ctx.pb).create(
                project=ctx.project_id,
                type="publish_article",
                payload={"articleId": article_id, "action": "publish"},
                idempotency_key=f"publish:article:{article_id}:auto:{int(time.time())}",
                max_attempts=3,
                entity_type="article",
                entity_id=article_id,
            )
            ctx.info(
                "auto-publish triggered (score OK)",
                {"score": score, "min_score": auto["min_score"], "article": article_id},
            )
        else:
            attempts = int(ctx.payload().get("autoAttempts") or 1)
            if attempts < int(auto["max_attempts"]):
                topic_id = article.get("topicId") or ""
                JobRepo(ctx.pb).create(
                    project=ctx.project_id,
                    type="write_article",
                    payload={
                        "topicId": topic_id,
                        "regenerate": True,
                        "trigger": "schedule",
                        "autoAttempts": attempts + 1,
                    },
                    idempotency_key=(
                        f"write:article:{topic_id}:auto:{attempts}:{int(time.time())}"
                    ),
                    max_attempts=3,
                    entity_type="article",
                    entity_id=article_id,
                )
                articles.set_status(article_id, "generating")
                ctx.info(
                    "auto-rewrite scheduled (score too low)",
                    {"score": score, "min_score": auto["min_score"], "attempt": attempts},
                )
            else:
                ctx.info(
                    "auto-publish gave up after max attempts — left for review",
                    {"score": score, "attempts": attempts, "article": article_id},
                )

    ctx.stage_completed("assembling", "مقاله آماده بازبینی است")
    ctx.progress(100, stage="done", message="مقاله آماده بازبینی است")
    return {
        "articleId": article_id,
        "words": word_count_from_html(html),
        "seoScore": score,
        "validationOk": True,
        "generatedRevision": revision.get("revision"),
    }


# ---------------------------------------------------------------------------
# Outline generation (strict schema + repair)
# ---------------------------------------------------------------------------
async def _generate_outline(ctx: JobContext, topic: dict[str, Any]) -> dict[str, Any]:
    from app.domain.prompt_render import PromptRenderError
    from app.services.prompt_service import PromptService

    retrieval = await _retrieval_data(ctx, topic)
    service = PromptService(ctx.pb, ctx.registry)
    context = service.build_context(
        ctx.config,
        topic=topic,
        retrieval_context=retrieval["context"],
        internal_links=retrieval["links"],
    )

    # Prompts are data: render the stored templates with the variable context.
    outline_user = ctx.config.prompt("outline_user")
    if outline_user:
        try:
            user = service.render(outline_user, context)
        except PromptRenderError as exc:
            raise _prompt_config_error("outline_user", exc) from exc
    else:
        user = _OUTLINE_TASK_FALLBACK
        if retrieval["context"]:
            user += "\n\n## زمینه بازیابی (فقط نتایج مرتبط)\n" + retrieval["context"]

    system_raw = ctx.config.prompt("outline_system") or ctx.config.prompt("brand_voice")
    system = None
    if system_raw:
        try:
            system = service.render(system_raw, context)
        except PromptRenderError as exc:
            raise _prompt_config_error("outline_system", exc) from exc

    params = _generation_params(ctx, "outline")
    raw = await ctx.providers.llm.generate(system=system, user=user, params=params)
    try:
        outline = normalize_outline(extract_json(raw.text))
    except ValueError as first_error:
        # safe repair/retry: one validation-prompt pass before giving up
        repaired = await _repair_outline(ctx, raw.text, topic)
        if repaired is None:
            raise first_error
        outline = repaired
    # deterministic keyword enforcement: title/H1 + at least one H2 always
    # carry the keyword (prompts are best-effort; this is the guarantee).
    from app.domain.seo_enforce import enforce_outline

    return enforce_outline(outline, str(topic.get("keyword") or ""))


async def _repair_outline(
    ctx: JobContext, raw: str, topic: dict[str, Any] | None = None
) -> dict[str, Any] | None:
    from app.services.prompt_service import PromptService

    validation = ctx.config.prompt("validation")
    if not validation:
        return None
    try:
        from app.domain.parsing import extract_json as _extract

        service = PromptService(ctx.pb, ctx.registry)
        context = service.build_context(ctx.config, topic=topic, raw_output=raw[:12000])
        prompt = service.render(validation, context)
        params = _generation_params(ctx, "outline")
        repaired_raw = await ctx.providers.llm.generate(system=None, user=prompt, params=params)
        ctx.warning("outline repaired via validation prompt")
        return normalize_outline(_extract(repaired_raw.text))
    except Exception:
        return None


def _persist_sections(
    ctx: JobContext, sections: SectionRepo, article_id: str, outline: dict[str, Any]
) -> None:
    """Create/refresh section records from the outline (rows, not the snapshot)."""
    existing = sections.list_for_article(article_id)
    for i, plan in enumerate(outline.get("sections") or []):
        payload = {
            "heading": plan["heading"],
            "contentBrief": plan.get("content_brief") or "",
            "internalLinks": plan.get("internal_links") or [],
            "status": "pending",
            "error": {},
            "generationAttempts": 0,
            "promptVersion": 0,
            "content": "",
            "provider": "",
            "model": "",
            "tokenUsage": {},
            "generationLatency": 0,
        }
        if i < len(existing):
            sections.update(existing[i]["id"], payload)
        else:
            sections.create(
                article=article_id,
                position=i,
                heading=plan["heading"],
                content_brief=plan.get("content_brief") or "",
                internal_links=plan.get("internal_links") or [],
            )
    for stale in existing[len(outline.get("sections") or []) :]:
        sections.delete(stale["id"])


def _section_plan_at(
    outline: dict[str, Any], position: int, section: dict[str, Any]
) -> dict[str, Any]:
    """The immutable plan for this section (fallback: current row values)."""
    if 0 <= position < len(outline.get("sections") or []):
        plan = outline["sections"][position]
        return {**plan, "position": position}
    return {
        "heading": section.get("heading") or "",
        "content_brief": section.get("contentBrief") or "",
        "internal_links": section.get("internalLinks") or [],
        "position": position,
    }


def _collect_links(outline: dict[str, Any]) -> list[dict[str, str]]:
    seen: set[str] = set()
    links: list[dict[str, str]] = []
    for plan in outline.get("sections") or []:
        for link in plan.get("internal_links") or []:
            url = str(link.get("url") or "").rstrip("/")
            if url and url not in seen:
                seen.add(url)
                links.append(
                    {
                        "title": str(link.get("title") or ""),
                        "url": str(link.get("url") or ""),
                        "anchor_text": str(link.get("anchor_text") or link.get("title") or ""),
                    }
                )
    return links


# ---------------------------------------------------------------------------
# Section generation
# ---------------------------------------------------------------------------
async def _generate_section(
    ctx: JobContext,
    article: dict[str, Any],
    outline: dict[str, Any],
    plan: dict[str, Any],
) -> dict[str, Any]:
    from app.domain.prompt_render import PromptRenderError
    from app.services.prompt_service import PromptService

    service = PromptService(ctx.pb, ctx.registry)
    context = service.build_context(
        ctx.config,
        article=article,
        section=plan,
        internal_links=plan.get("internal_links") or [],
    )

    section_user = ctx.config.prompt("section_user")
    if section_user:
        try:
            user = service.render(section_user, context)
        except PromptRenderError as exc:
            raise _prompt_config_error("section_user", exc) from exc
    else:
        user = _SECTION_TASK_FALLBACK
        links = context["internal_links"]
        if links:
            user += "\n\n## لینک‌های داخلی این بخش (در صورت نیاز استفاده کن)\n" + links

    system_raw = ctx.config.prompt("section_system") or ctx.config.prompt("brand_voice")
    system = None
    if system_raw:
        try:
            system = service.render(system_raw, context)
        except PromptRenderError as exc:
            raise _prompt_config_error("section_system", exc) from exc

    params = _generation_params(ctx, "section")
    import time

    started = time.monotonic()
    result = await ctx.providers.llm_for("section").generate(
        system=system,
        user=user,
        params=params,
    )
    # prefer provider-reported latency; fall back to wall clock
    latency_ms = result.latency_ms or int((time.monotonic() - started) * 1000)
    html = result.text.strip()
    SectionContent(html=html)  # schema gate: non-empty

    # prompt version = active section_user prompt version (0 when only fallback)
    prompt_version = _active_prompt_version(ctx, "section_user")
    from app.domain.chunker import word_count_from_html

    return {
        "html": html,
        "provider": result.provider,
        "model": result.model,
        "token_usage": {
            "prompt_tokens": result.usage.get("prompt_tokens", 0),
            "completion_tokens": result.usage.get("completion_tokens", 0),
        },
        "latency_ms": latency_ms,
        "prompt_version": prompt_version,
        "words": word_count_from_html(html),
    }


def _active_prompt_version(ctx: JobContext, ptype: str) -> int:
    # Version is carried on config (resolved once per job via resolve_all),
    # so no per-section PocketBase query is needed here.
    return ctx.config.prompt_version(ptype)


def _generation_params(ctx: JobContext, role: str = "outline") -> GenerationParams:
    cfg = ctx.config.role_llm(role)
    return GenerationParams(
        temperature=float(cfg.get("temperature") or 0.7),
        max_tokens=int(cfg.get("max_tokens") or 4096),
        timeout=float(cfg.get("timeout") or 120.0),
    )


_OUTLINE_TASK_FALLBACK = (
    "خروجی را فقط به‌صورت JSON معتبر با این ساختار بده:\n"
    '{"title": string, "slug": string, "sections": ['
    '{"heading": string, "content_brief": string, "internal_links": [{"title": string, "url": string, "anchor_text": string}]}'
    "]}\n"
    "سرفصل‌ها مختصر و حاوی کلمه کلیدی باشند؛ title باید عیناً شامل کلمه کلیدی باشد و slug از روی آن "
    "ساخته شود تا URL حاوی کلمه کلیدی باشد؛ دست‌کم یک تیتر بخش حاوی کلمه کلیدی باشد؛ "
    "هیچ توضیحی خارج از JSON ننویس."
)

_SECTION_TASK_FALLBACK = (
    "فقط HTML معتبر برای بخش برگردان: تیتر با <h2> و محتوا با <p>/<ul>/<ol>/<strong>/<em>/<a>؛ "
    "بدون استایل inline، بدون تیتر <h1>، بدون fence و بدون توضیح اضافه. "
    "هر بخش دست‌کم ۱۵۰ کلمه و دست‌کم یک لیست (ul/ol) داشته باشد؛ بخش اول مقاله باید اولین جمله‌اش را "
    "با کلمه کلیدی شروع کند؛ کلمه کلیدی را طبیعی و حدود ۱ تا ۲ بار در هر ۱۰۰ کلمه تکرار کن."
)


# ---------------------------------------------------------------------------
# Retrieval context (RetrievalService + InternalLinkingService + ContextBuilder)
# ---------------------------------------------------------------------------
async def _retrieval_data(ctx: JobContext, topic: dict[str, Any]) -> dict[str, Any]:
    """Run the retrieval subsystem → {context, links} for prompt variables.

    RetrievalService (embed → project-filtered search → optional rerank) →
    InternalLinkingService (filter/dedupe/rank) → ContextBuilder (budgeted
    passages). The LLM never receives full documents.
    """
    empty = {"context": "", "links": []}
    retrieval = ctx.config.retrieval
    if not retrieval.get("top_k"):
        return empty
    try:
        from app.schemas.retrieval import RetrievalOptions
        from app.services.internal_linking import ContextBuilder
        from app.services.retrieval import RetrievalService

        query = f"{topic.get('title') or ''} {topic.get('keyword') or ''}".strip()
        service = RetrievalService(ctx.pb, ctx.registry)
        options = RetrievalOptions(
            candidate_count=int(retrieval["top_k"]),
            similarity_threshold=float(retrieval["similarity_threshold"]) or None,
            rerank=bool(retrieval["rerank_enabled"]),
            rerank_top_n=int(retrieval["rerank_top_n"]),
        )
        results = await service.retrieve(ctx.config, query, options)
        if not results:
            return empty

        budget = ctx.config.context
        builder = ContextBuilder(
            max_links=int(budget.get("max_links") or 5),
            max_passages=int(budget.get("max_passages") or 5),
            max_chars=int(budget.get("max_chars") or 4000),
        )
        retrieval_ctx = builder.build(
            query,
            results,
            current_url="",
            current_title=str(topic.get("title") or ""),
        )
        links = [link.to_dict() for link in retrieval_ctx.links]
        ctx.info(
            "retrieval context built",
            {
                "query": query,
                "results": len(results),
                "links": len(links),
                "passages": len(retrieval_ctx.passages),
                "used_chars": retrieval_ctx.used_chars,
            },
        )
        return {"context": retrieval_ctx.format_for_prompt(), "links": links}
    except Exception as exc:
        ctx.warning("retrieval context unavailable", {"error": str(exc)})
        return empty


def _prompt_config_error(which: str, exc: Any) -> ProviderError:
    return ProviderError(
        f"prompt rendering failed ({which}): {exc}",
        retryable=False,
        details={"prompt": which, "unknown": getattr(exc, "unknown", [])},
    )
