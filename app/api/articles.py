"""Articles — detail page, title/meta editor, section editors, publish action."""

from __future__ import annotations

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse

from app.api.deps import (
    ensure_record_in_project,
    require_hx,
    require_project_access,
    require_project_role,
    safe_str,
)
from app.api.errors import hx_error
from app.domain.article_html import slugify
from app.domain.sanitize import sanitize_html
from app.repositories.articles import ArticleRepo, SectionRepo
from app.repositories.jobs import JobRepo
from app.repositories.publishing_runs import PublishingRunRepo
from app.repositories.topics import TopicRepo
from app.templates import templates
from app.utils import error_response, success_response

router = APIRouter()


@router.get("/projects/{project_id}/articles/{article_id}", response_class=HTMLResponse)
def article_detail(request: Request, project_id: str, article_id: str):
    project = require_project_access(request, project_id)
    article = ArticleRepo(request.state.pb).get(article_id)
    if not article or article.get("project") != project_id:
        return templates.TemplateResponse(
            request,
            "pages/articles/not_found.html",
            {"title": "مقاله یافت نشد", "project": project},
        )
    sections = SectionRepo(request.state.pb).list_for_article(article_id)
    topic = TopicRepo(request.state.pb).get(article.get("topicId") or "")
    runs = PublishingRunRepo(request.state.pb).list_for_article(article_id, per_page=10)
    return templates.TemplateResponse(
        request,
        "pages/articles/detail.html",
        {
            "title": article.get("title", "مقاله"),
            "project": project,
            "article": article,
            "sections": sections,
            "topic": topic,
            "publish_runs": runs,
        },
    )


@router.post("/projects/{project_id}/articles/{article_id}/meta")
@hx_error("ذخیره ناموفق بود")
def save_meta(
    request: Request,
    project_id: str,
    article_id: str,
    title: str = Form(""),
    meta_description: str = Form(""),
):
    require_hx(request)
    require_project_access(request, project_id)
    require_project_role(request, project_id)
    article = ArticleRepo(request.state.pb).get(article_id)
    if not article:
        return error_response("مقاله یافت نشد")
    ensure_record_in_project(article, project_id, "article")
    title = safe_str(title) or article.get("title") or ""
    payload: dict = {"title": title, "metaDescription": safe_str(meta_description)}
    if not article.get("slug"):
        payload["slug"] = slugify(title)
    ArticleRepo(request.state.pb).update(article_id, payload)
    return success_response("عنوان و توضیحات ذخیره شد")


@router.post("/projects/{project_id}/articles/{article_id}/sections/{section_id}")
@hx_error("ذخیره بخش ناموفق بود")
def save_section(
    request: Request,
    project_id: str,
    article_id: str,
    section_id: str,
    heading: str = Form(""),
    content_brief: str = Form(""),
    content: str = Form(""),
):
    require_hx(request)
    require_project_access(request, project_id)
    require_project_role(request, project_id)
    repo = SectionRepo(request.state.pb)
    section = repo.get(section_id)
    if not section or section.get("article") != article_id:
        return error_response("بخش یافت نشد")
    article = ArticleRepo(request.state.pb).get(article_id)
    ensure_record_in_project(article, project_id, "article")
    clean = sanitize_html(content)
    repo.update(
        section_id,
        {
            "heading": safe_str(heading),
            "contentBrief": safe_str(content_brief),
            "content": clean,
            "status": "done",
            "error": {},
        },
    )
    # Edited sections invalidate the assembled article → back to review state.
    if article and article.get("status") not in ("published", "publishing"):
        ArticleRepo(request.state.pb).set_status(article_id, "review")
    return success_response("بخش ذخیره شد", extra_events={"refreshArticle": True})


@router.post("/projects/{project_id}/articles/{article_id}/regenerate")
@hx_error("شروع بازتولید ناموفق بود")
def regenerate_article(
    request: Request, project_id: str, article_id: str, section_id: str = Form("")
):
    """Regenerate one section (when `section_id` is given) or the whole
    article. NOTE: this is the single handler for this path — a duplicate
    route in workspace.py used to shadow/be shadowed; the review page's
    «بازتولید کامل» hits this with no section_id."""
    require_hx(request)
    require_project_access(request, project_id)
    require_project_role(request, project_id)
    article = ArticleRepo(request.state.pb).get(article_id)
    if not article or article.get("project") != project_id:
        return error_response("مقاله یافت نشد")

    if section_id:
        # --- per-section regeneration (workspace editor) ---
        repo = SectionRepo(request.state.pb)
        section = repo.get(section_id)
        if not section or section.get("article") != article_id:
            return error_response("بخش یافت نشد")
        repo.update(section_id, {"status": "pending", "content": "", "error": {}})
        ArticleRepo(request.state.pb).set_status(article_id, "generating")
        JobRepo(request.state.pb).create(
            project=project_id,
            type="generate_section",
            payload={"sectionId": section_id},
            idempotency_key=f"generate:section:{section_id}",
            max_attempts=3,
            entity_type="section",
            entity_id=section_id,
        )
        return success_response(
            "بازتولید بخش برنامه‌ریزی شد",
            extra_events={"refreshArticle": True, "refreshJobs": True},
        )

    # --- full article regeneration (review page: «بازتولید کامل») ---
    topic_id = article.get("topicId") or ""
    if not topic_id:
        return error_response("مقاله به موضوعی متصل نیست")
    ArticleRepo(request.state.pb).set_status(article_id, "generating")
    JobRepo(request.state.pb).create(
        project=project_id,
        type="write_article",
        payload={"topicId": topic_id, "regenerate": True},
        idempotency_key=f"write:article:{topic_id}:regen:{int(__import__('time').time())}",
        max_attempts=3,
        entity_type="article",
        entity_id=article_id,
    )
    return success_response(
        "بازتولید مقاله آغاز شد (نسخه قبلی حفظ می‌شود)",
        extra_events={"refreshArticle": True, "refreshJobs": True},
    )


