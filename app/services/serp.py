"""Optional live-SERP enrichment.

`SERP_PROVIDER = none` is a first-class state, not a degraded error: without a
provider the research engine still produces demand, competitor, cluster and
opportunity data — this module simply records that live SERP signals were not
collected. Nothing here ever fabricates a ranking position.

SERP observations are time-sensitive, so every row carries `observedAt` +
provider and is cached with a TTL rather than treated as a permanent fact.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from app.jobs.context import JobContext
from app.providers.base import SERPObservation
from app.repositories.jobs import now_utc, pb_dt
from app.repositories.research import SerpQueryRepo, SerpResultRepo

# ponytail: fixed 7-day caching for on-demand lookups; make it provider-defined
# when a provider with a different freshness policy is actually plugged in.
SERP_TTL_SECONDS = 7 * 86_400
NONE_PROVIDER = "none"


def provider_name(provider: Any) -> str:
    return str(getattr(provider, "provider_name", "") or NONE_PROVIDER)


def serp_available(provider: Any) -> bool:
    """False for the NoneSERPProvider — callers render 'not configured', not an error."""
    if provider is None:
        return False
    if provider_name(provider) == NONE_PROVIDER:
        return False
    return bool(getattr(provider, "available", True))


def _is_fresh(record: dict[str, Any], *, ttl: int = SERP_TTL_SECONDS) -> bool:
    stamp = record.get("observedAt") or ""
    if not stamp:
        return False
    try:
        observed = dt.datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
    except ValueError:
        return False
    if observed.tzinfo is None:
        observed = observed.replace(tzinfo=dt.UTC)
    age = (now_utc() - observed).total_seconds()
    return 0 <= age < ttl


def persist_observation(
    pb: Any, *, project_id: str, run_id: str, observation: SERPObservation
) -> str:
    """Store one observation (+ its organic results). Returns the query row id."""
    queries = SerpQueryRepo(pb)
    record = queries.upsert(
        project=project_id,
        run=run_id,
        keyword=observation.keyword,
        provider=observation.provider,
        locale=observation.locale,
        location=observation.location,
        device=observation.device,
        result_count=len(observation.results),
        features=observation.features,
        questions=observation.questions,
        related_searches=observation.related_searches,
        observed_at=observation.observed_at or pb_dt(now_utc()),
    )
    SerpResultRepo(pb).replace_for_query(
        record["id"],
        [
            {
                "position": result.position,
                "url": result.url,
                "domain": result.domain,
                "title": result.title,
                "snippet": result.snippet,
                "isFeaturedSnippet": result.is_featured_snippet,
                "isPeopleAlsoAsk": result.is_people_also_ask,
            }
            for result in observation.results
        ],
    )
    return record["id"]


async def collect_serp(
    ctx: JobContext,
    *,
    provider: Any,
    run_id: str,
    keywords: list[str],
    locale: str,
    location: str = "",
    limit: int = 0,
    device: str = "desktop",
) -> dict[str, Any]:
    """Collect SERP observations for the highest-demand keywords.

    No-op (with an explicit 'unavailable' count) when no provider is configured.
    """
    if not serp_available(provider):
        return {"available": False, "requested": 0, "collected": 0, "unavailable": 0, "skipped": 0}

    targets = [k for k in keywords if k.strip()]
    if limit > 0:
        targets = targets[:limit]

    collected = skipped = unavailable = 0
    for index, keyword in enumerate(targets, start=1):
        await ctx.check_cancelled()
        ctx.progress(
            70 + int(6 * index / max(1, len(targets))),
            stage="serp",
            message=f"Collecting SERP data ({index}/{len(targets)})…",
            current=index,
            total=len(targets),
        )
        existing = SerpQueryRepo(ctx.pb).get_observation(
            ctx.project_id, keyword, provider_name(provider), locale, location
        )
        if existing and _is_fresh(existing):
            skipped += 1
            continue
        try:
            observation = await provider.search(
                keyword=keyword, locale=locale, location=location, device=device
            )
        except Exception as exc:  # one keyword must not abort the SERP stage
            unavailable += 1
            ctx.warning("serp lookup failed", {"keyword": keyword, "error": str(exc)})
            continue
        if observation is None:
            unavailable += 1
            continue
        persist_observation(
            ctx.pb, project_id=ctx.project_id, run_id=run_id, observation=observation
        )
        collected += 1

    return {
        "available": True,
        "requested": len(targets),
        "collected": collected,
        "unavailable": unavailable,
        "skipped": skipped,
    }


async def refresh_keyword(
    ctx_pb: Any,
    provider: Any,
    *,
    project_id: str,
    keyword: str,
    locale: str,
    location: str = "",
    run_id: str = "",
    device: str = "desktop",
    force: bool = False,
) -> tuple[str, dict[str, Any] | None]:
    """On-demand SERP lookup for one keyword. Returns (status, observation row)."""
    if not serp_available(provider):
        return "unavailable", None
    queries = SerpQueryRepo(ctx_pb)
    name = provider_name(provider)
    existing = queries.get_observation(project_id, keyword, name, locale, location)
    if existing and not force and _is_fresh(existing):
        return "cached", existing
    observation = await provider.search(
        keyword=keyword, locale=locale, location=location, device=device
    )
    if observation is None:
        return "unavailable", existing
    query_id = persist_observation(
        ctx_pb, project_id=project_id, run_id=run_id, observation=observation
    )
    return "refreshed", queries.get(query_id)
