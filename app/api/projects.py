"""Projects — CRUD, tabs (settings/integrations/prompts/topics/articles/jobs/…), actions.

Routers only parse HTTP/HTMX and build responses; data access goes through
repositories, job creation through JobRepo.
"""

from __future__ import annotations

import re
import time
from typing import Any

from fastapi import APIRouter, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse
from starlette.datastructures import UploadFile as StarletteUploadFile

from app.api.deps import (
    PROJECT_ADMIN_ROLES,
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
    FIELD_LABELS,
    IMPORT_FIELDS,
    build_column_map,
    detect_columns,
    detect_header,
    get_preview,
    import_rows,
    parse_delimited,
    store_preview,
)
from app.templates import templates
from app.utils import error_response, ok_with_redirect, success_response

router = APIRouter()

TABS = [
    "settings",
    "integrations",
    "ai_models",
    "prompts",
    "topics",
    "articles",
    "retrieval",
    "jobs",
    "indexing",
    "publishing",
    "logs",
]

INTEGRATION_CATEGORIES = {
    "llm": "مدل زبانی (LLM)",
    "embedding": "جاسازی متن (Embedding)",
    "reranker": "بازچینش (Reranker)",
    "vector_store": "ذخیره‌سازی برداری (Qdrant)",
    "publisher": "انتشار (WordPress)",
}

HEALTH_LABELS = {
    "unknown": "نامشخص",
    "healthy": "سالم",
    "degraded": "مشکل‌دار",
    "unhealthy": "ناسالم",
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
        request, "pages/projects/list.html", {"title": "پروژه‌ها", "projects": projects}
    )


@router.post("/projects")
@hx_error("ساخت پروژه ناموفق بود")
def create_project(
    request: Request,
    name: str = Form(""),
    slug: str = Form(""),
    language: str = Form("fa"),
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
        language=safe_str(language, "fa"),
        timezone=safe_str(timezone, "Asia/Tehran"),
        description=safe_str(description),
        created_by=user.get("id", ""),
    )
    # Creator becomes owner of the project (authorization).
    MemberRepo(request.state.pb).add(project=project["id"], user=user.get("id", ""), role="owner")
    return ok_with_redirect("پروژه ساخته شد", f"/projects/{project['id']}")


@router.post("/projects/{project_id}/delete")
@hx_error("حذف پروژه ناموفق بود")
def delete_project(request: Request, project_id: str):
    require_hx(request)
    require_project_access(request, project_id)
    require_project_role(request, project_id, PROJECT_ADMIN_ROLES)
    ProjectRepo(request.state.pb).delete(project_id)
    return ok_with_redirect("پروژه حذف شد", "/projects", type="info")


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
@hx_error("تغییر وضعیت پروژه ناموفق بود")
def toggle_project_status(request: Request, project_id: str):
    """Flip the project between active and inactive (live)."""
    require_hx(request)
    require_project_access(request, project_id)
    require_project_role(request, project_id, PROJECT_ADMIN_ROLES)
    project = ProjectRepo(request.state.pb).get(project_id)
    if not project:
        return error_response("پروژه یافت نشد")
    new_status = "inactive" if project.get("status") == "active" else "active"
    ProjectRepo(request.state.pb).set_status(project_id, new_status)
    message = "پروژه فعال شد" if new_status == "active" else "پروژه غیرفعال شد"
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
            "title": f"موضوع‌ها — {project.get('name', '')}",
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
@hx_error("عملیات گروهی ناموفق بود")
def topics_bulk(
    request: Request, project_id: str, action: str = Form(""), topic_ids: str = Form("")
):
    """Bulk actions: generate | retry | cancel (comma-separated topic ids)."""
    require_hx(request)
    require_project_access(request, project_id)
    require_project_role(request, project_id)
    ids = [i.strip() for i in topic_ids.split(",") if i.strip()]
    if not ids:
        return error_response("موضوعی انتخاب نشده است")
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
        f"{done} موضوع به‌روزرسانی شد", extra_events={"refreshTopics": True, "refreshJobs": True}
    )


