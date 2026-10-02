"""SEO research — Google Ads connection, research runs, keyword/topic workspaces.

Facts-first SEO research built on the existing job engine:

    Google Ads OAuth → targeting + seeds → research run (background job)
        → keywords / competitors / clusters / gaps / opportunities
        → accepted opportunities become articles in the existing pipeline.

Nothing here computes SEO metrics: every number comes from Google Ads, a
configured SERP provider, a crawled page, or the project's own content.
Google Ads competition is PAID competition and is never rendered as organic
difficulty.
"""

from __future__ import annotations

import csv
import io
import json
import time
from typing import Any

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse

from app.api.deps import (
    current_user,
    require_hx,
    require_project_access,
    require_project_role,
    require_user,
    safe_bool,
    safe_int,
    safe_str,
)
from app.api.errors import hx_error, page_guard
from app.domain.keywords import normalize_keyword
from app.providers.base import PermanentError
from app.repositories.articles import ArticleRepo
from app.repositories.jobs import JobRepo
from app.repositories.research import (
    ArticleIdeaRepo,
    ClusterRepo,
    CompetitorPageRepo,
    ContentGapRepo,
    GoogleAdsConnectionRepo,
    GoogleAdsCustomerRepo,
    KeywordMetricRepo,
    KeywordRepo,
    KeywordVolumeRepo,
    ResearchRunRepo,
    ResearchSeedRepo,
    SerpQueryRepo,
    SerpResultRepo,
)
from app.services import google_ads as google_ads_service
from app.services.serp import refresh_keyword, serp_available
from app.templates import templates
from app.utils import error_response, ok_with_redirect, success_response

router = APIRouter()

RUN_PAGE = 25
IDEA_PAGE = 25
KEYWORD_PAGE = 50
MAX_SEEDS = 200
ACTIONS = ("accept", "reject", "roadmap")


def _project_or_none(request: Request, project_id: str) -> dict[str, Any] | None:
    try:
        return require_project_access(request, project_id)
    except (PermissionError, ValueError):
        return None


def _run_or_none(request: Request, project_id: str, run_id: str) -> dict[str, Any] | None:
    run = ResearchRunRepo(request.state.pb).get(run_id)
    if run is None or run.get("project") != project_id:
        return None
    return run


def _lines(value: str, *, limit: int = MAX_SEEDS) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for line in (value or "").splitlines():
        text = line.strip()
        if not text or text in seen:
            continue
        seen.add(text)
        out.append(text[:2000])
        if len(out) >= limit:
            break
    return out


def _seed_rows(
    *,
    keywords: str,
    site: str,
    urls: str,
    competitors: str,
) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for value in _lines(keywords):
        rows.append(
            {
                "seedType": "keyword",
                "value": value,
                "normalizedValue": normalize_keyword(value),
            }
        )
    if site.strip():
        rows.append(
            {
                "seedType": "site",
                "value": site.strip()[:2000],
                "normalizedValue": site.strip().lower()[:2000],
            }
        )
    for value in _lines(urls, limit=50):
        rows.append({"seedType": "url", "value": value, "normalizedValue": value.lower()})
    for value in _lines(competitors, limit=50):
        rows.append(
            {
                "seedType": "competitor",
                "value": value,
                "normalizedValue": value.lower().removeprefix("https://").rstrip("/"),
            }
        )
    return rows


# --------------------------------------------------------------------------- #
# Google Ads connection
# --------------------------------------------------------------------------- #
@router.get("/projects/google-ads/connect", response_class=HTMLResponse)
def google_ads_connect(request: Request, project_id: str = ""):
    """Entry point of the Google consent flow (a browser navigation)."""
    user = require_user(request)
    pb = request.state.pb
    if not google_ads_service.client_configured(pb, project_id):
        target = f"/projects/{project_id}?tab=research&ga=unconfigured" if project_id else "/"
        return RedirectResponse(target, status_code=303)
    state = google_ads_service.make_state(user_id=user["id"], project_id=project_id)
    url = google_ads_service.authorize_url(
        state, request_base=str(request.base_url), pb=pb, project_id=project_id
    )
    return RedirectResponse(url, status_code=303)