def _queue_publish_job(request: Request, project_id: str, article_id: str, action: str) -> None:
    """Queue a publish job with a distinct idempotency key per action; skips
    when an identical job is already active (pending/retrying/running)."""
    import time

    active = JobRepo(request.state.pb).first(
        filter=f'type="publish_article" && payload.articleId="{article_id}" && payload.action="{action}" && (status="pending" || status="retrying" || status="running")'
    )
    if active:
        return
    JobRepo(request.state.pb).create(
        project=project_id,
        type="publish_article",
        payload={"articleId": article_id, "action": action},
        idempotency_key=f"{action}:article:{article_id}:{int(time.time())}",
        max_attempts=3,
        entity_type="article",
        entity_id=article_id,
    )


@router.post("/projects/{project_id}/articles/{article_id}/publish")
@hx_error("شروع انتشار ناموفق بود")
def publish_article(request: Request, project_id: str, article_id: str):
    require_hx(request)
    require_project_access(request, project_id)
    require_project_role(request, project_id)
    article = ArticleRepo(request.state.pb).get(article_id)
    if not article or article.get("project") != project_id:
        return error_response("مقاله یافت نشد")
    if not article.get("finalHtml"):
        return error_response("مقاله هنوز محتوایی ندارد")
    _queue_publish_job(request, project_id, article_id, "publish")
    return success_response(
        "انتشار آغاز شد", extra_events={"refreshArticle": True, "refreshJobs": True}
    )


@router.post("/projects/{project_id}/articles/{article_id}/update")
@hx_error("شروع به‌روزرسانی ناموفق بود")
def update_article_post(request: Request, project_id: str, article_id: str):
    """Content refresh on the existing WordPress post (never a duplicate)."""
    require_hx(request)
    require_project_access(request, project_id)
    require_project_role(request, project_id)
    article = ArticleRepo(request.state.pb).get(article_id)
    if not article or article.get("project") != project_id:
        return error_response("مقاله یافت نشد")
    if not article.get("wordpressPostId"):
        return error_response("مقاله هنوز در وردپرس منتشر نشده است — ابتدا انتشار دهید")
    _queue_publish_job(request, project_id, article_id, "update")
    return success_response(
        "به‌روزرسانی در وردپرس آغاز شد",
        extra_events={"refreshArticle": True, "refreshJobs": True},
    )


@router.post("/projects/{project_id}/articles/{article_id}/unpublish")
@hx_error("شروع لغو انتشار ناموفق بود")
def unpublish_article_post(request: Request, project_id: str, article_id: str):
    """Safely unpublish: WordPress post → private (reversible)."""
    require_hx(request)
    require_project_access(request, project_id)
    require_project_role(request, project_id)
    article = ArticleRepo(request.state.pb).get(article_id)
    if not article or article.get("project") != project_id:
        return error_response("مقاله یافت نشد")
    if not article.get("wordpressPostId"):
        return error_response("مقاله در وردپرس نیست")
    _queue_publish_job(request, project_id, article_id, "unpublish")
    return success_response(
        "لغو انتشار (خصوصی‌سازی) آغاز شد",
        extra_events={"refreshArticle": True, "refreshJobs": True},
    )


@router.post("/projects/{project_id}/articles/{article_id}/publish-runs/{run_id}/retry")
@hx_error("تلاش مجدد ناموفق بود")
def retry_publish_run(request: Request, project_id: str, article_id: str, run_id: str):
    """Retry a failed publish attempt."""
    require_hx(request)
    require_project_access(request, project_id)
    require_project_role(request, project_id)
    from app.repositories.publishing_runs import PublishingRunRepo

    run = PublishingRunRepo(request.state.pb).get(run_id)
    if not run or run.get("article") != article_id:
        return error_response("تلاش انتشار یافت نشد")
    article = ArticleRepo(request.state.pb).get(article_id)
    ensure_record_in_project(article, project_id, "article")
    mode = str(run.get("mode") or "publish")
    if mode not in ("publish", "update", "unpublish"):
        mode = "publish"
    _queue_publish_job(request, project_id, article_id, mode)
    return success_response(
        "تلاش مجدد برنامه‌ریزی شد", extra_events={"refreshArticle": True, "refreshJobs": True}
    )
