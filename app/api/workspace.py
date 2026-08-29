"""Article workspace — 3-pane editor, outline ops, live section status."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse

from app.api.deps import (
    require_hx,
    require_project_access,
    require_project_role,
    require_user,
    safe_str,
)
from app.api.errors import hx_error, page_guard
from app.domain.article_validation import ArticleValidator
from app.repositories.articles import ArticleRepo, SectionRepo
from app.repositories.jobs import JobRepo, now_utc
from app.repositories.publishing_runs import PublishingRunRepo
from app.repositories.topics import TopicRepo
from app.services.outline_editor import OutlineEditor
from app.services.revisions import RevisionService
from app.templates import templates
from app.utils import error_response, success_response

router = APIRouter()


@router.get("/projects/{project_id}/articles/{article_id}/workspace", response_class=HTMLResponse)
@page_guard("مشکلی در بارگذاری کارگاه مقاله پیش آمد — دوباره تلاش کنید.")
def article_workspace(request: Request, project_id: str, article_id: str):
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
    internal_links = _collect_links(article)
    return templates.TemplateResponse(
        request,
        "pages/articles/workspace.html",
        {
            "title": article.get("title", "مقاله"),
            "project": project,
            "article": article,
            "sections": sections,
            "topic": topic,
            "publish_runs": runs,
            "internal_links": internal_links,
        },
    )


def _collect_links(article: dict) -> list[dict]:
    """Deduplicated intended internal links from the outline snapshot."""
    outline = article.get("outline")
    if not isinstance(outline, dict):
        return []
    seen: set[str] = set()
    links: list[dict] = []
    for plan in outline.get("sections") or []:
        if not isinstance(plan, dict):
            continue
        for link in plan.get("internal_links") or []:
            if not isinstance(link, dict):
                continue
            url = str(link.get("url") or "").rstrip("/")
            if url and url not in seen:
                seen.add(url)
                links.append(link)
    return links


@router.get("/projects/{project_id}/articles/{article_id}/outline", response_class=HTMLResponse)
def article_outline_pane(request: Request, project_id: str, article_id: str):
    """Left pane fragment — polled while sections generate."""
    require_project_access(request, project_id)
    article = ArticleRepo(request.state.pb).get(article_id)
    if not article or article.get("project") != project_id:
        return HTMLResponse("")
    sections = SectionRepo(request.state.pb).list_for_article(article_id)
    return templates.TemplateResponse(
        request,
        "pages/articles/_outline_pane.html",
        {"project": {"id": project_id}, "article": article, "sections": sections},
    )


@router.get(
    "/projects/{project_id}/articles/{article_id}/sections/{section_id}/status",
    response_class=HTMLResponse,
)
def section_status(request: Request, project_id: str, article_id: str, section_id: str):
    """Live section generation status — polled every ~2.5s while generating."""
    require_project_access(request, project_id)
    section = SectionRepo(request.state.pb).get(section_id)
    if not section or section.get("article") != article_id:
        return HTMLResponse("")
    article = ArticleRepo(request.state.pb).get(article_id)
    if not article or article.get("project") != project_id:
        return HTMLResponse("")
    elapsed = None
    started = section.get("generationStartedAt")
    if started:
        try:
            import datetime as dt

            s = (
                started
                if isinstance(started, dt.datetime)
                else dt.datetime.fromisoformat(str(started).replace("Z", "+00:00"))
            )
            elapsed = max(0, int((now_utc() - s).total_seconds()))
        except (TypeError, ValueError):
            elapsed = None
    return templates.TemplateResponse(
        request,
        "pages/articles/_section_status.html",
        {"section": section, "elapsed": elapsed, "project_id": project_id},
    )


@router.post("/projects/{project_id}/articles/{article_id}/status")
@hx_error("تغییر وضعیت ناموفق بود")
def set_article_status(request: Request, project_id: str, article_id: str, status: str = Form("")):
    """Explicit status transitions from the workspace (e.g. review → ready_to_publish)."""
    require_hx(request)
    require_project_access(request, project_id)
    require_project_role(request, project_id)
    article = ArticleRepo(request.state.pb).get(article_id)
    if not article or article.get("project") != project_id:
        return error_response("مقاله یافت نشد")
    if status == "ready_to_publish" and article.get("status") not in (
        "review",
        "ready_to_publish",
    ):
        return error_response("مقاله هنوز آماده انتشار نیست")
    ArticleRepo(request.state.pb).set_status(article_id, status)
    return success_response("وضعیت مقاله به‌روزرسانی شد", extra_events={"refreshArticle": True})


@router.post("/projects/{project_id}/articles/{article_id}/assemble")
@hx_error("برنامه‌ریزی بازسازی ناموفق بود")
def queue_assemble(request: Request, project_id: str, article_id: str):
    """Queue an assemble job (rebuild final HTML from current sections).

    Reuses an assemble job that is still active (pending/retrying/running); a
    fresh job is created otherwise, so re-assembling a finished article works.
    """
    require_hx(request)
    require_project_access(request, project_id)
    require_project_role(request, project_id)
    article = ArticleRepo(request.state.pb).get(article_id)
    if not article or article.get("project") != project_id:
        return error_response("مقاله یافت نشد")
    active = JobRepo(request.state.pb).first(
        filter=f'type="assemble_article" && payload.articleId="{article_id}" && (status="pending" || status="retrying" || status="running")'
    )
    if active:
        return success_response(
            "مقاله در حال ساخت است — صفحه خودبه‌خود تازه می‌شود",
            extra_events={"refreshArticle": True},
        )
    JobRepo(request.state.pb).create(
        project=project_id,
        type="assemble_article",
        payload={"articleId": article_id},
        idempotency_key=f"assemble:article:{article_id}:{int(now_utc().timestamp())}",
        max_attempts=60,
        entity_type="article",
        entity_id=article_id,
    )
    return success_response(
        "بازسازی مقاله برنامه‌ریزی شد",
        extra_events={"refreshArticle": True, "refreshJobs": True},
    )


# ---------------------------------------------------------------------------
# Outline editor ops
# ---------------------------------------------------------------------------
def _load_article(request: Request, project_id: str, article_id: str) -> dict | None:
    article = ArticleRepo(request.state.pb).get(article_id)
    if not article or article.get("project") != project_id:
        return None
    return article


@router.post("/projects/{project_id}/articles/{article_id}/outline/sections/{position}/move")
@hx_error("جابه‌جایی بخش ناموفق بود")
def outline_move(
    request: Request, project_id: str, article_id: str, position: int, direction: str = Form("")
):
    require_hx(request)
    require_project_access(request, project_id)
    require_project_role(request, project_id)
    article = _load_article(request, project_id, article_id)
    if not article:
        return error_response("مقاله یافت نشد")
    try:
        OutlineEditor(request.state.pb).move(article, position, direction)
    except ValueError as e:
        return error_response(str(e))
    return success_response("بخش جابه‌جا شد", extra_events={"refreshArticle": True})


@router.post("/projects/{project_id}/articles/{article_id}/outline/sections/add")
@hx_error("افزودن بخش ناموفق بود")
def outline_add(
    request: Request,
    project_id: str,
    article_id: str,
    heading: str = Form(""),
    content_brief: str = Form(""),
):
    require_hx(request)
    require_project_access(request, project_id)
    require_project_role(request, project_id)
    article = _load_article(request, project_id, article_id)
    if not article:
        return error_response("مقاله یافت نشد")
    try:
        OutlineEditor(request.state.pb).add(article, safe_str(heading), safe_str(content_brief))
    except ValueError as e:
        return error_response(str(e))
    return success_response("بخش اضافه شد", extra_events={"refreshArticle": True})


@router.post("/projects/{project_id}/articles/{article_id}/outline/sections/{position}/delete")
@hx_error("حذف بخش ناموفق بود")
def outline_delete(request: Request, project_id: str, article_id: str, position: int):
    require_hx(request)
    require_project_access(request, project_id)
    require_project_role(request, project_id)
    article = _load_article(request, project_id, article_id)
    if not article:
        return error_response("مقاله یافت نشد")
    try:
        OutlineEditor(request.state.pb).delete(article, position)
    except ValueError as e:
        return error_response(str(e))
    return success_response("بخش حذف شد", extra_events={"refreshArticle": True})


@router.post("/projects/{project_id}/articles/{article_id}/outline/sections/{position}/brief")
@hx_error("ذخیره خلاصه ناموفق بود")
def outline_update_brief(
    request: Request,
    project_id: str,
    article_id: str,
    position: int,
    heading: str = Form(""),
    content_brief: str = Form(""),
):
    require_hx(request)
    require_project_access(request, project_id)
    require_project_role(request, project_id)
    article = _load_article(request, project_id, article_id)
    if not article:
        return error_response("مقاله یافت نشد")
    try:
        OutlineEditor(request.state.pb).update_brief(
            article, position, safe_str(heading), safe_str(content_brief)
        )
    except ValueError as e:
        return error_response(str(e))
    return success_response(
        "خلاصه بخش ذخیره شد — بخش برای بازتولید علامت‌گذاری شد",
        extra_events={"refreshArticle": True},
    )


# ---------------------------------------------------------------------------
# Review workflow
# ---------------------------------------------------------------------------
def _live_validation(pb: Any, article: dict, sections: list[dict], keyword: str):
    """Run the full validator against the CURRENT content (fresh, not cached)."""
    from app.repositories.projects import ProjectSettingsRepo

    settings = ProjectSettingsRepo(pb).get_for_project(article.get("project") or "")
    min_words = int(settings.get("minArticleWords") or 300)
    ordered = sorted(sections, key=lambda s: int(s.get("position") or 0))
    html = article.get("finalHtml") or ""
    report = ArticleValidator(min_words=min_words, keyword=keyword).validate(
        title=article.get("title") or "",
        slug=article.get("slug") or "",
        outline=article.get("outline") or {},
        sections=ordered,
        html=html,
    )
    return report


@router.get("/projects/{project_id}/articles/{article_id}/review", response_class=HTMLResponse)
@page_guard("مشکلی در بارگذاری صفحه بازبینی پیش آمد — دوباره تلاش کنید.")
def article_review(request: Request, project_id: str, article_id: str):
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
    revisions = RevisionService(request.state.pb).list(article_id, per_page=30)
    report = _live_validation(
        request.state.pb, article, sections, str(topic.get("keyword") or "") if topic else ""
    )
    internal_links = _collect_links(article)
    return templates.TemplateResponse(
        request,
        "pages/articles/review.html",
        {
            "title": f"بازبینی — {article.get('title', '')}",
            "project": project,
            "article": article,
            "sections": sections,
            "topic": topic,
            "revisions": revisions,
            "report": report,
            "internal_links": internal_links,
        },
    )


@router.post("/projects/{project_id}/articles/{article_id}/approve")
@hx_error("تأیید مقاله ناموفق بود")
def approve_article(request: Request, project_id: str, article_id: str):
    """review → approved. Only allowed when the live validation passes."""
    require_hx(request)
    require_project_access(request, project_id)
    require_project_role(request, project_id)
    article = ArticleRepo(request.state.pb).get(article_id)
    if not article or article.get("project") != project_id:
        return error_response("مقاله یافت نشد")
    if article.get("status") != "review":
        return error_response("فقط مقاله در وضعیت بازبینی قابل تأیید است")
    sections = SectionRepo(request.state.pb).list_for_article(article_id)
    topic = TopicRepo(request.state.pb).get(article.get("topicId") or "")
    report = _live_validation(
        request.state.pb, article, sections, str(topic.get("keyword") or "") if topic else ""
    )
    if not report.ok:
        issues = "; ".join(i.message for i in report.issues[:5])
        return error_response(f"مقاله اعتبارسنجی را پاس نکرد: {issues}")
    RevisionService(request.state.pb).snapshot(
        article,
        "manual",
        note="approved by reviewer",
        created_by=require_user(request).get("id", ""),
    )
    ArticleRepo(request.state.pb).set_approved(article_id)
    return success_response(
        "مقاله تأیید شد — انتشار فعال شد", extra_events={"refreshArticle": True}
    )


@router.post("/projects/{project_id}/articles/{article_id}/send-back")
@hx_error("بازگرداندن مقاله ناموفق بود")
def send_back_article(request: Request, project_id: str, article_id: str, note: str = Form("")):
    """review/approved → sent_back with reviewer feedback (history preserved)."""
    require_hx(request)
    require_project_access(request, project_id)
    require_project_role(request, project_id)
    article = ArticleRepo(request.state.pb).get(article_id)
    if not article or article.get("project") != project_id:
        return error_response("مقاله یافت نشد")
    if article.get("status") not in ("review", "approved"):
        return error_response("مقاله در وضعیت قابل بازگشت نیست")
    RevisionService(request.state.pb).snapshot(
        article,
        "manual",
        note=f"sent back: {note}",
        created_by=require_user(request).get("id", ""),
    )
    ArticleRepo(request.state.pb).send_back(article_id, safe_str(note))
    return success_response("مقاله بازگردانده شد", extra_events={"refreshArticle": True})


@router.post("/projects/{project_id}/articles/{article_id}/revisions/{revision_id}/rollback")
@hx_error("بازگشت به نسخه ناموفق بود")
def rollback_article(request: Request, project_id: str, article_id: str, revision_id: str):
    require_hx(request)
    require_project_access(request, project_id)
    require_project_role(request, project_id)
    user = require_user(request)
    try:
        RevisionService(request.state.pb).rollback(
            article_id, revision_id, created_by=user.get("id", "")
        )
    except ValueError as e:
        return error_response(str(e))
    ArticleRepo(request.state.pb).set_status(article_id, "review")
    return success_response("بازگشت به نسخه انجام شد", extra_events={"refreshArticle": True})