@router.post("/projects/{project_id}/topics/{topic_id}/cancel")
@hx_error("لغو موضوع ناموفق بود")
def cancel_topic(request: Request, project_id: str, topic_id: str):
    require_hx(request)
    require_project_access(request, project_id)
    require_project_role(request, project_id)
    topic = TopicRepo(request.state.pb).get(topic_id)
    if not topic or topic.get("project") != project_id:
        return error_response("موضوع یافت نشد")
    TopicRepo(request.state.pb).set_status(topic_id, "cancelled")
    return success_response("موضوع لغو شد", extra_events={"refreshTopics": True})


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
        context["categories"] = INTEGRATION_CATEGORIES
        context["health_labels"] = HEALTH_LABELS
        context["known_providers"] = {
            cat: registry.available_providers(cat) for cat in INTEGRATION_CATEGORIES
        }
    elif tab == "prompts":
        from app.domain.prompt_render import VARIABLE_REGISTRY

        context["prompt_types"] = {
            "outline_system": "رئوس — سیستم",
            "outline_user": "رئوس — کاربر",
            "section_system": "بخش — سیستم",
            "section_user": "بخش — کاربر",
            "seo_rules": "قوانین سئو",
            "internal_linking": "لینک‌سازی داخلی",
            "brand_voice": "صدای برند",
            "validation": "اعتبارسنجی مقاله",
        }
        repo = PromptRepo(pb)
        context["prompts"] = repo.list_for_project(project_id)
        context["history"] = {
            ptype: repo.history(project_id, ptype, limit=20) for ptype in context["prompt_types"]
        }
        context["topics"] = TopicRepo(pb).list_for_project(project_id, per_page=50)
        context["variables"] = VARIABLE_REGISTRY
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
            ("outline", "مدل رئوس مطالب (Outline)", True),
            ("section", "مدل نویسنده بخش‌ها (Section)", True),
            ("meta", "مدل متا/سئو (اختیاری)", False),
            ("review", "مدل بازبینی (اختیاری)", False),
        ]
    elif tab == "retrieval":
        context["settings"] = ProjectSettingsRepo(pb).get_for_project(project_id)
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

    return context


@router.get("/projects/{project_id}", response_class=HTMLResponse)
def project_detail(request: Request, project_id: str, tab: str = "settings"):
    try:
        project = require_project_access(request, project_id)
    except (PermissionError, ValueError):
        return templates.TemplateResponse(
            request, "pages/projects/not_found.html", {"title": "پروژه یافت نشد"}
        )
    active = tab if tab in TABS else "settings"
    context = _tab_context(
        request.state.pb,
        project,
        active,
        status=request.query_params.get("status") or "",
        q=request.query_params.get("q") or "",
    )
    context["title"] = project.get("name", "پروژه")
    context["active_tab"] = active
    context["tabs"] = TABS
    return templates.TemplateResponse(request, "pages/projects/detail.html", context)


@router.get("/projects/{project_id}/tabs/{tab}", response_class=HTMLResponse)
def project_tab(request: Request, project_id: str, tab: str):
    try:
        project = require_project_access(request, project_id)
    except (PermissionError, ValueError):
        if is_hx_request(request):
            return error_response("پروژه یافت نشد یا دسترسی ندارید")
        return templates.TemplateResponse(
            request, "pages/projects/not_found.html", {"title": "پروژه یافت نشد"}
        )
    if tab not in TABS:
        return HTMLResponse("")
    context = _tab_context(
        request.state.pb,
        project,
        tab,
        status=request.query_params.get("status") or "",
        q=request.query_params.get("q") or "",
    )
    return templates.TemplateResponse(request, f"pages/projects/tabs/{tab}.html", context)


# ---------------------------------------------------------------------------
# Settings (flat schema)
# ---------------------------------------------------------------------------
@router.post("/projects/{project_id}/settings")
@hx_error("ذخیره تنظیمات ناموفق بود")
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
        "autosave": {
            "enabled": safe_bool(autosave_enabled),
            "interval_minutes": safe_int(autosave_interval_minutes, 5),
        },
    }
    ProjectSettingsRepo(request.state.pb).upsert(project_id, data)
    return success_response("تنظیمات ذخیره شد")


