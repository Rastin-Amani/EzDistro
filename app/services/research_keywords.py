"""Google Ads keyword collection for research runs.

Facts-first: every number persisted here comes straight from the provider
(Google Ads Keyword Planning). Nothing here invents search volume, competition
or bids — and Google Ads `competition` / `competitionIndex` are always *paid*
(ads) competition, never organic difficulty.

Targeting is resolved to REAL Google Ads constant ids via GAQL, so a run can
never silently target the wrong market. If a country or language cannot be
resolved the run fails loudly instead of mis-targeting.
"""

from __future__ import annotations

import time
from typing import Any

from app.domain.keywords import normalize_keyword
from app.jobs.context import JobContext
from app.providers.base import KeywordIdea, PermanentError, SEEDKind
from app.repositories.jobs import now_utc, pb_dt
from app.repositories.research import KeywordMetricRepo, KeywordRepo, KeywordVolumeRepo

PAGE_SIZE = 10_000  # Google Ads Keyword Planning practical maximum per page
KEYWORD_BATCH = 20  # keyword seeds per request — keeps one failed batch cheap
CONSTANT_TTL = 86_400.0  # language/geo constants are stable; refresh daily

_language_cache: dict[str, tuple[float, str]] = {}
_geo_cache: dict[str, tuple[float, dict[str, Any]]] = {}


# ---------------------------------------------------------------------------
# Constant resolution (GAQL)
# ---------------------------------------------------------------------------
def _cache_get(cache: dict[str, tuple[float, Any]], key: str) -> Any:
    hit = cache.get(key)
    if not hit:
        return None
    stamp, value = hit
    if time.monotonic() - stamp > CONSTANT_TTL:
        cache.pop(key, None)
        return None
    return value


async def _gaql(provider: Any, query: str) -> list[dict[str, Any]]:
    """Run a GAQL search when the provider supports it (Google Ads does)."""
    search = getattr(provider, "search", None)
    if search is None:
        return []
    return list(await search(query))


def _resource(row: dict[str, Any], key: str, plural: str) -> str:
    node = row.get(key) or {}
    name = node.get("resourceName") or ""
    if name:
        return str(name)
    ident = node.get("id")
    return f"{plural}/{ident}" if ident else ""


async def resolve_language(provider: Any, code: str) -> str:
    """Language code ('en', 'fa', 'es') → 'languageConstants/1000'."""
    code = (code or "").strip().lower()
    if not code or not code.replace("-", "").isalpha():
        raise PermanentError(f"research: invalid language code {code!r}")
    cached = _cache_get(_language_cache, code)
    if cached:
        return str(cached)
    rows = await _gaql(
        provider,
        "SELECT language_constant.id, language_constant.code FROM language_constant "
        f"WHERE language_constant.code = '{code}'",
    )
    for row in rows:
        name = _resource(row, "languageConstant", "languageConstants")
        if name:
            _language_cache[code] = (time.monotonic(), name)
            return name
    raise PermanentError(
        f"Google Ads does not recognise the language '{code}'. "
        "Use an ISO language code such as 'en', 'fa' or 'es'."
    )


async def resolve_geo(provider: Any, country_code: str) -> dict[str, Any]:
    """Country code ('US') → {'ids': ['geoTargetConstants/2840'], 'name': 'United States'}."""
    code = (country_code or "").strip().upper()
    if len(code) != 2 or not code.isalpha():
        raise PermanentError(f"research: invalid country code {country_code!r}")
    cached = _cache_get(_geo_cache, code)
    if cached:
        return dict(cached)
    rows = await _gaql(
        provider,
        "SELECT geo_target_constant.id, geo_target_constant.name, "
        "geo_target_constant.canonical_name, geo_target_constant.country_code "
        "FROM geo_target_constant WHERE geo_target_constant.country_code = "
        f"'{code}' AND geo_target_constant.target_type = 'Country'",
    )
    for row in rows:
        name = _resource(row, "geoTargetConstant", "geoTargetConstants")
        if not name:
            continue
        node = row.get("geoTargetConstant") or {}
        label = str(node.get("name") or node.get("canonicalName") or code)
        value = {"ids": [name], "name": label, "id": str(node.get("id") or "")}
        _geo_cache[code] = (time.monotonic(), value)
        return dict(value)
    raise PermanentError(
        f"Google Ads has no country target for '{code}'. "
        "Provide a two-letter country code (US, GB, DE …) or an explicit geo target id."
    )


# ---------------------------------------------------------------------------
# Seed planning
# ---------------------------------------------------------------------------
def as_url(value: str) -> str:
    value = (value or "").strip()
    if value and "://" not in value:
        return "https://" + value
    return value


