"""Projects — CRUD, tabs (settings/integrations/prompts/topics/articles/jobs/…), actions.

Routers only parse HTTP/HTMX and build responses; data access goes through
repositories, job creation through JobRepo.
"""

from __future__ import annotations

import base64
import re
import time
from typing import Any

from fastapi import APIRouter, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse
from starlette.datastructures import UploadFile as StarletteUploadFile

from app.api.deps import (
    PROJECT_ADMIN_ROLES,
    current_user,
    ensure_record_in_project,
    is_hx_request,
    project_scope,
    require_hx,
    require_project_access,
    require_project_role,
    require_user,
    safe_bool,
    safe_float,
    safe_int,
    safe_str,
)
from app.api.errors import hx_error
from app.domain.validation import normalize_base_url
from app.repositories.articles import ArticleRepo
from app.repositories.indexing import DocumentRepo, IndexRunRepo
from app.repositories.integrations import IntegrationRepo
from app.repositories.jobs import JobEventRepo, JobRepo
from app.repositories.members import MemberRepo
from app.repositories.projects import ProjectRepo, ProjectSettingsRepo
from app.repositories.prompts import PromptRepo
from app.repositories.publishing_runs import PublishingRunRepo
from app.repositories.schedules import ScheduleRepo
from app.repositories.topics import TopicRepo
from app.services.prompt_service import PromptService
from app.services.secrets import SecretsService, get_secrets_service
from app.services.topic_import import (
    IMPORT_FIELDS,
    build_column_map,
    detect_columns,
    detect_header,
    field_labels,
    get_preview,
    import_rows,
    parse_delimited,
    store_preview,
)
from app.templates import templates
from app.utils import error_response, hx_trigger, ok_with_redirect, success_response

# Targeting options for the research tab. A short, common list is enough — the
# Google Ads geo/language ids are resolved from these codes at collection time.
RESEARCH_COUNTRIES = [
    ("US", "United States"),
    ("GB", "United Kingdom"),
    ("CA", "Canada"),
    ("AU", "Australia"),
    ("DE", "Germany"),
    ("FR", "France"),
    ("ES", "Spain"),
    ("IT", "Italy"),
    ("NL", "Netherlands"),
    ("BR", "Brazil"),
    ("IN", "India"),
    ("TR", "Türkiye"),
    ("AE", "United Arab Emirates"),
    ("SA", "Saudi Arabia"),
]
RESEARCH_LANGUAGES = [
    ("en", "English"),
    ("de", "German"),
    ("fr", "French"),
    ("es", "Spanish"),
    ("it", "Italian"),
    ("nl", "Dutch"),
    ("pt", "Portuguese"),
    ("tr", "Turkish"),
    ("ar", "Arabic"),
    ("fa", "Persian"),
]

router = APIRouter()

TABS = [
    # Ordered as the daily operator flow: configure → research → write →
    # publish → index → monitor. The tab bar is a workflow trail, not an
    # alphabetical list.
    "settings",
    "integrations",
    "ai_models",
    "prompts",
    "research",
    "topics",
    "articles",
    "images",
    "publishing",
    "retrieval",
    "indexing",
    "jobs",
    "logs",
]


def integration_categories() -> dict[str, str]:
    """Lazy (request-time) translations — call this, never index at import time."""
    return {
        "llm": ("Language model (LLM)"),
        "embedding": ("Text embedding"),
        "reranker": ("Reranking"),
        "vector_store": ("Vector store (Qdrant)"),
        "publisher": ("Publishing (WordPress)"),
        "image": ("Image generation (AI Image)"),
        "serp": ("SERP data (optional)"),
        "google_ads": ("Google Ads (keyword research)"),
    }


def health_labels() -> dict[str, str]:
    return {
        "unknown": ("Unknown"),
        "healthy": ("Healthy"),
        "degraded": ("Degraded"),
        "unhealthy": ("Unhealthy"),
    }


# ---------------------------------------------------------------------------
# List & create
# ---------------------------------------------------------------------------
@router.get("/projects", response_class=HTMLResponse)
def projects_list(request: Request):
    """List the projects the current user can see (admins: all active)."""
    scope = project_scope(request)
    if scope is None:
        projects = ProjectRepo(request.state.pb).list_active()
    elif scope:
        projects = ProjectRepo(request.state.pb).list_records(
            filter="(" + " || ".join(f'id="{s}"' for s in scope) + ') && status="active"',
            sort="name",
        )
    else:
        projects = []
    return templates.TemplateResponse(
        request, "pages/projects/list.html", {"title": ("Projects"), "projects": projects}
    )


@router.post("/projects")
@hx_error("Creating project failed")
def create_project(
    request: Request,
    name: str = Form(""),
    slug: str = Form(""),
    language: str = Form("en"),
    timezone: str = Form("Asia/Tehran"),
    description: str = Form(""),
):
    require_hx(request)
    user = require_user(request)
    slug_value = slug.strip().lower() or None
    if not slug_value:
        slug_value = (
            re.sub(r"[^a-z0-9-]+", "-", name.strip().lower()).strip("-")
            or f"project-{int(time.time())}"
        )
    project = ProjectRepo(request.state.pb).create(
        name=safe_str(name),
        slug=slug_value,
        language=safe_str(language, "en"),
        timezone=safe_str(timezone, "Asia/Tehran"),
        description=safe_str(description),
        created_by=user.get("id", ""),
    )
    # Creator becomes owner of the project (authorization).
    MemberRepo(request.state.pb).add(project=project["id"], user=user.get("id", ""), role="owner")
    return ok_with_redirect(("Project created"), f"/projects/{project['id']}")


@router.post("/projects/{project_id}/delete")
@hx_error("Deleting project failed")
def delete_project(request: Request, project_id: str):
    require_hx(request)
    require_project_access(request, project_id)
    require_project_role(request, project_id, PROJECT_ADMIN_ROLES)
    ProjectRepo(request.state.pb).delete(project_id)
    return ok_with_redirect(("Project deleted"), "/projects", type="info")


# ---------------------------------------------------------------------------
# Project status (live active/inactive toggle)
# ---------------------------------------------------------------------------
@router.get("/projects/{project_id}/status-toggle", response_class=HTMLResponse)
def project_status_toggle(request: Request, project_id: str):
    """Fragment — the header's live status toggle (refreshed via refreshProjectStatus)."""
    project = require_project_access(request, project_id)
    return templates.TemplateResponse(
        request,
        "pages/projects/_status_toggle.html",
        {"project": project, "project_id": project_id},
    )


@router.post("/projects/{project_id}/toggle-status")
@hx_error("Toggling project failed")
def toggle_project_status(request: Request, project_id: str):
    """Flip the project between active and inactive (live)."""
    require_hx(request)
    require_project_access(request, project_id)
    require_project_role(request, project_id, PROJECT_ADMIN_ROLES)
    project = ProjectRepo(request.state.pb).get(project_id)
    if not project:
        return error_response("Project not found")
    new_status = "inactive" if project.get("status") == "active" else "active"
    ProjectRepo(request.state.pb).set_status(project_id, new_status)
    message = ("Project activated") if new_status == "active" else ("Project deactivated")
    return success_response(message, extra_events={"refreshProjectStatus": True})


# ---------------------------------------------------------------------------
# Topics workspace (full page)
# ---------------------------------------------------------------------------
TOPIC_SORTS = {
    "priority": "-priority,-created",
    "priority_asc": "priority,created",
    "created": "-created",
    "title": "title",
}