# ---------------------------------------------------------------------------
# Schedules
# ---------------------------------------------------------------------------
@router.post("/projects/{project_id}/schedules")
@hx_error("ذخیره زمان‌بندی ناموفق بود")
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
    schedule = ScheduleRepo(request.state.pb)
    existing = schedule.first(filter=f'project="{project_id}" && kind="{kind}"')
    payload = {
        "enabled": safe_bool(enabled),
        "intervalMinutes": safe_int(interval_minutes, 1440),
    }
    if existing:
        schedule.update(existing["id"], payload)
    else:
        schedule.create(
            project=project_id,
            name=kind,
            kind=kind,
            interval_minutes=safe_int(interval_minutes, 1440),
            payload={},
        )
        created = schedule.first(filter=f'project="{project_id}" && kind="{kind}"')
        if created:
            schedule.update(created["id"], payload)
    return success_response("زمان‌بندی ذخیره شد")


# ---------------------------------------------------------------------------
# Integrations (configurable; secrets encrypted, never rendered plaintext)
# ---------------------------------------------------------------------------
@router.post("/projects/{project_id}/integrations")
@hx_error("ذخیره اتصال ناموفق بود")
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
):
    require_hx(request)
    require_project_access(request, project_id)
    require_project_role(request, project_id)
    user = require_user(request)
    if category not in INTEGRATION_CATEGORIES:
        return error_response("دسته‌بندی نامعتبر است")
    secrets: SecretsService = get_secrets_service()
    repo = IntegrationRepo(request.state.pb)

    existing = repo.get(record_id) if record_id else None
    if existing:
        ensure_record_in_project(existing, project_id, "integration")
    if existing and not secret:
        secrets_enc = existing.get("secretsEnc") or ""
    else:
        import json

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
    return success_response("اتصال ذخیره شد", extra_events={"refreshIntegrations": True})


@router.post("/projects/{project_id}/integrations/{record_id}/toggle")
@hx_error("تغییر وضعیت ناموفق بود")
def toggle_integration(request: Request, project_id: str, record_id: str):
    require_hx(request)
    require_project_access(request, project_id)
    require_project_role(request, project_id)
    repo = IntegrationRepo(request.state.pb)
    integration = repo.get(record_id)
    if not integration:
        return error_response("اتصال یافت نشد")
    ensure_record_in_project(integration, project_id, "integration")
    repo.set_enabled(record_id, not integration.get("enabled", False))
    return success_response("وضعیت اتصال تغییر کرد", extra_events={"refreshIntegrations": True})


@router.post("/projects/{project_id}/integrations/{record_id}/test")
@hx_error("تست اتصال ناموفق بود")
async def test_integration(request: Request, project_id: str, record_id: str):
    require_hx(request)
    require_project_access(request, project_id)
    repo = IntegrationRepo(request.state.pb)
    integration = repo.get(record_id)
    if not integration or integration.get("project") != project_id:
        return error_response("اتصال یافت نشد")
    from app.providers.registry import ProviderRegistry

    result = await ProviderRegistry(request.state.pb).test_integration(
        require_project_access(request, project_id), integration
    )
    return templates.TemplateResponse(
        request,
        "pages/projects/tabs/integration_health.html",
        {"integration": integration, "result": result},
    )


@router.post("/projects/{project_id}/integrations/{record_id}/delete")
@hx_error("حذف اتصال ناموفق بود")
def delete_integration(request: Request, project_id: str, record_id: str):
    require_hx(request)
    require_project_access(request, project_id)
    require_project_role(request, project_id)
    integration = IntegrationRepo(request.state.pb).get(record_id)
    ensure_record_in_project(integration, project_id, "integration")
    IntegrationRepo(request.state.pb).delete(record_id)
    return success_response("اتصال حذف شد", extra_events={"refreshIntegrations": True})


