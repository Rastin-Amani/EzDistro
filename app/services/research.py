"""Research run orchestrator.

Sequences one SEO research run end to end as a background job:

    validate → wordpress_sync → keyword_collection → competitor_crawl → serp
             → clustering → content_gaps → opportunities → finalize

Each stage records its own summary in ``research_runs.stageState`` and advances
``currentStage``/``progress``/``counts``, which is what makes a run both
inspectable and resumable: a stage that already has a summary is skipped unless
the run is forced, so a crash mid-run costs one stage, not the whole run.

Long work stays in the worker. Google Ads calls are serialised through the
provider's own semaphore; AI work is batched inside each service.
"""

from __future__ import annotations

from typing import Any

from app.config import settings as app_settings
from app.jobs.context import JobCancelled
from app.providers.base import PermanentError
from app.repositories.research import (
    CompetitorPageRepo,
    GoogleAdsConnectionRepo,
    ResearchRunRepo,
    ResearchSeedRepo,
)
from app.services.google_ads import client_for_connection
from app.services.research_clustering import cluster_run
from app.services.research_competitors import crawl_competitors
from app.services.research_keywords import collect_keywords
from app.services.research_opportunities import (
    compute_content_gaps,
    generate_opportunities,
)
from app.services.serp import collect_serp, serp_available
from app.services.wordpress_sync import sync_posts

STAGE_PERCENT = {
    "validate": 5,
    "wordpress_sync": 20,
    "keyword_collection": 50,
    "competitor_crawl": 62,
    "serp": 72,
    "clustering": 82,
    "gaps": 88,
    "opportunities": 96,
    "finalize": 100,
}
SERP_KEYWORD_LIMIT = 200


def _cap(value: Any, default: int, *, minimum: int = 0) -> int:
    """Coerce a config cap to an int; unset/invalid/below-minimum falls back to default."""
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = default
    return number if number >= minimum else default


class StageTracker:
    """Persists per-stage progress on the run and answers "already done?"."""

    def __init__(self, ctx: Any, run_id: str, *, force: bool = False) -> None:
        run = ResearchRunRepo(ctx.pb).get(run_id) or {}
        self.ctx = ctx
        self.run_id = run_id
        self.force = force
        self.state: dict[str, Any] = dict(run.get("stageState") or {})
        self.counts: dict[str, Any] = dict(run.get("counts") or {})

    def done(self, stage: str) -> bool:
        return stage in self.state and not self.force

    def start(self, stage: str, message: str = "") -> None:
        self.ctx.stage_started(stage, message or f"{stage} started")
        self.ctx.progress(STAGE_PERCENT.get(stage, 0), stage=stage, message=message or f"{stage}…")

    def record(self, stage: str, summary: dict[str, Any], message: str = "") -> None:
        self.state[stage] = summary
        self.counts[stage] = {
            key: value
            for key, value in summary.items()
            if isinstance(value, int | float | str | bool) and key != "errors"
        }
        counts = dict(self.counts)
        if stage == "keyword_collection":
            counts["keywords"] = summary.get("ideas", 0)
        elif stage == "clustering":
            counts["clusters"] = summary.get("clusters", 0)
        elif stage == "opportunities":
            counts["opportunities"] = summary.get("created", 0)
        ResearchRunRepo(self.ctx.pb).set_stage(
            self.run_id,
            stage,
            progress=STAGE_PERCENT.get(stage, 0),
            stage_state=self.state,
            counts=counts,
        )
        self.ctx.stage_completed(stage, message or f"{stage} completed", summary)


async def _run_stage(
    stages: StageTracker,
    stage: str,
    coro_factory: Any,
    *,
    message: str = "",
) -> dict[str, Any]:
    """Run a stage unless it already completed; return its (possibly cached) summary."""
    if stages.done(stage):
        return dict(stages.state.get(stage) or {})
    stages.start(stage, message)
    summary = await coro_factory()
    if not isinstance(summary, dict):
        summary = {"result": summary}
    stages.record(stage, summary, message)
    return summary


async def _validate(ctx: Any, run: dict[str, Any], seeds: list[dict[str, Any]]) -> dict[str, Any]:
    if not seeds:
        raise PermanentError(
            "this research run has no seeds: add at least one keyword, URL or competitor"
        )
    kinds = {str(seed.get("seedType") or "") for seed in seeds}
    notes: list[str] = []
    if kinds & {"keyword", "url", "site", "keyword_and_url"}:
        if not run.get("connection"):
            raise PermanentError(
                "connect Google Ads before running keyword research, "
                "or run competitor-only research"
            )
        if not run.get("customerId"):
            raise PermanentError("pick a Google Ads customer for this research run")
    else:
        notes.append("no Google Ads seeds — keyword collection will be skipped")
    return {"seeds": len(seeds), "kinds": sorted(kinds), "notes": notes}