@router.get("/projects/{project_id}/topics", response_class=HTMLResponse)
def topics_page(
    request: Request,
    project_id: str,
    status: str = "",
    q: str = "",
    sort: str = "priority",
    page: int = 1,
):
    project = require_project_access(request, project_id)
    page = max(1, page)
    per_page = 20
    rows, total = TopicRepo(request.state.pb).search(
        project_id,
        status=status or None,
        q=q,
        sort=TOPIC_SORTS.get(sort, TOPIC_SORTS["priority"]),
        page=page,
        per_page=per_page,
    )
    statuses = [
        "planned",
        "queued",
        "planning",
        "outline_ready",
        "writing",
        "review",
        "publishing",
        "published",
        "failed",
        "cancelled",
    ]
    counts = {s: TopicRepo(request.state.pb).count_by_status(project_id, s) for s in statuses}
    return templates.TemplateResponse(
        request,
        "pages/topics/index.html",
        {
            "title": f"Topics — {project.get('name', '')}",
            "project": project,
            "topics": rows,
            "total": total,
            "page": page,
            "per_page": per_page,
            "pages": max(1, -(-total // per_page)),
            "status_filter": status,
            "q": q,
            "sort": sort,
            "counts": counts,
            "statuses": statuses,
        },
    )


@router.post("/projects/{project_id}/topics/bulk")
@hx_error("Bulk operation failed")
def topics_bulk(
    request: Request, project_id: str, action: str = Form(""), topic_ids: str = Form("")
):
    """Bulk actions: generate | retry | cancel (comma-separated topic ids)."""
    require_hx(request)
    require_project_access(request, project_id)
    require_project_role(request, project_id)
    ids = [i.strip() for i in topic_ids.split(",") if i.strip()]
    if not ids:
        return error_response("No topic selected")
    repo = TopicRepo(request.state.pb)
    jobs = JobRepo(request.state.pb)
    done = 0
    for topic_id in ids:
        topic = repo.get(topic_id)
        if not topic or topic.get("project") != project_id:
            continue
        if action in ("generate", "retry") and topic.get("status") in (
            "planned",
            "failed",
            "cancelled",
        ):
            repo.set_status(topic_id, "queued")
            jobs.create(
                project=project_id,
                type="write_article",
                payload={"topicId": topic_id},
                idempotency_key=f"write:article:{topic_id}",
                max_attempts=3,
                entity_type="topic",
                entity_id=topic_id,
                priority=int(topic.get("priority") or 0),
            )
            done += 1
        elif action == "cancel" and topic.get("status") in (
            "planned",
            "queued",
            "planning",
            "outline_ready",
            "writing",
        ):
            repo.set_status(topic_id, "cancelled")
            done += 1
    return success_response(
        (f"{done} topic updated" if done == 1 else f"{done} topics updated"),
        extra_events={"refreshTopics": True, "refreshJobs": True},
    )


@router.post("/projects/{project_id}/topics/{topic_id}/cancel")
@hx_error("Cancelling topic failed")
def cancel_topic(request: Request, project_id: str, topic_id: str):
    require_hx(request)
    require_project_access(request, project_id)
    require_project_role(request, project_id)
    topic = TopicRepo(request.state.pb).get(topic_id)
    if not topic or topic.get("project") != project_id:
        return error_response("Topic not found")
    TopicRepo(request.state.pb).set_status(topic_id, "cancelled")
    return success_response(("Topic cancelled"), extra_events={"refreshTopics": True})


# ---------------------------------------------------------------------------
# Project page + tabs
# ---------------------------------------------------------------------------
def _tab_context(
    pb: Any,
    project: dict[str, Any],
    tab: str,
    *,
    status: str = "",
    q: str = "",
    user: dict[str, Any] | None = None,
    ga: str = "",
) -> dict[str, Any]:
    """Build the template context a project tab needs.

    Shared by the full-page render (``detail.html`` includes the active tab
    partial inline) and the HTMX tab fragment. Without this a direct load of
    ``/projects/{id}`` 500s because the included tab template has no variables
    (e.g. ``settings`` is undefined) — the crash fixed here.
    """
    project_id = project["id"]
    context: dict[str, Any] = {"project": project}

    if tab == "settings":
        from app.providers.registry import ProviderRegistry

        registry = ProviderRegistry(pb)
        context["settings"] = ProjectSettingsRepo(pb).get_for_project(project_id)
        context["schedules"] = ScheduleRepo(pb).list_for_project(project_id)
        context["llm_providers"] = registry.available_providers("llm")
        context["embedding_providers"] = registry.available_providers("embedding")
        context["reranker_providers"] = registry.available_providers("reranker")
    elif tab == "integrations":
        from app.providers.registry import ProviderRegistry

        registry = ProviderRegistry(pb)
        context["integrations"] = IntegrationRepo(pb).list_for_project(project_id)
        context["categories"] = integration_categories()
        context["health_labels"] = health_labels()
        context["known_providers"] = {
            cat: registry.available_providers(cat) for cat in integration_categories()
        }
    elif tab == "prompts":
        from app.domain.prompt_render import variable_labels

        context["prompt_types"] = {
            "brand_voice": ("Brand voice"),
            "seo_content_contract": ("SEO content contract"),
            "research_system": ("Research — system"),
            "research_user": ("Research — user"),
            "outline_system": ("Outline — system"),
            "outline_user": ("Outline — user"),
            "section_system": ("Section — system"),
            "section_user": ("Section — user"),
            "internal_linking": ("Internal linking"),
            "metadata_system": ("Metadata — system"),
            "metadata_user": ("Metadata — user"),
            "article_qa_system": ("Article QA — system"),
            "article_qa_user": ("Article QA — user"),
            "article_repair_system": ("Article repair — system"),
            "article_repair_user": ("Article repair — user"),
            "image_plan_system": ("Image plan — system"),
            "image_plan_user": ("Image plan — user"),
            "output_validation": ("Output validation"),
            "content_refresh_system": ("Content refresh"),
            # legacy aliases (kept resolving: seo_rules → contract, validation → repair)
            "seo_rules": ("SEO rules (legacy)"),
            "validation": ("Article validation (legacy)"),
        }
        repo = PromptRepo(pb)
        context["prompts"] = repo.list_for_project(project_id)
        context["history"] = {
            ptype: repo.history(project_id, ptype, limit=20) for ptype in context["prompt_types"]
        }
        context["topics"] = TopicRepo(pb).list_for_project(project_id, per_page=50)
        context["variables"] = variable_labels()
    elif tab == "topics":
        topic_repo = TopicRepo(pb)
        rows, _total = topic_repo.search(project_id, status=status or None, q=q, per_page=50)
        context["topics"] = rows
        context["status_filter"] = status or ""
        context["q"] = q
    elif tab == "articles":
        context["articles"] = ArticleRepo(pb).list_for_project(project_id, per_page=50)
    elif tab == "ai_models":
        from app.providers.registry import ProviderRegistry
        from app.repositories.app_settings import AppSettingsRepo

        registry = ProviderRegistry(pb)
        context["settings"] = ProjectSettingsRepo(pb).get_for_project(project_id)
        context["llm_meta"] = registry.provider_metadata("llm")
        context["global_defaults"] = AppSettingsRepo(pb).get_defaults().get("llm") or {}
        context["roles"] = [
            ("outline", ("Outline model"), True),
            ("section", ("Section writer model"), True),
            ("meta", ("Meta/SEO model (optional)"), False),
            ("review", ("Review model (optional)"), False),
        ]
    elif tab == "retrieval":
        context["settings"] = ProjectSettingsRepo(pb).get_for_project(project_id)
    elif tab == "images":
        from app.providers.registry import ProviderRegistry

        registry = ProviderRegistry(pb)
        context["settings"] = ProjectSettingsRepo(pb).get_for_project(project_id)
        providers = registry.available_providers("image")
        context["image_provider_options"] = [(p, p) for p in providers]
        context["image_fallback_options"] = [("", ("No fallback"))] + [(p, p) for p in providers]
    elif tab == "jobs":
        context["jobs"] = JobRepo(pb).list_for_project(project_id, per_page=30)
    elif tab == "indexing":
        from app.services.metrics import project_metrics, query_provider_metrics

        context["runs"] = IndexRunRepo(pb).list_for_project(project_id, per_page=10)
        context["documents"] = DocumentRepo(pb).list_for_project(project_id, per_page=10)
        context["indexed_count"] = DocumentRepo(pb).count_indexed(project_id)
        context["metrics"] = project_metrics(pb, project_id)
        context["provider_metrics"] = query_provider_metrics(pb, project_id=project_id, days=7)
    elif tab == "publishing":
        context["records"] = PublishingRunRepo(pb).list_for_project(project_id, per_page=30)
    elif tab == "logs":
        context["events"] = JobEventRepo(pb).list_for_project(project_id, per_page=40)
    elif tab == "research":
        from app.repositories.research import (
            GoogleAdsConnectionRepo,
            GoogleAdsCustomerRepo,
            ResearchRunRepo,
        )
        from app.services.google_ads import client_configured

        user = user or {}
        connections = GoogleAdsConnectionRepo(pb).for_user(user.get("id") or "")
        context["connections"] = connections
        context["customers"] = GoogleAdsCustomerRepo(pb).list_for_project(project_id)
        context["runs"] = ResearchRunRepo(pb).list_for_project(project_id, page=1, per_page=25)
        context["google_ads_configured"] = client_configured(pb, project_id)
        context["serp_configured"] = any(
            integration.get("category") == "serp" and integration.get("enabled")
            for integration in IntegrationRepo(pb).list_all(filter=f'project="{project_id}"')
        )
        context["countries"] = RESEARCH_COUNTRIES
        context["languages"] = RESEARCH_LANGUAGES
        context["ga"] = ga

    return context


@router.get("/projects/{project_id}", response_class=HTMLResponse)
def project_detail(request: Request, project_id: str, tab: str = "settings"):
    try:
        project = require_project_access(request, project_id)
    except (PermissionError, ValueError):
        return templates.TemplateResponse(
            request, "pages/projects/not_found.html", {"title": ("Project not found")}
        )
    active = tab if tab in TABS else "settings"
    context = _tab_context(
        request.state.pb,
        project,
        active,
        status=request.query_params.get("status") or "",
        q=request.query_params.get("q") or "",
        user=current_user(request),
        ga=request.query_params.get("ga") or "",
    )
    context["title"] = project.get("name", ("Project"))
    context["active_tab"] = active
    context["tabs"] = TABS
    return templates.TemplateResponse(request, "pages/projects/detail.html", context)


@router.get("/projects/{project_id}/tabs/{tab}", response_class=HTMLResponse)
def project_tab(request: Request, project_id: str, tab: str):
    try:
        project = require_project_access(request, project_id)
    except (PermissionError, ValueError):
        if is_hx_request(request):
            return error_response("Project not found or access denied")
        return templates.TemplateResponse(
            request, "pages/projects/not_found.html", {"title": ("Project not found")}
        )
    if tab not in TABS:
        return HTMLResponse("")
    context = _tab_context(
        request.state.pb,
        project,
        tab,
        status=request.query_params.get("status") or "",
        q=request.query_params.get("q") or "",
        user=current_user(request),
        ga=request.query_params.get("ga") or "",
    )
    return templates.TemplateResponse(request, f"pages/projects/tabs/{tab}.html", context)


# ---------------------------------------------------------------------------
# Settings (flat schema)
# ---------------------------------------------------------------------------
@router.post("/projects/{project_id}/settings")
@hx_error("Saving settings failed")
def save_settings(
    request: Request,
    project_id: str,
    default_llm_provider: str = Form(""),
    default_llm_model: str = Form(""),
    embedding_provider: str = Form(""),
    embedding_model: str = Form(""),
    embedding_dimensions: str = Form(""),
    chunk_size: str = Form(""),
    chunk_overlap: str = Form(""),
    separator_strategy: str = Form(""),
    max_chunk_count: str = Form(""),
    retrieval_top_k: str = Form(""),
    similarity_threshold: str = Form(""),
    reranking_enabled: str = Form(""),
    reranker_provider: str = Form(""),
    reranker_model: str = Form(""),
    reranker_top_n: str = Form(""),
    context_max_links: str = Form(""),
    context_max_passages: str = Form(""),
    context_max_chars: str = Form(""),
    generation_concurrency: str = Form(""),
    min_article_words: str = Form(""),
    retry_max_attempts: str = Form(""),
    retry_backoff_base: str = Form(""),
    retry_backoff_max: str = Form(""),
    publishing_mode: str = Form(""),
    autosave_enabled: str = Form(""),
    autosave_interval_minutes: str = Form(""),
    auto_publish_enabled: str = Form(""),
    auto_publish_min_score: str = Form(""),
    auto_publish_max_attempts: str = Form(""),
    schedule_index_enabled: str = Form(""),
    schedule_index_interval: str = Form(""),
    schedule_write_enabled: str = Form(""),
    schedule_write_interval: str = Form(""),
    target_locale: str = Form(""),
    target_country: str = Form(""),
    target_audience: str = Form(""),
    brand_name: str = Form(""),
    preferred_terminology: str = Form(""),
    forbidden_terminology: str = Form(""),
    url_policy: str = Form(""),
    product_context: str = Form(""),
):
    require_hx(request)
    require_project_access(request, project_id)
    require_project_role(request, project_id)
    data = {
        "defaultLlmProvider": safe_str(default_llm_provider, "openai_compat"),
        "defaultLlmModel": safe_str(default_llm_model, "gpt-4o-mini"),
        "embeddingProvider": safe_str(embedding_provider, "openai_compat"),
        "embeddingModel": safe_str(embedding_model, "text-embedding-3-small"),
        "embeddingDimensions": safe_int(embedding_dimensions, 1536),
        "chunkSize": safe_int(chunk_size, 500),
        "chunkOverlap": safe_int(chunk_overlap, 100),
        "separatorStrategy": safe_str(separator_strategy, "auto"),
        "maxChunkCount": safe_int(max_chunk_count, 0),
        "retrievalTopK": safe_int(retrieval_top_k, 8),
        "similarityThreshold": safe_float(similarity_threshold, 0.35),
        "rerankingEnabled": safe_bool(reranking_enabled),
        "rerankerProvider": safe_str(reranker_provider, "cohere_compat"),
        "rerankerModel": safe_str(reranker_model, "rerank-v4.0"),
        "rerankerTopN": safe_int(reranker_top_n, 8),
        "contextMaxLinks": safe_int(context_max_links, 5),
        "contextMaxPassages": safe_int(context_max_passages, 5),
        "contextMaxChars": safe_int(context_max_chars, 4000),
        "generationConcurrency": safe_int(generation_concurrency, 2),
        "minArticleWords": safe_int(min_article_words, 300),
        "retryPolicy": {
            "max_attempts": safe_int(retry_max_attempts, 3),
            "backoff_base": safe_int(retry_backoff_base, 30),
            "backoff_max": safe_int(retry_backoff_max, 3600),
        },
        "publishingMode": safe_str(publishing_mode, "draft"),
        "targetLocale": safe_str(target_locale),
        "targetCountry": safe_str(target_country),
        "targetAudience": safe_str(target_audience),
        "brandName": safe_str(brand_name),
        "preferredTerminology": safe_str(preferred_terminology),
        "forbiddenTerminology": safe_str(forbidden_terminology),
        "urlPolicy": safe_str(url_policy),
        "productContext": safe_str(product_context),
        "autosave": {
            "enabled": safe_bool(autosave_enabled),
            "interval_minutes": safe_int(autosave_interval_minutes, 5),
        },
        "autoPublish": {
            "enabled": safe_bool(auto_publish_enabled),
            "min_score": max(0, safe_int(auto_publish_min_score, 90)),
            "max_attempts": max(1, safe_int(auto_publish_max_attempts, 3)),
        },
    }
    ProjectSettingsRepo(request.state.pb).upsert(project_id, data)
    # Schedules live in the SAME settings form (one save for the whole page):
    # persist them with the same submission.
    _persist_schedule(
        request.state.pb, project_id, "index", schedule_index_enabled, schedule_index_interval
    )
    _persist_schedule(
        request.state.pb, project_id, "write", schedule_write_enabled, schedule_write_interval
    )
    return success_response("Settings saved")


@router.post("/projects/{project_id}/settings/images")
@hx_error("Saving image settings failed")
def save_image_settings(
    request: Request,
    project_id: str,
    image_cover_provider: str = Form(""),
    image_cover_model: str = Form(""),
    image_interior_provider: str = Form(""),
    image_interior_model: str = Form(""),
    image_fallback_provider: str = Form(""),
    image_fallback_model: str = Form(""),
    image_cover_aspect_ratio: str = Form(""),
    image_interior_aspect_ratio: str = Form(""),
    image_cover_min_width: str = Form(""),
    image_max_interior_images: str = Form(""),
    image_max_retries: str = Form(""),
    image_optimization_format: str = Form(""),
    image_ai_qa_enabled: str = Form(""),
    image_prompt_language: str = Form(""),
    style_tone: str = Form(""),
    style_palette: str = Form(""),
    style_lighting: str = Form(""),
    style_negative: str = Form(""),
    style_prohibited: str = Form(""),
):
    """Project-level image config: providers/models per role, fallback, density,
    style profile. Model IDs are data — the pipeline never hardcodes them."""
    require_hx(request)
    require_project_access(request, project_id)
    require_project_role(request, project_id)
    style: dict[str, str] = {}
    for key, raw in (
        ("tone", style_tone),
        ("palette", style_palette),
        ("lighting", style_lighting),
        ("negative_prompt", style_negative),
        ("prohibited", style_prohibited),
    ):
        value = safe_str(raw)
        if value:
            style[key] = value
    data = {
        "imageCoverProvider": safe_str(image_cover_provider, "gemini"),
        "imageCoverModel": safe_str(image_cover_model, "gemini-3-pro-image"),
        "imageInteriorProvider": safe_str(image_interior_provider, "bfl"),
        "imageInteriorModel": safe_str(image_interior_model, "flux-2-klein-9b"),
        "imageFallbackProvider": safe_str(image_fallback_provider),
        "imageFallbackModel": safe_str(image_fallback_model),
        "imageCoverAspectRatio": safe_str(image_cover_aspect_ratio, "16:9"),
        "imageInteriorAspectRatio": safe_str(image_interior_aspect_ratio, "16:9"),
        "imageCoverMinWidth": max(0, safe_int(image_cover_min_width, 1200)),
        "imageMaxInteriorImages": max(0, safe_int(image_max_interior_images, 4)),
        "imageMaxRetries": max(1, safe_int(image_max_retries, 3)),
        "imageOptimizationFormat": safe_str(image_optimization_format, "webp"),
        "imageAiQaEnabled": safe_bool(image_ai_qa_enabled),
        "imagePromptLanguage": safe_str(image_prompt_language, "en"),
        "imageStyle": style,
    }
    ProjectSettingsRepo(request.state.pb).upsert(project_id, data)
    return success_response(
        ("Image settings saved"),
        extra_events={"refreshArticle": True},
    )


_TEST_IMAGE_PROMPT = (
    "A simple flat test illustration: a single red circle centered on a plain white background"
)


@router.post("/projects/{project_id}/settings/images/test")
@hx_error("Image generation test failed")
async def test_image_generation(request: Request, project_id: str):
    """One-off diagnostic: generate a tiny fixed-prompt image with the SAVED
    cover provider/model and show the result inline (like test_integration).
    Uses the article pipeline's provider resolution — configuration problems
    surface here, not on a real article."""
    require_hx(request)
    require_project_access(request, project_id)
    require_project_role(request, project_id)
    project = require_project_access(request, project_id)
    settings = ProjectSettingsRepo(request.state.pb).get_for_project(project_id)
    provider_name = safe_str(settings.get("imageCoverProvider") or "gemini")
    model = safe_str(settings.get("imageCoverModel") or "")
    if not model:
        return templates.TemplateResponse(
            request,
            "pages/projects/tabs/_image_test_result.html",
            {"error": ("Cover image model is not set — save the model first")},
        )
    from app.domain.images import classify_error, dims_for_aspect
    from app.providers.base import ImageRequest, ProviderError
    from app.providers.registry import ProviderRegistry

    # Replicate the REAL cover request (same dims the article pipeline sends) —
    # some gateways reject sizes the pipeline would never use (e.g. square on
    # AvalAI's gemini-3-pro-image).
    width, height = dims_for_aspect(
        safe_str(settings.get("imageCoverAspectRatio"), "16:9") or "16:9",
        role="cover",
        min_width=safe_int(settings.get("imageCoverMinWidth"), 1200),
    )
    try:
        provider = ProviderRegistry(request.state.pb).get_image_provider(
            project, settings, role_config={"provider": provider_name, "model": model}
        )
        started = time.monotonic()
        result = await provider.generate_image(
            ImageRequest(
                prompt=_TEST_IMAGE_PROMPT,
                width=width,
                height=height,
                aspect_ratio=safe_str(settings.get("imageCoverAspectRatio"), "16:9") or "16:9",
            )
        )
        await provider.aclose()
    except ProviderError as exc:
        category = str((exc.details or {}).get("category") or classify_error(str(exc)))
        return templates.TemplateResponse(
            request,
            "pages/projects/tabs/_image_test_result.html",
            {"error": str(exc), "category": category},
        )
    # Gateways that pick their own output size report 0x0 — read actual dims.
    out_w, out_h = result.width, result.height
    if not (out_w and out_h):
        import io

        from PIL import Image

        with Image.open(io.BytesIO(result.data)) as img:
            out_w, out_h = img.size
    return templates.TemplateResponse(
        request,
        "pages/projects/tabs/_image_test_result.html",
        {
            "data_uri": f"data:{result.mime_type};base64,"
            + base64.b64encode(result.data).decode("ascii"),
            "provider": provider_name,
            "model": model,
            "bytes": len(result.data),
            "latency": int((time.monotonic() - started) * 1000),
            "width": out_w,
            "height": out_h,
        },
    )


def _persist_schedule(pb: Any, project_id: str, kind: str, enabled: str, interval: str) -> None:
    """Upsert one schedule row (index/write) from settings-form fields."""
    repo = ScheduleRepo(pb)
    existing = repo.first(filter=f'project="{project_id}" && kind="{kind}"')
    interval_minutes = max(1, safe_int(interval, 1440))
    enabled_bool = safe_bool(enabled)
    if existing:
        repo.update(existing["id"], {"enabled": enabled_bool, "intervalMinutes": interval_minutes})
    else:
        repo.create(
            project=project_id,
            name=kind,
            kind=kind,
            interval_minutes=interval_minutes,
            payload={},
            enabled=enabled_bool,
        )


# ---------------------------------------------------------------------------
# Schedules
# ---------------------------------------------------------------------------
_SCHEDULE_KINDS = {"index": ("Auto-indexing"), "write": ("Auto-writing")}


@router.post("/projects/{project_id}/schedules")
@hx_error("Saving schedule failed")
def save_schedules(
    request: Request,
    project_id: str,
    kind: str = Form(""),
    enabled: str = Form(""),
    interval_minutes: str = Form(""),
):
    require_hx(request)
    require_project_access(request, project_id)
    require_project_role(request, project_id)
    # A request without a known kind cannot be a schedule save — refuse instead
    # of silently writing a garbage row (which used to show a success toast).
    if kind not in _SCHEDULE_KINDS:
        return error_response("Invalid schedule type")
    schedule = ScheduleRepo(request.state.pb)
    existing = schedule.first(filter=f'project="{project_id}" && kind="{kind}"')
    interval = max(1, safe_int(interval_minutes, 1440))
    payload = {
        "enabled": safe_bool(enabled),
        "intervalMinutes": interval,
    }
    if existing:
        schedule.update(existing["id"], payload)
    else:
        schedule.create(
            project=project_id,
            name=kind,
            kind=kind,
            interval_minutes=interval,
            payload={},
            enabled=safe_bool(enabled),
        )
    # Re-render the row from the DATABASE so the saved status/interval are
    # visible immediately (swap target #sched-{kind}).
    sched = schedule.first(filter=f'project="{project_id}" && kind="{kind}"')
    resp = templates.TemplateResponse(
        request,
        "components/schedule_panel.html",
        {
            "project": {"id": project_id},
            "kind": kind,
            "label_text": _SCHEDULE_KINDS[kind],
            "sched": sched,
        },
    )
    resp.headers.update(
        hx_trigger({"show-toast": {"message": ("Schedule saved"), "type": "success"}})
    )
    return resp


# ---------------------------------------------------------------------------
# Integrations (configurable; secrets encrypted, never rendered plaintext)
# ---------------------------------------------------------------------------
@router.post("/projects/{project_id}/integrations")
@hx_error("Saving connection failed")
def save_integration(
    request: Request,
    project_id: str,
    record_id: str = Form(""),
    category: str = Form(""),
    provider: str = Form(""),
    display_name: str = Form(""),
    base_url: str = Form(""),
    model: str = Form(""),
    username: str = Form(""),
    secret: str = Form(""),
    client_secret: str = Form(""),
    client_id: str = Form(""),
    redirect_uri: str = Form(""),
    api_version: str = Form(""),
    login_customer_id: str = Form(""),
    developer_token: str = Form(""),
):
    require_hx(request)
    require_project_access(request, project_id)
    require_project_role(request, project_id)
    user = require_user(request)
    if category not in integration_categories():
        return error_response("Invalid category")
    secrets: SecretsService = get_secrets_service()
    repo = IntegrationRepo(request.state.pb)

    existing = repo.get(record_id) if record_id else None
    if existing:
        ensure_record_in_project(existing, project_id, "integration")

    import json

    existing_cfg: dict[str, Any] = (existing.get("configuration") or {}) if existing else {}

    if category == "google_ads":
        # Google Ads stores the OAuth client: public fields in `configuration`,
        # the client secret (+ optional developer token) encrypted in `secretsEnc`.
        if existing and not client_secret:
            ga_secrets_enc = existing.get("secretsEnc") or ""
        else:
            ga_secrets_enc = secrets.encrypt(
                json.dumps(
                    {
                        "client_secret": client_secret,
                        "developer_token": safe_str(developer_token),
                    },
                    ensure_ascii=False,
                )
            )
        ga_configuration: dict[str, Any] = {
            "client_id": safe_str(client_id),
            "redirect_uri": safe_str(redirect_uri),
            "api_version": safe_str(api_version) or "v25",
            "login_customer_id": safe_str(login_customer_id),
            "masked": existing_cfg.get("masked")
            if existing and not client_secret
            else (secrets.mask(client_secret) if client_secret else ""),
        }
        ga_payload = {
            "category": category,
            "provider": "google_ads",
            "displayName": safe_str(display_name) or "Google Ads",
            "configuration": ga_configuration,
            "secretsEnc": ga_secrets_enc,
            "enabled": bool(existing.get("enabled")) if existing else True,
            "createdBy": user.get("id", ""),
        }
        if existing:
            repo.update(existing["id"], ga_payload)
        else:
            repo.create(
                project=project_id,
                category=ga_payload["category"],
                provider=ga_payload["provider"],
                display_name=ga_payload["displayName"],
                configuration=ga_payload["configuration"],
                secrets_enc=ga_payload["secretsEnc"],
                enabled=ga_payload["enabled"],
                created_by=ga_payload["createdBy"],
            )
        return success_response(("Connection saved"), extra_events={"refreshIntegrations": True})

    if existing and not secret:
        secrets_enc = existing.get("secretsEnc") or ""
    else:
        secrets_enc = secrets.encrypt(json.dumps({"api_key": secret}, ensure_ascii=False))

    configuration: dict[str, Any] = {
        "base_url": normalize_base_url(safe_str(base_url)),
        "model": safe_str(model),
        # masked preview — the browser NEVER receives the real secret
        "masked": (existing.get("configuration") or {}).get("masked")
        if existing and not secret
        else (secrets.mask(secret) if secret else ""),
    }
    if category == "publisher":
        configuration["username"] = safe_str(username)

    payload = {
        "category": category,
        "provider": safe_str(provider) or "openai_compat",
        "displayName": safe_str(display_name) or category,
        "configuration": configuration,
        "secretsEnc": secrets_enc,
        "enabled": bool(existing.get("enabled")) if existing else True,
        "createdBy": user.get("id", ""),
    }
    if existing:
        repo.update(existing["id"], payload)
    else:
        repo.create(
            project=project_id,
            category=payload["category"],
            provider=payload["provider"],
            display_name=payload["displayName"],
            configuration=payload["configuration"],
            secrets_enc=payload["secretsEnc"],
            enabled=payload["enabled"],
            created_by=payload["createdBy"],
        )
    return success_response(("Connection saved"), extra_events={"refreshIntegrations": True})


@router.post("/projects/{project_id}/integrations/{record_id}/toggle")
@hx_error("Toggling failed")
def toggle_integration(request: Request, project_id: str, record_id: str):
    require_hx(request)
    require_project_access(request, project_id)
    require_project_role(request, project_id)
    repo = IntegrationRepo(request.state.pb)
    integration = repo.get(record_id)
    if not integration:
        return error_response("Connection not found")
    ensure_record_in_project(integration, project_id, "integration")
    repo.set_enabled(record_id, not integration.get("enabled", False))
    return success_response(
        ("Connection status changed"), extra_events={"refreshIntegrations": True}
    )


@router.post("/projects/{project_id}/integrations/{record_id}/test")
@hx_error("Connection test failed")
async def test_integration(request: Request, project_id: str, record_id: str):
    require_hx(request)
    require_project_access(request, project_id)
    repo = IntegrationRepo(request.state.pb)
    integration = repo.get(record_id)
    if not integration or integration.get("project") != project_id:
        return error_response("Connection not found")
    from app.providers.registry import ProviderRegistry

    result = await ProviderRegistry(request.state.pb).test_integration(
        require_project_access(request, project_id), integration
    )
    return templates.TemplateResponse(
        request,
        "pages/projects/tabs/integration_health.html",
        {"integration": integration, "result": result},
    )


@router.get("/projects/{project_id}/integrations/{record_id}/models")
async def integration_models(request: Request, project_id: str, record_id: str):
    """Model discovery for a saved connection — returns datalist options.

    Only works for an existing connection because the stored (encrypted) API key
    is what the provider is queried with; a brand-new connection must be saved
    first. Returns an empty datalist when the provider can't list models, so the
    field stays fully typeable.
    """
    try:
        project = require_project_access(request, project_id)
        if not record_id:
            return HTMLResponse("")
        integration = IntegrationRepo(request.state.pb).get(record_id)
        if not integration or integration.get("project") != project_id:
            return HTMLResponse("")
        from app.providers.registry import ProviderRegistry

        models = await ProviderRegistry(request.state.pb).list_models(project, integration)
        return templates.TemplateResponse(
            request,
            "pages/projects/tabs/model_options.html",
            {"models": models},
        )
    except Exception:
        return HTMLResponse("")


@router.post("/projects/{project_id}/integrations/{record_id}/delete")
@hx_error("Deleting connection failed")
def delete_integration(request: Request, project_id: str, record_id: str):
    require_hx(request)
    require_project_access(request, project_id)
    require_project_role(request, project_id)
    integration = IntegrationRepo(request.state.pb).get(record_id)
    ensure_record_in_project(integration, project_id, "integration")
    IntegrationRepo(request.state.pb).delete(record_id)
    return success_response(("Connection deleted"), extra_events={"refreshIntegrations": True})


# ---------------------------------------------------------------------------
# Prompts (versioned)
# ---------------------------------------------------------------------------
@router.post("/projects/{project_id}/prompts")
def save_prompts(
    request: Request,
    project_id: str,
    brand_voice: str = Form(""),
    seo_content_contract: str = Form(""),
    research_system: str = Form(""),
    research_user: str = Form(""),
    outline_system: str = Form(""),
    outline_user: str = Form(""),
    section_system: str = Form(""),
    section_user: str = Form(""),
    internal_linking: str = Form(""),
    metadata_system: str = Form(""),
    metadata_user: str = Form(""),
    article_qa_system: str = Form(""),
    article_qa_user: str = Form(""),
    article_repair_system: str = Form(""),
    article_repair_user: str = Form(""),
    image_plan_system: str = Form(""),
    image_plan_user: str = Form(""),
    output_validation: str = Form(""),
    content_refresh_system: str = Form(""),
    seo_rules: str = Form(""),
    validation: str = Form(""),
):
    require_hx(request)
    require_project_access(request, project_id)
    require_project_role(request, project_id)
    user = require_user(request)
    values = {
        "brand_voice": brand_voice,
        "seo_content_contract": seo_content_contract,
        "research_system": research_system,
        "research_user": research_user,
        "outline_system": outline_system,
        "outline_user": outline_user,
        "section_system": section_system,
        "section_user": section_user,
        "internal_linking": internal_linking,
        "metadata_system": metadata_system,
        "metadata_user": metadata_user,
        "article_qa_system": article_qa_system,
        "article_qa_user": article_qa_user,
        "article_repair_system": article_repair_system,
        "article_repair_user": article_repair_user,
        "image_plan_system": image_plan_system,
        "image_plan_user": image_plan_user,
        "output_validation": output_validation,
        "content_refresh_system": content_refresh_system,
        "seo_rules": seo_rules,
        "validation": validation,
    }
    from app.domain.prompt_render import PromptRenderError
    from app.services.prompt_service import PromptService

    service = PromptService(request.state.pb)
    try:
        for _ptype, content in values.items():
            unknown = service.validate_content(content)
            if unknown:
                raise PromptRenderError(unknown)
    except PromptRenderError as e:
        return error_response(
            ("Invalid variable(s): ") + ", ".join(e.unknown), extra_events={"refreshPrompts": True}
        )
    for ptype, content in values.items():
        service.save(project_id, ptype, content, author=user.get("id", ""))
    return success_response(("Prompts saved"), extra_events={"refreshPrompts": True})


# ---------------------------------------------------------------------------
# Prompt management (versioning, compare, tester)
# ---------------------------------------------------------------------------
@router.post("/projects/{project_id}/prompts/{ptype}/activate")
@hx_error("Activating version failed")
def activate_prompt_version(
    request: Request, project_id: str, ptype: str, version_id: str = Form("")
):
    require_hx(request)
    require_project_access(request, project_id)
    require_project_role(request, project_id)
    version = PromptRepo(request.state.pb).get(version_id)
    if version is None or version.get("type") != ptype:
        return error_response("Version not found")
    if str(version.get("project") or "") not in ("", project_id):
        return error_response("Version not found")
    PromptService(request.state.pb).activate(project_id, ptype, version_id)
    return success_response(("Version activated"), extra_events={"refreshPrompts": True})


@router.post("/projects/{project_id}/prompts/{ptype}/duplicate")
@hx_error("Duplication failed")
def duplicate_prompt_version(
    request: Request, project_id: str, ptype: str, version_id: str = Form("")
):
    require_hx(request)
    require_project_access(request, project_id)
    require_project_role(request, project_id)
    user = require_user(request)
    version = PromptRepo(request.state.pb).get(version_id)
    if version is None or version.get("type") != ptype:
        return error_response("Version not found")
    if str(version.get("project") or "") not in ("", project_id):
        return error_response("Version not found")
    PromptService(request.state.pb).duplicate(
        project_id, ptype, version_id, author=user.get("id", "")
    )
    return success_response(
        ("Version duplicated (inactive)"), extra_events={"refreshPrompts": True}
    )


@router.get("/projects/{project_id}/prompts/compare")
def compare_prompt_versions(
    request: Request, project_id: str, ptype: str = "", a: str = "", b: str = ""
):
    try:
        require_project_access(request, project_id)
        repo = PromptRepo(request.state.pb)
        va, vb = repo.get(a), repo.get(b)
        for version in (va, vb):
            if version is None:
                continue
            if str(version.get("project") or "") not in ("", project_id):
                return HTMLResponse("")
        return templates.TemplateResponse(
            request,
            "pages/projects/tabs/prompt_compare.html",
            {"va": va, "vb": vb, "ptype": ptype},
        )
    except Exception:
        return HTMLResponse("")


@router.post("/projects/{project_id}/prompts/test")
@hx_error("Prompt test failed")
async def test_prompt(
    request: Request,
    project_id: str,
    ptype: str = Form(""),
    content: str = Form(""),
    topic_id: str = Form(""),
    model_role: str = Form(""),
):
    """Interactive prompt tester — renders + runs WITHOUT touching stored versions."""
    require_hx(request)
    require_project_access(request, project_id)
    from app.services.settings import ProjectConfig

    config = ProjectConfig.load(request.state.pb, project_id)
    topic = None
    if topic_id:
        topic = TopicRepo(request.state.pb).get(topic_id)

    # live retrieval context for the tester (same subsystem the writer uses)
    retrieval_context, links = "", []
    if topic and ptype in ("outline_user", "outline_system", "section_user", "section_system"):
        from app.services.internal_linking import ContextBuilder
        from app.services.retrieval import RetrievalService

        service = RetrievalService(request.state.pb)
        from app.schemas.retrieval import RetrievalOptions

        options = RetrievalOptions(
            candidate_count=int(config.retrieval.get("top_k") or 20),
            similarity_threshold=float(config.retrieval.get("similarity_threshold") or 0.0) or None,
            rerank=bool(config.retrieval.get("rerank_enabled")),
            rerank_top_n=int(config.retrieval.get("rerank_top_n") or 8),
        )
        results = await service.retrieve(
            config, f"{topic.get('title') or ''} {topic.get('keyword') or ''}".strip(), options
        )
        builder = ContextBuilder(
            max_links=int(config.context.get("max_links") or 5),
            max_passages=int(config.context.get("max_passages") or 5),
            max_chars=int(config.context.get("max_chars") or 4000),
        )
        ctx = builder.build("", results, current_title=str(topic.get("title") or ""))
        retrieval_context = ctx.format_for_prompt()
        links = [link.to_dict() for link in ctx.links]

    result = await PromptService(request.state.pb).test(
        config,
        ptype=ptype,
        content=content,
        topic=topic,
        retrieval_context=retrieval_context,
        internal_links=links,
        model_role=model_role or ("outline" if ptype.startswith("outline") else "section"),
    )
    return templates.TemplateResponse(
        request,
        "pages/projects/tabs/prompt_test_result.html",
        {"result": result, "ptype": ptype},
    )


# ---------------------------------------------------------------------------
# Topics
# ---------------------------------------------------------------------------
@router.post("/projects/{project_id}/topics")
@hx_error("Adding topic failed")
def create_topic(
    request: Request,
    project_id: str,
    title: str = Form(""),
    keyword: str = Form(""),
    pillar: str = Form(""),
    cluster: str = Form(""),
    type: str = Form("article"),
    priority: str = Form(""),
    week: str = Form(""),
    url: str = Form(""),
):
    require_hx(request)
    require_project_access(request, project_id)
    require_project_role(request, project_id)
    if not safe_str(title):
        return error_response("Topic title is required")
    week_value = safe_int(week, 0)
    TopicRepo(request.state.pb).create(
        project=project_id,
        title=safe_str(title),
        keyword=safe_str(keyword),
        pillar=safe_str(pillar),
        cluster=safe_str(cluster),
        type=safe_str(type, "article"),
        priority=safe_int(priority, 0),
        week=week_value or None,
        url=safe_str(url),
    )
    return success_response(("Topic added"), extra_events={"refreshTopics": True})


@router.post("/projects/{project_id}/topics/{topic_id}/write")
@hx_error("Starting article generation failed")
def write_topic(request: Request, project_id: str, topic_id: str):
    require_hx(request)
    require_project_access(request, project_id)
    require_project_role(request, project_id)
    topic = TopicRepo(request.state.pb).get(topic_id)
    if not topic or topic.get("project") != project_id:
        return error_response("Topic not found")
    topic = TopicRepo(request.state.pb).set_status(topic_id, "queued")
    JobRepo(request.state.pb).create(
        project=project_id,
        type="write_article",
        payload={"topicId": topic_id},
        idempotency_key=f"write:article:{topic_id}",
        max_attempts=3,
        entity_type="topic",
        entity_id=topic_id,
        priority=int(topic.get("priority") or 0),
    )
    return success_response(
        ("Article generation started"), extra_events={"refreshTopics": True, "refreshJobs": True}
    )


@router.post("/projects/{project_id}/topics/{topic_id}/delete")
@hx_error("Deleting topic failed")
def delete_topic(request: Request, project_id: str, topic_id: str):
    require_hx(request)
    require_project_access(request, project_id)
    require_project_role(request, project_id)
    topic = TopicRepo(request.state.pb).get(topic_id)
    if not topic or topic.get("project") != project_id:
        return error_response("Topic not found")
    TopicRepo(request.state.pb).delete(topic_id)
    return success_response(("Topic deleted"), extra_events={"refreshTopics": True})


# ---------------------------------------------------------------------------
# Topics — bulk import (paste / CSV / TSV)
# ---------------------------------------------------------------------------
def _decode_csv_bytes(data: bytes) -> str:
    """Best-effort decode for uploaded CSV files (Excel-safe)."""
    for encoding in ("utf-8-sig", "utf-16", "cp1256"):
        try:
            return data.decode(encoding)
        except (UnicodeDecodeError, ValueError):
            continue
    return data.decode("utf-8", errors="replace")


@router.post("/projects/{project_id}/topics/import/preview")
@hx_error("Reading file or text failed")
async def topic_import_preview(
    request: Request,
    project_id: str,
    csv_text: str = Form(""),
    file: UploadFile | None = File(None),
):
    """Parse pasted text or an uploaded CSV and render the column-mapping form.

    The parsed rows are cached behind a one-time token so the confirm step
    doesn't re-transmit or re-parse the payload.
    """
    require_hx(request)
    require_project_access(request, project_id)
    require_project_role(request, project_id)

    filename = ""
    # Note: request.form() builds starlette UploadFile instances, while the
    # route annotation uses fastapi's UploadFile (a subclass) — isinstance
    # against starlette's class is what matches the bound object.
    if isinstance(file, StarletteUploadFile) and file.filename:
        filename = file.filename
        csv_text = _decode_csv_bytes(await file.read())
    csv_text = csv_text.strip()
    if not csv_text:
        return error_response("No text or file provided")

    delimiter, rows = parse_delimited(csv_text)
    if not rows:
        return error_response("No rows found")
    has_header = detect_header(rows)
    columns = detect_columns(rows, has_header)
    column_map = build_column_map(columns, has_header)
    token = store_preview(
        rows=rows,
        delimiter=delimiter,
        has_header=has_header,
        columns=columns,
        filename=filename,
    )
    data_preview = rows[1 : min(4, len(rows))] if has_header else rows[:3]
    return templates.TemplateResponse(
        request,
        "components/topics/import_mapping.html",
        {
            "token": token,
            "filename": filename,
            "delimiter": delimiter,
            "has_header": has_header,
            "columns": columns,
            "column_map": column_map,
            "fields": IMPORT_FIELDS,
            "field_labels": field_labels(),
            "row_count": len(rows) - (1 if has_header else 0),
            "data_preview": data_preview,
            "project": require_project_access(request, project_id),
        },
    )


@router.post("/projects/{project_id}/topics/import")
@hx_error("Bulk add failed")
async def topic_import(request: Request, project_id: str):
    """Confirm an import: create the cached rows using the chosen column map."""
    require_hx(request)
    require_project_access(request, project_id)
    require_project_role(request, project_id)

    form = await request.form()
    token = str(form.get("token", "") or "")
    entry = get_preview(token)
    if not entry:
        return error_response("Preview expired; please try again")

    mapping: dict[str, int] = {}
    for field in IMPORT_FIELDS:
        raw = str(form.get(f"col_{field}", "") or "")
        if raw.isdigit():
            mapping[field] = int(raw)
    if "title" not in mapping:
        return error_response("Select the title column")

    summary = import_rows(
        request.state.pb,
        project_id,
        entry["rows"],
        mapping,
        bool(entry.get("has_header")),
    )
    return templates.TemplateResponse(
        request,
        "components/topics/import_result.html",
        {
            "summary": summary,
            "filename": entry.get("filename", "") or ("Pasted text"),
            "project": require_project_access(request, project_id),
        },
    )


@router.get("/projects/{project_id}/topics/import")
@hx_error("Loading form failed")
def topic_import_form(request: Request, project_id: str):
    """Step-1 partial (paste/file) — used by the "Edit text" back button."""
    require_hx(request)
    require_project_access(request, project_id)
    require_project_role(request, project_id)
    project = require_project_access(request, project_id)
    return templates.TemplateResponse(
        request, "components/topics/import_step1.html", {"project": project}
    )


# ---------------------------------------------------------------------------
# Actions: index run
# ---------------------------------------------------------------------------
@router.post("/projects/{project_id}/run-index")
@hx_error("Starting indexing failed")
def run_index(request: Request, project_id: str, full: str = Form("0")):
    """Start an indexing run. full=1 → force re-embed + stale vector cleanup."""
    require_hx(request)
    require_project_access(request, project_id)
    require_project_role(request, project_id)
    is_full = safe_bool(full)
    JobRepo(request.state.pb).create(
        project=project_id,
        type="index_project",
        payload={"trigger": "manual", "force": is_full, "full": is_full},
        idempotency_key=f"index:project:{project_id}:{'full' if is_full else 'incr'}:{int(time.time())}",
        max_attempts=3,
        entity_type="project",
        entity_id=project_id,
    )
    return success_response(
        ("Full reindex started") if is_full else ("Incremental indexing started"),
        extra_events={"refreshIndexing": True, "refreshJobs": True},
    )


@router.post("/projects/{project_id}/documents/{document_id}/reindex")
@hx_error("Starting document indexing failed")
def reindex_document(request: Request, project_id: str, document_id: str, force: str = Form("0")):
    """Reindex a single document (selected-document reindex)."""
    require_hx(request)
    require_project_access(request, project_id)
    require_project_role(request, project_id)
    doc = DocumentRepo(request.state.pb).get(document_id)
    if not doc or doc.get("project") != project_id:
        return error_response("Document not found")
    JobRepo(request.state.pb).create(
        project=project_id,
        type="index_document",
        payload={"sourceId": str(doc.get("sourceId")), "force": safe_bool(force)},
        idempotency_key=f"index:document:{project_id}:{doc.get('sourceId')}:{int(time.time())}",
        max_attempts=3,
        entity_type="document",
        entity_id=document_id,
    )
    return success_response(
        ("Document indexing started"), extra_events={"refreshIndexing": True, "refreshJobs": True}
    )


@router.post("/projects/{project_id}/index-runs/{run_id}/retry")
@hx_error("Retry failed")
def retry_index_run(request: Request, project_id: str, run_id: str):
    """Retry a failed indexing run — resumes from its checkpoint."""
    require_hx(request)
    require_project_access(request, project_id)
    require_project_role(request, project_id)
    run = IndexRunRepo(request.state.pb).get(run_id)
    if not run or run.get("project") != project_id:
        return error_response("Run not found")
    JobRepo(request.state.pb).create(
        project=project_id,
        type="index_project",
        payload={"trigger": "manual", "resumeFrom": str(run.get("lastSourceId") or "0")},
        idempotency_key=f"index:project:{project_id}:retry:{run_id}:{int(time.time())}",
        max_attempts=3,
        entity_type="project",
        entity_id=project_id,
    )
    return success_response(
        ("Indexing run rescheduled"),
        extra_events={"refreshIndexing": True, "refreshJobs": True},
    )


# ---------------------------------------------------------------------------
# AI models
# ---------------------------------------------------------------------------
@router.post("/projects/{project_id}/ai-models")
@hx_error("Saving models failed")
def save_ai_models(
    request: Request,
    project_id: str,
    # outline
    outline_provider: str = Form(""),
    outline_model: str = Form(""),
    outline_temperature: str = Form(""),
    outline_max_tokens: str = Form(""),
    outline_timeout: str = Form(""),
    outline_retry_attempts: str = Form(""),
    outline_retry_base: str = Form(""),
    # section
    section_provider: str = Form(""),
    section_model: str = Form(""),
    section_temperature: str = Form(""),
    section_max_tokens: str = Form(""),
    section_timeout: str = Form(""),
    section_retry_attempts: str = Form(""),
    section_retry_base: str = Form(""),
    # optional roles
    meta_provider: str = Form(""),
    meta_model: str = Form(""),
    review_provider: str = Form(""),
    review_model: str = Form(""),
    # optional inline connection (custom/openai-compat endpoints)
    base_url: str = Form(""),
    api_key: str = Form(""),
):
    """Save per-role AI model configuration.

    Model changes create an audit event (previous config preserved); historical
    section metadata (provider/model/promptVersion) is never modified.
    """
    require_hx(request)
    require_project_access(request, project_id)
    require_project_role(request, project_id)
    user = require_user(request)

    settings = ProjectSettingsRepo(request.state.pb)
    before = settings.get_for_project(project_id)

    data = {
        "outlineProvider": safe_str(outline_provider),
        "outlineModel": safe_str(outline_model),
        "outlineTemperature": safe_float(outline_temperature, 0.7) or 0.7,
        "outlineMaxTokens": safe_int(outline_max_tokens, 4096) or 4096,
        "outlineTimeout": safe_float(outline_timeout, 120.0) or 120.0,
        "outlineRetry": {
            "max_attempts": safe_int(outline_retry_attempts, 3) or 3,
            "backoff_base": safe_int(outline_retry_base, 30) or 30,
        },
        "sectionProvider": safe_str(section_provider),
        "sectionModel": safe_str(section_model),
        "sectionTemperature": safe_float(section_temperature, 0.7) or 0.7,
        "sectionMaxTokens": safe_int(section_max_tokens, 4096) or 4096,
        "sectionTimeout": safe_float(section_timeout, 120.0) or 120.0,
        "sectionRetry": {
            "max_attempts": safe_int(section_retry_attempts, 3) or 3,
            "backoff_base": safe_int(section_retry_base, 30) or 30,
        },
        "metaProvider": safe_str(meta_provider),
        "metaModel": safe_str(meta_model),
        "reviewProvider": safe_str(review_provider),
        "reviewModel": safe_str(review_model),
    }
    settings.upsert(project_id, data)

    # inline connection (custom/openai-compat endpoints): upsert an llm integration
    if base_url or api_key:
        _upsert_llm_integration(
            request, project_id, outline_provider or section_provider, base_url, api_key, user
        )

    _audit_model_changes(request, project_id, user, before, settings.get_for_project(project_id))
    return success_response("Models saved")


def _upsert_llm_integration(
    request: Request,
    project_id: str,
    provider: str,
    base_url: str,
    api_key: str,
    user: dict[str, Any],
) -> None:
    """Create/update an llm integration for the given provider (secrets encrypted)."""
    from app.services.secrets import get_secrets_service as _secrets

    secrets = _secrets()
    repo = IntegrationRepo(request.state.pb)
    provider_name = safe_str(provider) or "custom"
    existing = repo.first(
        filter=f'project="{project_id}" && category="llm" && provider="{provider_name}"'
    )
    configuration: dict[str, Any] = {
        "base_url": normalize_base_url(safe_str(base_url)),
        "model": "",
    }
    if api_key:
        import json

        secrets_enc = secrets.encrypt(json.dumps({"api_key": api_key}, ensure_ascii=False))
        configuration["masked"] = secrets.mask(api_key)
    else:
        secrets_enc = existing.get("secretsEnc") or "" if existing else ""
        configuration["masked"] = (
            (existing.get("configuration") or {}).get("masked") if existing else ""
        )
    if existing:
        repo.update(
            existing["id"],
            {"configuration": configuration, "secretsEnc": secrets_enc, "enabled": True},
        )
    else:
        repo.create(
            project=project_id,
            category="llm",
            provider=provider_name,
            display_name=f"LLM ({provider_name})",
            configuration=configuration,
            secrets_enc=secrets_enc,
            enabled=True,
            created_by=user.get("id", ""),
        )


def _audit_model_changes(
    request: Request,
    project_id: str,
    user: dict[str, Any],
    before: dict[str, Any],
    after: dict[str, Any],
) -> None:
    """Append-only audit event per role whose provider/model changed."""
    events = JobEventRepo(request.state.pb)
    for role in ("outline", "section", "meta", "review"):
        p_old, p_new = before.get(f"{role}Provider") or "", after.get(f"{role}Provider") or ""
        m_old, m_new = before.get(f"{role}Model") or "", after.get(f"{role}Model") or ""
        if (p_old, m_old) != (p_new, m_new):
            events.add(
                project=project_id,
                event_type="config.model_changed",
                message=f"model changed for {role}: {p_old or '—'}/{m_old or '—'} → {p_new or '—'}/{m_new or '—'}",
                metadata={
                    "role": role,
                    "previous": {"provider": p_old, "model": m_old},
                    "new": {"provider": p_new, "model": m_new},
                    "changed_by": user.get("id", ""),
                },
            )


@router.get("/projects/{project_id}/ai-models/{role}/models")
async def discover_models(request: Request, project_id: str, role: str):
    """Model discovery for a role's provider — returns datalist options (cached)."""
    try:
        require_project_access(request, project_id)
        if role not in ("outline", "section", "meta", "review"):
            return HTMLResponse("")
        from app.providers.registry import ProviderRegistry
        from app.services.settings import ProjectConfig

        config = ProjectConfig.load(request.state.pb, project_id)
        role_cfg = config.role_llm(role)
        registry = ProviderRegistry(request.state.pb)
        integration = registry.active_integration_for_provider(
            project_id, "llm", role_cfg["provider"]
        )
        models: list[str] = []
        if integration:
            models = await registry.list_models(
                require_project_access(request, project_id), integration
            )
        return templates.TemplateResponse(
            request,
            "pages/projects/tabs/model_options.html",
            {"models": models, "role": role},
        )
    except Exception:
        return HTMLResponse("")


@router.post("/projects/{project_id}/ai-models/global")
@hx_error("Saving defaults failed")
def save_global_llm_defaults(
    request: Request,
    project_id: str,
    outline_provider: str = Form(""),
    outline_model: str = Form(""),
    section_provider: str = Form(""),
    section_model: str = Form(""),
    meta_provider: str = Form(""),
    meta_model: str = Form(""),
    review_provider: str = Form(""),
    review_model: str = Form(""),
):
    """Global default LLM settings (admin only) — per-project values override."""
    require_hx(request)
    from app.api.deps import require_admin

    require_admin(request)
    from app.repositories.app_settings import AppSettingsRepo

    repo = AppSettingsRepo(request.state.pb)
    current = repo.get_defaults().get("llm") or {}
    for role, provider, model in (
        ("outline", outline_provider, outline_model),
        ("section", section_provider, section_model),
        ("meta", meta_provider, meta_model),
        ("review", review_provider, review_model),
    ):
        current.setdefault(role, {})["provider"] = safe_str(provider)
        current.setdefault(role, {})["model"] = safe_str(model)
    repo.set_llm_defaults(current)
    return success_response("Global defaults saved")


# ---------------------------------------------------------------------------
# Retrieval diagnostics
# ---------------------------------------------------------------------------
@router.post("/projects/{project_id}/retrieval/diagnose")
@hx_error("Retrieval failed")
async def retrieval_diagnose(
    request: Request,
    project_id: str,
    query: str = Form(""),
    candidate_count: str = Form(""),
    similarity_threshold: str = Form(""),
    rerank_enabled: str = Form(""),
    rerank_top_n: str = Form(""),
):
    """Interactive retrieval diagnostics — shows query, scores, candidates."""
    require_hx(request)
    require_project_access(request, project_id)
    if not safe_str(query):
        return error_response("Search query is required")
    from app.schemas.retrieval import RetrievalOptions
    from app.services.internal_linking import ContextBuilder
    from app.services.retrieval import RetrievalService
    from app.services.settings import ProjectConfig

    project = require_project_access(request, project_id)
    config = ProjectConfig.load(request.state.pb, project_id)
    options = RetrievalOptions(
        candidate_count=safe_int(candidate_count, 20) or 20,
        similarity_threshold=safe_float(similarity_threshold, 0.0) or None,
        rerank=safe_bool(rerank_enabled),
        rerank_top_n=safe_int(rerank_top_n, 8) or 8,
    )
    results = await RetrievalService(request.state.pb).retrieve(config, query, options)
    builder = ContextBuilder(
        max_links=int(config.context.get("max_links") or 5),
        max_passages=int(config.context.get("max_passages") or 5),
        max_chars=int(config.context.get("max_chars") or 4000),
    )
    context = builder.build(query, results, current_title="")
    return templates.TemplateResponse(
        request,
        "pages/projects/tabs/retrieval_results.html",
        {
            "project": project,
            "query": query,
            "results": results,
            "context": context,
            "options": options,
        },
    )