@router.get("/auth/google-ads/callback", response_class=HTMLResponse)
@router.get("/projects/google-ads/callback", response_class=HTMLResponse)
async def google_ads_callback(
    request: Request,
    code: str = "",
    state: str = "",
    error: str = "",
):
    """OAuth redirect target. Always a navigation → redirect with a flag."""
    require_user(request)
    payload: dict[str, Any] = {}
    try:
        payload = google_ads_service.read_state(state)
    except PermanentError:
        payload = {}
    project_id = str(payload.get("project_id") or "")
    target = f"/projects/{project_id}?tab=research" if project_id else "/"
    if error or not code:
        flag = "denied" if error == "access_denied" else "error"
        return RedirectResponse(f"{target}&ga={flag}", status_code=303)
    if not payload:
        return RedirectResponse("/?ga=error", status_code=303)
    try:
        await google_ads_service.connect(
            request.state.pb,
            user_id=str(payload.get("user_id") or ""),
            code=code,
            request_base=str(request.base_url),
            project_id=project_id,
        )
    except Exception:
        return RedirectResponse(f"{target}&ga=error", status_code=303)
    return RedirectResponse(f"{target}&ga=connected", status_code=303)


@router.post("/projects/{project_id}/google-ads/disconnect")
@hx_error("Could not disconnect Google Ads.")
def google_ads_disconnect(request: Request, project_id: str):
    require_hx(request)
    require_project_access(request, project_id)
    require_project_role(request, project_id)
    user = require_user(request)
    connection = GoogleAdsConnectionRepo(request.state.pb).for_user(user["id"])
    if connection:
        google_ads_service.disconnect(request.state.pb, connection["id"])
    return success_response("Google Ads disconnected.", extra_events={"refreshResearch": True})


@router.post("/projects/{project_id}/google-ads/customers/discover")
@hx_error("Could not read the Google Ads customers for this account.")
async def google_ads_discover(request: Request, project_id: str):
    require_hx(request)
    require_project_access(request, project_id)
    require_project_role(request, project_id)
    user = require_user(request)
    connection = GoogleAdsConnectionRepo(request.state.pb).for_user(user["id"])
    if connection is None:
        return error_response("Connect Google Ads first.")
    try:
        customers = await google_ads_service.discover_customers(
            request.state.pb, connection, project_id=project_id
        )
    except PermanentError as exc:
        return error_response(str(exc))
    return success_response(
        f"Found {len(customers)} Google Ads customer(s).",
        extra_events={"refreshResearch": True},
    )


@router.post("/projects/{project_id}/google-ads/customers/select")
@hx_error("Could not select that Google Ads customer.")
def google_ads_select(
    request: Request,
    project_id: str,
    customer_record_id: str = Form(""),
):
    require_hx(request)
    require_project_access(request, project_id)
    require_project_role(request, project_id)
    user = require_user(request)
    connection = GoogleAdsConnectionRepo(request.state.pb).for_user(user["id"])
    if connection is None:
        return error_response("Connect Google Ads first.")
    record = GoogleAdsCustomerRepo(request.state.pb).get(customer_record_id)
    if record is None or record.get("connection") != connection["id"]:
        return error_response("That Google Ads customer is not available on this connection.")
    google_ads_service.attach_customer_to_project(
        request.state.pb, project_id=project_id, customer_record_id=customer_record_id
    )
    return success_response("Google Ads customer selected.", extra_events={"refreshResearch": True})