async def _wordpress_stage(ctx: Any) -> dict[str, Any]:
    try:
        publisher = ctx.providers.publisher
    except PermanentError as exc:
        return {"skipped": True, "reason": str(exc)[:200], "created": 0}
    result = await sync_posts(ctx, publisher)
    return dict(result)


async def _keyword_stage(
    ctx: Any, run: dict[str, Any], seeds: list[dict[str, Any]], *, max_keywords: int
) -> dict[str, Any]:
    if not any(str(seed.get("seedType")) != "competitor" for seed in seeds):
        return {"ideas": 0, "skipped": True, "reason": "competitor-only research run"}
    connection = GoogleAdsConnectionRepo(ctx.pb).get(run.get("connection") or "")
    if connection is None:
        raise PermanentError(
            "the Google Ads connection for this run no longer exists — reconnect Google Ads"
        )
    client = client_for_connection(connection)
    try:
        result = await collect_keywords(
            ctx,
            provider=client,
            run_id=run["id"],
            customer_id=str(run.get("customerId") or ""),
            targeting=dict(run.get("targeting") or {}),
            seeds=seeds,
            max_keywords=max_keywords,
        )
    finally:
        await client.aclose()
    return dict(result)


async def _competitor_stage(
    ctx: Any, run: dict[str, Any], seeds: list[dict[str, Any]], *, max_pages: int
) -> dict[str, Any]:
    domains = [
        str(seed.get("value") or "") for seed in seeds if str(seed.get("seedType")) == "competitor"
    ]
    domains = [domain for domain in domains if domain.strip()]
    if not domains:
        return {"domains": 0, "pages": 0, "skipped": True, "reason": "no competitor domains"}
    return dict(
        await crawl_competitors(
            ctx,
            run_id=run["id"],
            domains=domains,
            max_pages=max_pages,
            concurrency=int(app_settings.research_crawl_concurrency or 4),
        )
    )


async def _serp_stage(
    ctx: Any, run: dict[str, Any], *, limit: int, locale: str, location: str
) -> dict[str, Any]:
    provider = ctx.registry.get_serp_provider(ctx.config.project, ctx.config.settings)
    if not serp_available(provider):
        return {"available": False, "requested": 0, "collected": 0, "reason": "no SERP provider"}
    if limit <= 0:
        return {"available": True, "requested": 0, "collected": 0, "reason": "SERP budget is 0"}
    keywords = _top_keywords(ctx, run["id"], limit=min(limit, SERP_KEYWORD_LIMIT))
    return dict(
        await collect_serp(
            ctx,
            provider=provider,
            run_id=run["id"],
            keywords=keywords,
            locale=locale,
            location=location,
            limit=limit,
        )
    )


def _top_keywords(ctx: Any, run_id: str, *, limit: int) -> list[str]:
    from app.repositories.research import KeywordMetricRepo, KeywordRepo

    metrics = KeywordMetricRepo(ctx.pb).list_for_run(
        run_id, sort="-avgMonthlySearches", page=1, per_page=min(limit, 500)
    )
    texts = KeywordRepo(ctx.pb).map_by_ids([row.get("keyword") or "" for row in metrics])
    out: list[str] = []
    for row in metrics:
        keyword_row = texts.get(row.get("keyword") or "") or {}
        text = keyword_row.get("displayKeyword") or keyword_row.get("normalizedKeyword") or ""
        if text:
            out.append(text)
    return out[:limit]