# ---------------------------------------------------------------------------
# Prompts (versioned)
# ---------------------------------------------------------------------------
@router.post("/projects/{project_id}/prompts")
def save_prompts(
    request: Request,
    project_id: str,
    outline_system: str = Form(""),
    outline_user: str = Form(""),
    section_system: str = Form(""),
    section_user: str = Form(""),
    seo_rules: str = Form(""),
    internal_linking: str = Form(""),
    brand_voice: str = Form(""),
    validation: str = Form(""),
):
    require_hx(request)
    require_project_access(request, project_id)
    require_project_role(request, project_id)
    user = require_user(request)
    values = {
        "outline_system": outline_system,
        "outline_user": outline_user,
        "section_system": section_system,
        "section_user": section_user,
        "seo_rules": seo_rules,
        "internal_linking": internal_linking,
        "brand_voice": brand_voice,
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
            "متغیر(های) نامعتبر: " + ", ".join(e.unknown), extra_events={"refreshPrompts": True}
        )
    for ptype, content in values.items():
        service.save(project_id, ptype, content, author=user.get("id", ""))
    return success_response("پرامپت‌ها ذخیره شد", extra_events={"refreshPrompts": True})


# ---------------------------------------------------------------------------
# Prompt management (versioning, compare, tester)
# ---------------------------------------------------------------------------
@router.post("/projects/{project_id}/prompts/{ptype}/activate")
@hx_error("فعال‌سازی نسخه ناموفق بود")
def activate_prompt_version(
    request: Request, project_id: str, ptype: str, version_id: str = Form("")
):
    require_hx(request)
    require_project_access(request, project_id)
    require_project_role(request, project_id)
    version = PromptRepo(request.state.pb).get(version_id)
    if version is None or version.get("type") != ptype:
        return error_response("نسخه یافت نشد")
    if str(version.get("project") or "") not in ("", project_id):
        return error_response("نسخه یافت نشد")
    PromptService(request.state.pb).activate(project_id, ptype, version_id)
    return success_response("نسخه فعال شد", extra_events={"refreshPrompts": True})


@router.post("/projects/{project_id}/prompts/{ptype}/duplicate")
@hx_error("تکراری‌سازی ناموفق بود")
def duplicate_prompt_version(
    request: Request, project_id: str, ptype: str, version_id: str = Form("")
):
    require_hx(request)
    require_project_access(request, project_id)
    require_project_role(request, project_id)
    user = require_user(request)
    version = PromptRepo(request.state.pb).get(version_id)
    if version is None or version.get("type") != ptype:
        return error_response("نسخه یافت نشد")
    if str(version.get("project") or "") not in ("", project_id):
        return error_response("نسخه یافت نشد")
    PromptService(request.state.pb).duplicate(
        project_id, ptype, version_id, author=user.get("id", "")
    )
    return success_response("نسخه تکراری‌سازی شد (غیرفعال)", extra_events={"refreshPrompts": True})


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
@hx_error("تست پرامپت ناموفق بود")
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
@hx_error("افزودن موضوع ناموفق بود")
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
        return error_response("عنوان موضوع الزامی است")
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
    return success_response("موضوع اضافه شد", extra_events={"refreshTopics": True})


@router.post("/projects/{project_id}/topics/{topic_id}/write")
@hx_error("شروع تولید مقاله ناموفق بود")
def write_topic(request: Request, project_id: str, topic_id: str):
    require_hx(request)
    require_project_access(request, project_id)
    require_project_role(request, project_id)
    topic = TopicRepo(request.state.pb).get(topic_id)
    if not topic or topic.get("project") != project_id:
        return error_response("موضوع یافت نشد")
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
        "تولید مقاله آغاز شد", extra_events={"refreshTopics": True, "refreshJobs": True}
    )