# --------------------------------------------------------------------------- #
# Research runs
# --------------------------------------------------------------------------- #
@router.post("/projects/{project_id}/research/start")
@hx_error("Could not start the research run.")
def research_start(
    request: Request,
    project_id: str,
    name: str = Form(""),
    keywords: str = Form(""),
    site: str = Form(""),
    urls: str = Form(""),
    competitors: str = Form(""),
    country: str = Form(""),
    language: str = Form("en"),
    locale: str = Form(""),
    network: str = Form("GOOGLE_SEARCH"),
    include_adult: str = Form(""),
    clustering: str = Form("auto"),
    goal: str = Form(""),
    connection_id: str = Form(""),
    customer_id: str = Form(""),
    max_keywords: str = Form(""),
    max_competitor_pages: str = Form(""),
    max_serp_queries: str = Form(""),
    refresh: str = Form(""),
):
    require_hx(request)
    require_project_access(request, project_id)
    require_project_role(request, project_id)
    user = current_user(request) or {}

    seeds = _seed_rows(keywords=keywords, site=site, urls=urls, competitors=competitors)
    if not seeds:
        return error_response(
            "Add at least one keyword, URL, site or competitor domain to research."
        )

    needs_ads = any(row["seedType"] != "competitor" for row in seeds)
    target_country = (safe_str(country) or "US").upper()[:2]
    target_locale = safe_str(locale) or f"{safe_str(language) or 'en'}-{target_country}"
    if needs_ads and not customer_id:
        return error_response("Pick a Google Ads customer before running keyword research.")

    targeting = {
        "country": target_country,
        "language": (safe_str(language) or "en").lower()[:5],
        "locale": target_locale[:10],
        "network": safe_str(network) or "GOOGLE_SEARCH",
        "includeAdultKeywords": safe_bool(include_adult),
    }
    config: dict[str, Any] = {
        "seeds": seeds,
        "clustering": safe_str(clustering) or "auto",
        "goal": safe_str(goal)[:4000],
        "targeting": targeting,
    }
    for key, raw, floor in (
        ("maxKeywords", max_keywords, 1),
        ("maxCompetitorPages", max_competitor_pages, 1),
        ("maxSerpQueries", max_serp_queries, 0),
    ):
        value = safe_int(raw, 0)
        if value >= floor and safe_str(raw).strip():
            config[key] = value
    force = safe_bool(refresh)
    if force:
        config["force"] = True

    research_type = "competitors" if not needs_ads else "mixed"
    if needs_ads and {row["seedType"] for row in seeds} == {"keyword"}:
        research_type = "keywords"

    fingerprint = ResearchRunRepo.fingerprint(targeting, seeds)
    runs = ResearchRunRepo(request.state.pb)
    existing = runs.for_fingerprint(project_id, fingerprint)
    if existing and not force:
        return ok_with_redirect(
            "Reusing the previous research run for the same targeting and seeds.",
            f"/projects/{project_id}/research/{existing['id']}",
        )

    run = runs.create_run(
        project=project_id,
        name=(safe_str(name) or f"Research {time.strftime('%Y-%m-%d %H:%M')}")[:120],
        research_type=research_type,
        config=config,
        targeting=targeting,
        fingerprint=fingerprint,
        connection=connection_id or "",
        customer_id=customer_id,
        created_by=str(user.get("id") or ""),
    )
    ResearchSeedRepo(request.state.pb).replace_for_run(run["id"], seeds)
    job = JobRepo(request.state.pb).create(
        project=project_id,
        type="research_run",
        payload={"runId": run["id"], "force": force},
        idempotency_key=f"research_run:{run['id']}",
        entity_type="research_run",
        entity_id=run["id"],
        max_attempts=2,
    )
    runs.update(run["id"], {"job": job["id"]})
    return ok_with_redirect(
        "Research started — this runs in the background.",
        f"/projects/{project_id}/research/{run['id']}",
    )


@router.post("/projects/{project_id}/research/{run_id}/cancel")
@hx_error("Could not cancel the research run.")
def research_cancel(request: Request, project_id: str, run_id: str):
    require_hx(request)
    require_project_access(request, project_id)
    require_project_role(request, project_id)
    run = _run_or_none(request, project_id, run_id)
    if run is None:
        return error_response("Research run not found.")
    job_id = run.get("job") or ""
    if job_id:
        JobRepo(request.state.pb).update(job_id, {"cancelRequested": True, "status": "cancelled"})
    ResearchRunRepo(request.state.pb).set_status(
        run_id, "cancelled", error_code="cancelled", error_message="Cancelled by user"
    )
    return success_response("Research run cancelled.", extra_events={"refreshResearch": True})