async def research_pipeline(
    ctx: Any, *, run: dict[str, Any], force: bool = False
) -> dict[str, Any]:
    """Run every stage for one research run."""
    run_id = run["id"]
    config = dict(run.get("config") or {})
    targeting = dict(run.get("targeting") or {})
    seeds = [
        {"seedType": row.get("seedType"), "value": row.get("value")}
        for row in ResearchSeedRepo(ctx.pb).list_for_run(run_id)
    ]
    if not seeds:
        seeds = [dict(seed) for seed in config.get("seeds") or [] if isinstance(seed, dict)]

    force = force or bool(config.get("force"))
    stages = StageTracker(ctx, run_id, force=force)
    max_keywords = _cap(
        config.get("maxKeywords"), app_settings.research_max_keywords_per_run, minimum=1
    )
    pages_config = config.get("maxCompetitorPages")
    max_pages = int(pages_config) if pages_config else app_settings.research_max_competitor_pages
    max_serp = _cap(config.get("maxSerpQueries"), app_settings.research_max_serp_queries)
    locale = str(targeting.get("locale") or "")
    location = str(targeting.get("locationName") or targeting.get("location") or "")

    await _run_stage(
        stages,
        "validate",
        lambda: _validate(ctx, run, seeds),
        message="Validating research configuration…",
    )
    await _run_stage(
        stages,
        "wordpress_sync",
        lambda: _wordpress_stage(ctx),
        message="Synchronizing WordPress content…",
    )
    await _run_stage(
        stages,
        "keyword_collection",
        lambda: _keyword_stage(ctx, run, seeds, max_keywords=max_keywords),
        message="Collecting keyword ideas from Google Ads…",
    )
    await _run_stage(
        stages,
        "competitor_crawl",
        lambda: _competitor_stage(ctx, run, seeds, max_pages=max_pages),
        message="Analyzing competitor content…",
    )
    await _run_stage(
        stages,
        "serp",
        lambda: _serp_stage(ctx, run, limit=max_serp, locale=locale, location=location),
        message="Collecting SERP observations…",
    )
    method = str(config.get("clustering") or "auto")
    await _run_stage(
        stages,
        "clustering",
        lambda: cluster_run(ctx, run_id=run_id, method=method),
        message="Clustering keywords…",
    )
    await _run_stage(
        stages,
        "gaps",
        lambda: _gaps(ctx, run_id),
        message="Comparing your content with competitors…",
    )
    await _run_stage(
        stages,
        "opportunities",
        lambda: generate_opportunities(
            ctx,
            run_id=run_id,
            goal_text=str(config.get("goal") or ""),
            structured_goal=config.get("goalOptions") or None,
            max_opportunities=int(config.get("maxOpportunities") or 0),
        ),
        message="Generating article opportunities…",
    )
    summary = await _run_stage(
        stages, "finalize", lambda: _finalize(ctx, run_id), message="Finalizing research…"
    )
    return {"runId": run_id, "stages": sorted(stages.state), "counts": stages.counts, **summary}


async def _gaps(ctx: Any, run_id: str) -> dict[str, Any]:
    from app.repositories.research import ContentGapRepo

    rows = compute_content_gaps(ctx.pb, run_id=run_id, project_id=ctx.project_id)
    by_type: dict[str, int] = {}
    for row in ContentGapRepo(ctx.pb).list_all(filter=f'run="{run_id}"'):
        gap_type = str(row.get("gapType") or "")
        by_type[gap_type] = by_type.get(gap_type, 0) + 1
    return {"gaps": rows, "byType": by_type}


async def _finalize(ctx: Any, run_id: str) -> dict[str, Any]:
    from app.repositories.research import (
        ArticleIdeaRepo,
        ClusterRepo,
        ContentGapRepo,
        KeywordMetricRepo,
    )

    run = ResearchRunRepo(ctx.pb).get(run_id) or {}
    project_id = run.get("project") or ctx.project_id
    keywords = KeywordMetricRepo(ctx.pb).count_for_run(run_id)
    clusters = ClusterRepo(ctx.pb).count_for_run(run_id)
    pages = CompetitorPageRepo(ctx.pb).count_for_run(run_id)
    gaps = ContentGapRepo(ctx.pb).count_for_run(run_id)
    ideas = ArticleIdeaRepo(ctx.pb).summarize_run(run_id)
    ResearchRunRepo(ctx.pb).set_status(run_id, "completed")
    return {
        "keywords": keywords,
        "clusters": clusters,
        "competitorPages": pages,
        "contentGaps": gaps,
        "opportunities": ideas.get("total", 0),
        "actions": {
            key: ideas.get(key, 0) for key in ("generate", "update", "expand", "support", "reject")
        },
        "project": project_id,
    }


def _register() -> None:
    """Register the job handler (import-time side effect, like other services)."""
    from app.jobs.handlers import register_job

    @register_job("research_run")
    async def handle_research_run(ctx: Any) -> dict[str, Any]:  # noqa: D401
        payload = ctx.payload()
        run_id = str(payload.get("runId") or "")
        if not run_id:
            raise PermanentError("research_run job requires a runId in its payload")
        runs = ResearchRunRepo(ctx.pb)
        run = runs.get(run_id)
        if run is None:
            raise PermanentError("research run not found")
        runs.set_status(run_id, "running")
        try:
            return await research_pipeline(ctx, run=run, force=bool(payload.get("force")))
        except JobCancelled:
            runs.set_status(
                run_id,
                "cancelled",
                error_code="cancelled",
                error_message="Research run cancelled",
            )
            raise
        except PermanentError as exc:
            runs.set_status(
                run_id,
                "failed",
                error_code="permanent_error",
                error_message=str(exc)[:500],
            )
            raise
        except Exception as exc:
            runs.set_status(
                run_id,
                "failed",
                error_code=type(exc).__name__,
                error_message=str(exc)[:500],
            )
            raise


_register()