@router.post("/projects/{project_id}/topics/{topic_id}/delete")
@hx_error("حذف موضوع ناموفق بود")
def delete_topic(request: Request, project_id: str, topic_id: str):
    require_hx(request)
    require_project_access(request, project_id)
    require_project_role(request, project_id)
    topic = TopicRepo(request.state.pb).get(topic_id)
    if not topic or topic.get("project") != project_id:
        return error_response("موضوع یافت نشد")
    TopicRepo(request.state.pb).delete(topic_id)
    return success_response("موضوع حذف شد", extra_events={"refreshTopics": True})


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
@hx_error("خواندن فایل یا متن ناموفق بود")
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
        return error_response("متن یا فایلی وارد نشده است")

    delimiter, rows = parse_delimited(csv_text)
    if not rows:
        return error_response("هیچ ردیفی پیدا نشد")
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
            "field_labels": FIELD_LABELS,
            "row_count": len(rows) - (1 if has_header else 0),
            "data_preview": data_preview,
            "project": require_project_access(request, project_id),
        },
    )


@router.post("/projects/{project_id}/topics/import")
@hx_error("افزودن گروهی ناموفق بود")
async def topic_import(request: Request, project_id: str):
    """Confirm an import: create the cached rows using the chosen column map."""
    require_hx(request)
    require_project_access(request, project_id)
    require_project_role(request, project_id)

    form = await request.form()
    token = str(form.get("token", "") or "")
    entry = get_preview(token)
    if not entry:
        return error_response("پیش‌نمایش منقضی شده است؛ دوباره تلاش کنید")

    mapping: dict[str, int] = {}
    for field in IMPORT_FIELDS:
        raw = str(form.get(f"col_{field}", "") or "")
        if raw.isdigit():
            mapping[field] = int(raw)
    if "title" not in mapping:
        return error_response("ستون عنوان را انتخاب کنید")

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
            "filename": entry.get("filename", "") or "متن جای‌گذاری‌شده",
            "project": require_project_access(request, project_id),
        },
    )


@router.get("/projects/{project_id}/topics/import")
@hx_error("بارگذاری فرم ناموفق بود")
def topic_import_form(request: Request, project_id: str):
    """Step-1 partial (paste/file) — used by the «تغییر متن» back button."""
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
@hx_error("شروع نمایه‌سازی ناموفق بود")
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
        "بازنمایه کامل آغاز شد" if is_full else "نمایه‌سازی افزایشی آغاز شد",
        extra_events={"refreshIndexing": True, "refreshJobs": True},
    )


@router.post("/projects/{project_id}/documents/{document_id}/reindex")
@hx_error("شروع نمایه‌سازی مستند ناموفق بود")
def reindex_document(request: Request, project_id: str, document_id: str, force: str = Form("0")):
    """Reindex a single document (selected-document reindex)."""
    require_hx(request)
    require_project_access(request, project_id)
    require_project_role(request, project_id)
    doc = DocumentRepo(request.state.pb).get(document_id)
    if not doc or doc.get("project") != project_id:
        return error_response("مستند یافت نشد")
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
        "نمایه‌سازی مستند آغاز شد", extra_events={"refreshIndexing": True, "refreshJobs": True}
    )


@router.post("/projects/{project_id}/index-runs/{run_id}/retry")
@hx_error("تلاش مجدد ناموفق بود")
def retry_index_run(request: Request, project_id: str, run_id: str):
    """Retry a failed indexing run — resumes from its checkpoint."""
    require_hx(request)
    require_project_access(request, project_id)
    require_project_role(request, project_id)
    run = IndexRunRepo(request.state.pb).get(run_id)
    if not run or run.get("project") != project_id:
        return error_response("اجرا یافت نشد")
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
        "اجرای نمایه‌سازی دوباره برنامه‌ریزی شد",
        extra_events={"refreshIndexing": True, "refreshJobs": True},
    )


# ---------------------------------------------------------------------------
# AI models
# ---------------------------------------------------------------------------
@router.post("/projects/{project_id}/ai-models")
@hx_error("ذخیره مدل‌ها ناموفق بود")
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
    return success_response("مدل‌ها ذخیره شد")


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
@hx_error("ذخیره پیش‌فرض‌ها ناموفق بود")
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
    return success_response("پیش‌فرض‌های سراسری ذخیره شد")


# ---------------------------------------------------------------------------
# Retrieval diagnostics
# ---------------------------------------------------------------------------
@router.post("/projects/{project_id}/retrieval/diagnose")
@hx_error("بازیابی ناموفق بود")
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
        return error_response("عبارت جستجو الزامی است")
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