@router.get("/projects/{project_id}/research/{run_id}", response_class=HTMLResponse)
@page_guard("Something went wrong loading this research run — please try again.")
def research_run_page(request: Request, project_id: str, run_id: str, tab: str = "keywords"):
    project = require_project_access(request, project_id)
    run = _run_or_none(request, project_id, run_id)
    if run is None:
        return templates.TemplateResponse(
            request,
            "pages/projects/not_found.html",
            {"title": ("Research run not found"), "project": project},
        )
    active = (
        tab
        if tab in {"keywords", "serp", "competitors", "clusters", "gaps", "opportunities"}
        else "keywords"
    )
    context = _run_context(request, project, run, active)
    context["title"] = run.get("name") or "Research"
    return templates.TemplateResponse(request, "pages/research/run.html", context)


def _run_context(
    request: Request,
    project: dict[str, Any],
    run: dict[str, Any],
    active: str,
    *,
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Shared context for the research page and every tab fragment."""
    pb = request.state.pb
    context: dict[str, Any] = {
        "project": project,
        "run": run,
        "active_tab": active,
        "tabs": ["keywords", "serp", "competitors", "clusters", "gaps", "opportunities"],
        "params": params or {},
    }
    if active == "keywords":
        context.update(_keywords_context(pb, run, params or {}))
    elif active == "serp":
        context.update(_serp_context(pb, run))
    elif active == "competitors":
        context.update(_competitors_context(pb, run))
    elif active == "clusters":
        context.update(_clusters_context(pb, run))
    elif active == "gaps":
        context.update(_gaps_context(pb, run))
    elif active == "opportunities":
        context.update(_opportunities_context(pb, run, params or {}))
    return context


def _keywords_context(pb: Any, run: dict[str, Any], params: dict[str, Any]) -> dict[str, Any]:
    run_id = run["id"]
    metrics = KeywordMetricRepo(pb)
    query = safe_str(params.get("q"))
    min_volume = safe_int(params.get("min_volume"), 0)
    filters = []
    if min_volume > 0:
        filters.append(f"avgMonthlySearches >= {min_volume}")
    if safe_str(params.get("cluster")):
        filters.append(f"cluster={_q(safe_str(params.get('cluster')))}")
    if query:
        keywords = KeywordRepo(pb)
        matched = keywords.map_by_normalized(
            run.get("project") or "",
            safe_str((run.get("targeting") or {}).get("language")),
            safe_str((run.get("targeting") or {}).get("locationId")),
            [normalize_keyword(query)],
        )
        ids = [row["id"] for row in matched.values()]
        if not ids:
            return {
                "rows": [],
                "total": 0,
                "stats": metrics.stats_for_run(run_id),
                "q": query,
                "min_volume": min_volume,
                "page": 1,
                "clusters": ClusterRepo(pb).list_for_run(run_id),
            }
        filters.append("(" + " || ".join(f"keyword={_q(i)}" for i in ids) + ")")
    page = max(1, safe_int(params.get("page"), 1))
    rows = metrics.list_for_run(
        run_id,
        filter=" && ".join(filters),
        sort=safe_str(params.get("sort")) or "-avgMonthlySearches",
        page=page,
        per_page=KEYWORD_PAGE,
    )
    keyword_rows = KeywordRepo(pb).map_by_ids([row.get("keyword") or "" for row in rows])
    clusters = ClusterRepo(pb).map_for_run(run_id)
    table: list[dict[str, Any]] = []
    for row in rows:
        keyword_row = keyword_rows.get(row.get("keyword") or "") or {}
        table.append(
            {
                "metric": row,
                "text": keyword_row.get("displayKeyword")
                or keyword_row.get("normalizedKeyword")
                or "",
                "cluster": (clusters.get(row.get("cluster") or "") or {}).get("name") or "",
            }
        )
    return {
        "rows": table,
        "total": metrics.count_for_run(run_id, filter=" && ".join(filters)),
        "stats": metrics.stats_for_run(run_id),
        "clusters": ClusterRepo(pb).list_for_run(run_id),
        "q": query,
        "min_volume": min_volume,
        "page": page,
        "per_page": KEYWORD_PAGE,
    }


def _serp_context(pb: Any, run: dict[str, Any]) -> dict[str, Any]:
    run_id = run["id"]
    queries = SerpQueryRepo(pb).list_for_run(run_id, per_page=50)
    return {
        "queries": queries,
        "available": bool(queries),
        "configured": _project_serp_configured(pb, run),
        "count": SerpQueryRepo(pb).count_for_run(run_id),
    }


def _project_serp_configured(pb: Any, run: dict[str, Any]) -> bool:
    from app.providers.registry import ProviderRegistry
    from app.services.settings import ProjectConfig

    try:
        config = ProjectConfig.load(pb, run.get("project") or "")
        provider = ProviderRegistry(pb).get_serp_provider(config.project, config.settings)
    except Exception:
        return False
    return serp_available(provider)


def _competitors_context(pb: Any, run: dict[str, Any]) -> dict[str, Any]:
    rows = CompetitorPageRepo(pb).list_for_run(run["id"], per_page=50)
    return {"pages": rows, "total": CompetitorPageRepo(pb).count_for_run(run["id"])}


def _clusters_context(pb: Any, run: dict[str, Any]) -> dict[str, Any]:
    clusters = ClusterRepo(pb).list_for_run(run["id"])
    metrics = KeywordMetricRepo(pb).list_for_run(run["id"], per_page=500)
    by_cluster: dict[str, list[dict[str, Any]]] = {}
    for row in metrics:
        by_cluster.setdefault(row.get("cluster") or "", []).append(row)
    return {"clusters": clusters, "by_cluster": by_cluster}


def _gaps_context(pb: Any, run: dict[str, Any]) -> dict[str, Any]:
    rows = ContentGapRepo(pb).list_for_run(run["id"], per_page=50)
    gaps = ContentGapRepo(pb)
    return {
        "gaps": rows,
        "total": gaps.count_for_run(run["id"]),
        "by_type": {
            gap_type: gaps.count_for_run(run["id"], gap_type=gap_type)
            for gap_type in (
                "competitor_only",
                "under_served",
                "expansion",
                "update",
                "supporting",
                "you_only",
                "both",
            )
        },
    }


def _opportunities_context(pb: Any, run: dict[str, Any], params: dict[str, Any]) -> dict[str, Any]:
    run_id = run["id"]
    ideas = ArticleIdeaRepo(pb)
    action = safe_str(params.get("action"))
    status = safe_str(params.get("status"))
    filters = []
    if action:
        filters.append(f"action={_q(action)}")
    if status:
        filters.append(f"status={_q(status)}")
    page = max(1, safe_int(params.get("page"), 1))
    # Only 'proposed' ideas are managed here; accepted ones live in Articles.
    if not status:
        filters.append('status="proposed"')
    rows = ideas.list_for_run(
        run_id,
        filter=" && ".join(filters),
        sort=safe_str(params.get("sort")) or "-opportunityScore",
        page=page,
        per_page=IDEA_PAGE,
    )
    return {
        "ideas": rows,
        "total": ideas.count_for_run(run_id, filter=" && ".join(filters)),
        "summary": ideas.summarize_run(run_id),
        "action": action,
        "status": status,
        "page": page,
        "per_page": IDEA_PAGE,
    }


def _q(value: str) -> str:
    from app.repositories.research import q

    return q(value)


@router.get("/projects/{project_id}/research/{run_id}/status", response_class=HTMLResponse)
def research_status(request: Request, project_id: str, run_id: str):
    """Polled fragment: live stage progress."""
    project = _project_or_none(request, project_id)
    run = _run_or_none(request, project_id, run_id) if project else None
    if project is None or run is None:
        return HTMLResponse("")
    counts = dict(run.get("counts") or {})
    ideas = ArticleIdeaRepo(request.state.pb).summarize_run(run_id)
    return templates.TemplateResponse(
        request,
        "pages/research/_status.html",
        {"project": project, "run": run, "counts": counts, "summary": ideas},
    )


@router.get("/projects/{project_id}/research/{run_id}/tab/{tab}", response_class=HTMLResponse)
def research_tab(
    request: Request,
    project_id: str,
    run_id: str,
    tab: str,
    q: str = "",
    page: str = "",
    action: str = "",
    status: str = "",
    min_volume: str = "",
    sort: str = "",
):
    """HTMX fragment for one workspace tab."""
    project = _project_or_none(request, project_id)
    run = _run_or_none(request, project_id, run_id) if project else None
    if project is None or run is None:
        return HTMLResponse("")
    if tab not in {"keywords", "serp", "competitors", "clusters", "gaps", "opportunities"}:
        return HTMLResponse("")
    params = {
        "q": q,
        "page": page,
        "action": action,
        "status": status,
        "min_volume": min_volume,
        "sort": sort,
    }
    context = _run_context(request, project, run, tab, params=params)
    return templates.TemplateResponse(request, f"pages/research/tabs/_{tab}.html", context)


@router.get(
    "/projects/{project_id}/research/{run_id}/keywords/{metric_id}",
    response_class=HTMLResponse,
)
def keyword_detail(request: Request, project_id: str, run_id: str, metric_id: str):
    """Keyword drawer: real metrics, monthly trend, coverage, SERP status."""
    project = _project_or_none(request, project_id)
    run = _run_or_none(request, project_id, run_id) if project else None
    if project is None or run is None:
        return HTMLResponse("")
    pb = request.state.pb
    metric = KeywordMetricRepo(pb).get(metric_id)
    if metric is None or metric.get("run") != run_id:
        return HTMLResponse("")
    keyword_row = KeywordRepo(pb).get(metric.get("keyword") or "") or {}
    volumes = KeywordVolumeRepo(pb).for_keyword(run_id, metric.get("keyword") or "")
    observation = SerpQueryRepo(pb).get_observation(
        project_id,
        keyword_row.get("displayKeyword") or keyword_row.get("normalizedKeyword") or "",
        "",
        safe_str((run.get("targeting") or {}).get("locale")),
        "",
    )
    results = SerpResultRepo(pb).list_for_query(observation["id"]) if observation else []
    cluster = ClusterRepo(pb).get(metric.get("cluster") or "") if metric.get("cluster") else None
    related = ArticleRepo(pb).list_for_project(project_id, per_page=500)
    text = keyword_row.get("displayKeyword") or keyword_row.get("normalizedKeyword") or ""
    matches = [
        article
        for article in related
        if text and normalize_keyword(text) in normalize_keyword(article.get("title") or "")
    ][:5]
    return templates.TemplateResponse(
        request,
        "pages/research/_keyword_detail.html",
        {
            "project": project,
            "run": run,
            "metric": metric,
            "keyword": keyword_row,
            "volumes": volumes,
            "cluster": cluster,
            "observation": observation,
            "results": results,
            "articles": matches,
            "max_volume": max([row.get("monthlySearches") or 0 for row in volumes] or [0]),
        },
    )


@router.post("/projects/{project_id}/research/{run_id}/keywords/{metric_id}/serp")
@hx_error("Could not refresh SERP data for this keyword.")
async def keyword_serp_refresh(request: Request, project_id: str, run_id: str, metric_id: str):
    require_hx(request)
    require_project_access(request, project_id)
    require_project_role(request, project_id)
    run = _run_or_none(request, project_id, run_id)
    if run is None:
        return error_response("Research run not found.")
    pb = request.state.pb
    metric = KeywordMetricRepo(pb).get(metric_id)
    if metric is None or metric.get("run") != run_id:
        return error_response("Keyword not found.")
    keyword_row = KeywordRepo(pb).get(metric.get("keyword") or "") or {}
    text = keyword_row.get("displayKeyword") or keyword_row.get("normalizedKeyword") or ""
    from app.providers.registry import ProviderRegistry
    from app.services.settings import ProjectConfig

    config = ProjectConfig.load(pb, project_id)
    provider = ProviderRegistry(pb).get_serp_provider(config.project, config.settings)
    targeting = dict(run.get("targeting") or {})
    status, _ = await refresh_keyword(
        pb,
        provider,
        project_id=project_id,
        keyword=text,
        locale=safe_str(targeting.get("locale")),
        location=safe_str(targeting.get("locationName")),
        run_id=run_id,
        force=True,
    )
    if status == "unavailable":
        return error_response(
            "SERP research is not configured — keyword and competitor research continue "
            "to work without it."
        )
    return success_response("SERP observation refreshed.")


@router.post("/projects/{project_id}/research/{run_id}/opportunities/{idea_id}/action")
@hx_error("Could not update that opportunity.")
def opportunity_action(
    request: Request,
    project_id: str,
    run_id: str,
    idea_id: str,
    action: str = Form(""),
):
    """Accept / roadmap / reject an opportunity — the handoff into Articles."""
    require_hx(request)
    require_project_access(request, project_id)
    require_project_role(request, project_id)
    run = _run_or_none(request, project_id, run_id)
    if run is None:
        return error_response("Research run not found.")
    action = safe_str(action).lower()
    if action not in ACTIONS:
        return error_response("Unknown action.")
    ideas = ArticleIdeaRepo(request.state.pb)
    idea = ideas.get(idea_id)
    if idea is None or idea.get("run") != run_id:
        return error_response("Opportunity not found.")

    if action == "reject":
        ideas.update(idea_id, {"status": "rejected"})
        return success_response("Opportunity rejected.", extra_events={"refreshResearch": True})

    if action == "roadmap":
        ideas.update(idea_id, {"status": "roadmap"})
        return success_response("Added to the roadmap.", extra_events={"refreshResearch": True})

    from app.services.research_handoff import accept_opportunity

    article = accept_opportunity(request.state.pb, idea=idea, config={"project": project_id})
    ideas.update(idea_id, {"status": "accepted", "article": article["id"]})
    return success_response(
        f"Draft queued in Articles: {article.get('title')}",
        extra_events={"refreshResearch": True},
    )


@router.get("/projects/{project_id}/research/{run_id}/opportunities.csv")
@page_guard("Could not export the research roadmap.")
def opportunities_export(request: Request, project_id: str, run_id: str):
    require_project_access(request, project_id)
    run = _run_or_none(request, project_id, run_id)
    if run is None:
        return RedirectResponse(f"/projects/{project_id}?tab=research", status_code=303)
    rows = ArticleIdeaRepo(request.state.pb).all_for_run(run_id)

    def generate() -> Any:
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow(
            [
                "title",
                "primary_keyword",
                "secondary_keywords",
                "intent",
                "content_type",
                "action",
                "status",
                "opportunity_score",
                "search_volume",
                "google_ads_competition",
                "confidence",
                "cluster",
                "existing_url",
                "recommended_angle",
            ]
        )
        for row in rows:
            writer.writerow(
                [
                    row.get("title") or "",
                    row.get("primaryKeyword") or "",
                    json.dumps(row.get("secondaryKeywords") or [], ensure_ascii=False),
                    row.get("intent") or "",
                    row.get("contentType") or "",
                    row.get("action") or "",
                    row.get("status") or "",
                    row.get("opportunityScore") or 0,
                    row.get("searchVolume") or 0,
                    row.get("googleAdsCompetition") or "",
                    row.get("confidence") or "",
                    row.get("cluster") or "",
                    row.get("canonicalExistingUrl") or "",
                    (row.get("recommendedAngle") or "").replace("\n", " "),
                ]
            )
            yield buffer.getvalue()
            buffer.seek(0)
            buffer.truncate(0)

    return StreamingResponse(
        generate(),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="opportunities-{run_id}.csv"'},
    )