def plan_batches(seeds: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Turn research seeds into the smallest number of provider requests.

    Keyword+URL mode is the union of the keyword batches and the URL seed: Google
    returns the same idea set, so combining them into one call would not add ideas.
    """
    keywords: list[str] = []
    single: list[dict[str, Any]] = []
    for seed in seeds:
        value = str(seed.get("value") or "").strip()
        if not value:
            continue
        kind = str(seed.get("seedType") or SEEDKind.KEYWORD)
        if kind == SEEDKind.KEYWORD:
            keywords.append(value)
        elif kind == SEEDKind.URL:
            single.append({"kind": SEEDKind.URL, "url": as_url(value), "label": value})
        elif kind in (SEEDKind.SITE, SEEDKind.KEYWORD_AND_URL, "competitor"):
            single.append({"kind": SEEDKind.SITE, "url": as_url(value), "label": value})

    batches: list[dict[str, Any]] = []
    for start in range(0, len(keywords), KEYWORD_BATCH):
        group = keywords[start : start + KEYWORD_BATCH]
        batches.append(
            {
                "kind": SEEDKind.KEYWORD,
                "keywords": group,
                "label": f"keywords {start + 1}-{start + len(group)}",
            }
        )
    batches.extend(single)
    return batches


def merge_ideas(store: dict[str, KeywordIdea], found: list[KeywordIdea]) -> list[KeywordIdea]:
    """Merge provider ideas into `store` (keyed by normalized text).

    Returns only the rows that should be persisted for this batch: brand-new
    keywords plus ones whose volume grew because a second seed surfaced them.
    """
    fresh: list[KeywordIdea] = []
    for idea in found:
        key = normalize_keyword(idea.text)
        if not key:
            continue
        current = store.get(key)
        if current is None or idea.avg_monthly_searches > current.avg_monthly_searches:
            store[key] = idea
            fresh.append(idea)
    return fresh


def _persist(
    *,
    pb: Any,
    project_id: str,
    run_id: str,
    language: str,
    location_id: str,
    locale: str,
    location_name: str,
    ideas: list[KeywordIdea],
) -> int:
    rows = [
        {
            "normalizedKeyword": normalize_keyword(idea.text),
            "displayKeyword": idea.text,
            "locale": locale,
            "locationName": location_name,
            "source": "google_ads",
        }
        for idea in ideas
        if idea.text.strip()
    ]
    if not rows:
        return 0
    by_key = KeywordRepo(pb).ensure_many(
        project=project_id, language=language, location_id=location_id, rows=rows
    )
    metrics: list[dict[str, Any]] = []
    volumes: dict[str, list[tuple[int, int, int]]] = {}
    for idea in ideas:
        record = by_key.get(normalize_keyword(idea.text))
        if not record:
            continue
        metrics.append(
            {
                "run": run_id,
                "keyword": record["id"],
                "avgMonthlySearches": int(idea.avg_monthly_searches or 0),
                "competition": idea.competition or "UNSPECIFIED",
                "competitionIndex": int(idea.competition_index or 0),
                "averageCpcMicros": int(idea.average_cpc_micros or 0),
                "lowTopOfPageBidMicros": int(idea.low_top_of_page_bid_micros or 0),
                "highTopOfPageBidMicros": int(idea.high_top_of_page_bid_micros or 0),
                "currencyCode": idea.currency_code or "",
                "observedAt": pb_dt(now_utc()),
            }
        )
        if idea.monthly_volumes:
            volumes[record["id"]] = idea.monthly_volumes
    KeywordMetricRepo(pb).upsert_many(metrics)
    volume_repo = KeywordVolumeRepo(pb)
    for keyword_id, points in volumes.items():
        volume_repo.replace_for_keyword(run_id, keyword_id, points)
    return len(metrics)


# ---------------------------------------------------------------------------
# Collection
# ---------------------------------------------------------------------------
async def resolve_targeting(provider: Any, targeting: dict[str, Any]) -> dict[str, Any]:
    """Fill in the real Google Ads constants a run needs (cached per code)."""
    locale = str(targeting.get("locale") or "en-US")
    language_code = str(targeting.get("language") or locale.split("-")[0] or "en")
    country = str(targeting.get("country") or "")

    language_id = str(targeting.get("languageId") or "").strip()
    if not language_id:
        language_id = await resolve_language(provider, language_code)

    geos: list[str] = [str(g) for g in (targeting.get("geoTargets") or []) if str(g).strip()]
    location_id = str(targeting.get("locationId") or "").strip()
    location_name = str(targeting.get("locationName") or "").strip()
    if not geos and country:
        geo = await resolve_geo(provider, country)
        geos = list(geo["ids"])
        location_id = location_id or geo.get("id") or country.upper()
        location_name = location_name or str(geo["name"])
    elif geos and not location_id:
        location_id = geos[0].rsplit("/", 1)[-1]

    return {
        "locale": locale,
        "language": language_code,
        "languageId": language_id,
        "geoTargets": geos,
        "locationId": location_id or locale,
        "locationName": location_name,
        "network": str(targeting.get("network") or "GOOGLE_SEARCH"),
        "includeAdultKeywords": bool(targeting.get("includeAdultKeywords")),
    }


async def collect_keywords(
    ctx: JobContext,
    *,
    provider: Any,
    run_id: str,
    customer_id: str,
    targeting: dict[str, Any],
    seeds: list[dict[str, Any]],
    max_keywords: int = 5_000,
) -> dict[str, Any]:
    """Collect, normalize and persist keyword ideas for one research run.

    Persisting per batch (not at the end) is what makes the run resumable: a
    crash after batch 40 re-upserts the same rows instead of restarting.
    """
    project_id = ctx.project_id
    resolved = await resolve_targeting(provider, targeting)
    batches = plan_batches(seeds)
    if not batches:
        raise PermanentError("no usable research seeds: add at least one keyword, URL or site")
    max_keywords = max(1, int(max_keywords))

    store: dict[str, KeywordIdea] = {}
    done = 0
    errors: list[str] = []
    budget_hit = False
    for index, batch in enumerate(batches, start=1):
        await ctx.check_cancelled()
        remaining = max_keywords - len(store)
        if remaining <= 0:
            budget_hit = True
            break
        ctx.progress(
            2 + int(58 * (index - 1) / len(batches)),
            stage="keyword_collection",
            message=f"Collecting keyword ideas ({index}/{len(batches)})…",
            current=index,
            total=len(batches),
        )
        try:
            found = await provider.generate_keyword_ideas(
                customer_id=customer_id,
                seed_kind=batch["kind"],
                keywords=batch.get("keywords"),
                url=batch.get("url", ""),
                language=resolved["languageId"],
                geo_targets=resolved["geoTargets"],
                network=resolved["network"],
                include_adult_keywords=resolved["includeAdultKeywords"],
                page_size=PAGE_SIZE,
                max_results=remaining,
            )
        except PermanentError:
            raise
        except Exception as exc:  # one bad seed must not lose the whole run
            errors.append(f"{batch['label']}: {exc}")
            ctx.warning("keyword batch failed", {"batch": batch["label"], "error": str(exc)})
            continue
        done += 1
        fresh = merge_ideas(store, found)
        if fresh:
            _persist(
                pb=ctx.pb,
                project_id=project_id,
                run_id=run_id,
                language=resolved["language"],
                location_id=resolved["locationId"],
                locale=resolved["locale"],
                location_name=resolved["locationName"],
                ideas=fresh,
            )

    ctx.info(
        "keyword collection finished",
        {"keywords": len(store), "batches": done, "errors": len(errors)},
    )
    return {
        "ideas": len(store),
        "batches": len(batches),
        "batchesDone": done,
        "errors": errors,
        "budgetExhausted": budget_hit,
        "languageId": resolved["languageId"],
        "geoTargets": resolved["geoTargets"],
        "locationId": resolved["locationId"],
        "locationName": resolved["locationName"],
    }


def _demo() -> None:
    batches = plan_batches(
        [
            {"seedType": "keyword", "value": "gym software"},
            {"seedType": "keyword", "value": ""},
            {"seedType": "url", "value": "example.com/features"},
            {"seedType": "competitor", "value": "rival.com"},
        ]
    )
    assert [b["kind"] for b in batches] == ["keyword", "url", "site"], batches
    assert batches[1]["url"] == "https://example.com/features"
    assert batches[0]["keywords"] == ["gym software"]

    store: dict[str, KeywordIdea] = {}
    fresh = merge_ideas(store, [KeywordIdea("Gym  Software", avg_monthly_searches=10)])
    assert len(fresh) == 1 and list(store) == ["gym software"]
    # A second seed surfacing the same keyword with less volume is not re-persisted.
    assert merge_ideas(store, [KeywordIdea("gym software", avg_monthly_searches=5)]) == []
    assert len(merge_ideas(store, [KeywordIdea("gym software", avg_monthly_searches=50)])) == 1


if __name__ == "__main__":
    _demo()
    print("research_keywords ok")
