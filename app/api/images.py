"""Article images — workspace actions (plan, generate, versions, metadata, WP upload).

All mutations are HTMX POSTs that enqueue persistent jobs; the heavy lifting
(provider calls, optimization, WordPress upload) happens in the worker process
via the image job handlers. Images render straight from PocketBase files
(public, unlisted URLs) — no proxy route needed.
"""

from __future__ import annotations

import re
import time

from fastapi import APIRouter, Form, Request

from app.api.deps import (
    require_hx,
    require_project_access,
    require_project_role,
    safe_bool,
    safe_int,
    safe_str,
)
from app.api.errors import hx_error
from app.i18n import _
from app.repositories.article_images import ArticleImageRepo
from app.repositories.articles import ArticleRepo
from app.repositories.jobs import JobRepo
from app.repositories.projects import ProjectSettingsRepo
from app.utils import error_response, success_response

router = APIRouter()

_ACTIVE_FILTER = '(status="pending" || status="retrying" || status="running")'
_SECTION_KEY_RE = re.compile(r"^section-\d+$")


def _load_article(request: Request, project_id: str, article_id: str) -> dict | None:
    article = ArticleRepo(request.state.pb).get(article_id)
    if not article or article.get("project") != project_id:
        return None
    return article


def _load_image(request: Request, project_id: str, article_id: str, image_id: str) -> dict | None:
    row = ArticleImageRepo(request.state.pb).get(image_id)
    if not row or str(row.get("project") or "") != project_id or row.get("article") != article_id:
        return None
    return row


def _queue_job(
    request: Request,
    project_id: str,
    *,
    job_type: str,
    payload: dict,
    idempotency_key: str,
    article_id: str,
    max_attempts: int = 3,
) -> None:
    JobRepo(request.state.pb).create(
        project=project_id,
        type=job_type,
        payload=payload,
        idempotency_key=idempotency_key,
        max_attempts=max_attempts,
        entity_type="article",
        entity_id=article_id,
    )


def _image_max_attempts(request: Request, project_id: str) -> int:
    settings = ProjectSettingsRepo(request.state.pb).get_for_project(project_id)
    return max(2, safe_int(settings.get("imageMaxRetries"), 3))


# ---------------------------------------------------------------------------
# Planning
# ---------------------------------------------------------------------------
@router.post("/projects/{project_id}/articles/{article_id}/images/plan")
@hx_error("Scheduling the image plan failed")
def queue_image_plan(request: Request, project_id: str, article_id: str):
    require_hx(request)
    require_project_access(request, project_id)
    require_project_role(request, project_id)
    article = _load_article(request, project_id, article_id)
    if not article:
        return error_response(_("Article not found"))
    if not (article.get("finalHtml") or article.get("generatedContent")):
        return error_response(_("The article has no content yet — generate the article first"))
    active = JobRepo(request.state.pb).first(
        filter=(
            f'type="plan_article_images" && payload.articleId="{article_id}" && {_ACTIVE_FILTER}'
        )
    )
    if active:
        return success_response(
            _("The image plan is already being processed — the page refreshes automatically"),
            extra_events={"refreshArticle": True},
        )
    _queue_job(
        request,
        project_id,
        job_type="plan_article_images",
        payload={"articleId": article_id},
        idempotency_key=f"image:plan:{article_id}:{int(time.time())}",
        article_id=article_id,
        max_attempts=3,
    )
    return success_response(
        _("Image plan queued for processing"),
        extra_events={"refreshArticle": True, "refreshJobs": True},
    )


# ---------------------------------------------------------------------------
# Generation (new version per run)
# ---------------------------------------------------------------------------
def _job_type_for(role: str) -> str:
    if role == "cover":
        return "generate_cover_image"
    if role == "interior":
        return "generate_interior_image"
    raise ValueError(f"invalid image role: {role}")


def _queue_generation(
    request: Request,
    project_id: str,
    article_id: str,
    *,
    role: str,
    section_key: str,
    message: str,
):
    job_type = _job_type_for(role)
    jobs = JobRepo(request.state.pb)
    active = jobs.first(
        filter=(
            f'type="{job_type}" && payload.articleId="{article_id}" '
            f'&& payload.sectionKey="{section_key}" && {_ACTIVE_FILTER}'
        )
    )
    if active:
        return success_response(
            _("This image is already queued for generation — the page refreshes automatically"),
            extra_events={"refreshArticle": True},
        )
    version = ArticleImageRepo(request.state.pb).next_version(article_id, role, section_key)
    _queue_job(
        request,
        project_id,
        job_type=job_type,
        payload={
            "articleId": article_id,
            "role": role,
            "sectionKey": section_key,
            "version": version,
        },
        idempotency_key=f"image:{role}:{section_key}:{article_id}:v{version}",
        article_id=article_id,
        max_attempts=_image_max_attempts(request, project_id),
    )
    return success_response(message, extra_events={"refreshArticle": True, "refreshJobs": True})


@router.post("/projects/{project_id}/articles/{article_id}/images/generate")
@hx_error("Image generation failed")
def queue_image_generate(
    request: Request,
    project_id: str,
    article_id: str,
    role: str = Form(""),
    section_key: str = Form(""),
):
    require_hx(request)
    require_project_access(request, project_id)
    require_project_role(request, project_id)
    article = _load_article(request, project_id, article_id)
    if not article:
        return error_response(_("Article not found"))
    if not article.get("imagePlan"):
        return error_response(_("Generate the image plan first"))
    role = safe_str(role)
    if role == "cover":
        section_key = "cover"
    elif role == "interior":
        section_key = safe_str(section_key)
        if not _SECTION_KEY_RE.match(section_key):
            return error_response(_("Invalid section key — it must be in the form section-N"))
    else:
        return error_response(_("Invalid image role"))
    return _queue_generation(
        request,
        project_id,
        article_id,
        role=role,
        section_key=section_key,
        message=_("Image generation queued"),
    )


@router.post("/projects/{project_id}/articles/{article_id}/images/{image_id}/regenerate")
@hx_error("Image regeneration failed")
def queue_image_regenerate(request: Request, project_id: str, article_id: str, image_id: str):
    """Regenerate = a NEW version row; the successful old version stays for compare/rollback."""
    require_hx(request)
    require_project_access(request, project_id)
    require_project_role(request, project_id)
    if not _load_article(request, project_id, article_id):
        return error_response(_("Article not found"))
    row = _load_image(request, project_id, article_id, image_id)
    if not row:
        return error_response(_("Image not found"))
    role = str(row.get("role") or "")
    section_key = str(row.get("sectionKey") or ("cover" if role == "cover" else ""))
    return _queue_generation(
        request,
        project_id,
        article_id,
        role=role,
        section_key=section_key,
        message=_("Image regeneration queued (new version)"),
    )


# ---------------------------------------------------------------------------
# Version selection / removal / metadata
# ---------------------------------------------------------------------------
@router.post("/projects/{project_id}/articles/{article_id}/images/{image_id}/select")
@hx_error("Selecting the version failed")
def select_image_version(request: Request, project_id: str, article_id: str, image_id: str):
    require_hx(request)
    require_project_access(request, project_id)
    require_project_role(request, project_id)
    row = _load_image(request, project_id, article_id, image_id)
    if not row:
        return error_response(_("Image not found"))
    if row.get("status") != "ready":
        return error_response(_("Only ready versions can be selected"))
    ArticleImageRepo(request.state.pb).set_active(image_id)
    return success_response(
        _("This image version was selected"), extra_events={"refreshArticle": True}
    )


@router.post("/projects/{project_id}/articles/{article_id}/images/{image_id}/remove")
@hx_error("Deleting the image failed")
def remove_image(request: Request, project_id: str, article_id: str, image_id: str):
    """Deactivate (never a hard delete): history stays for rollback/audit."""
    require_hx(request)
    require_project_access(request, project_id)
    require_project_role(request, project_id)
    row = _load_image(request, project_id, article_id, image_id)
    if not row:
        return error_response(_("Image not found"))
    ArticleImageRepo(request.state.pb).deactivate(image_id)
    return success_response(
        _("Image removed from the article (the version stays in history)"),
        extra_events={"refreshArticle": True},
    )


@router.post("/projects/{project_id}/articles/{article_id}/images/{image_id}/metadata")
@hx_error("Saving image info failed")
def update_image_metadata(
    request: Request,
    project_id: str,
    article_id: str,
    image_id: str,
    alt_text: str = Form(""),
    caption: str = Form(""),
):
    require_hx(request)
    require_project_access(request, project_id)
    require_project_role(request, project_id)
    row = _load_image(request, project_id, article_id, image_id)
    if not row:
        return error_response(_("Image not found"))
    alt = safe_str(alt_text)
    if not alt:
        return error_response(_("Alt text cannot be empty"))
    try:
        ArticleImageRepo(request.state.pb).update_metadata(
            image_id, alt_text=alt, caption=safe_str(caption) or None
        )
    except ValueError as exc:
        return error_response(str(exc))
    return success_response(_("Image info saved"), extra_events={"refreshArticle": True})


# ---------------------------------------------------------------------------
# Re-optimization + single-image WordPress upload
# ---------------------------------------------------------------------------
@router.post("/projects/{project_id}/articles/{article_id}/images/{image_id}/optimize")
@hx_error("Image optimization failed")
def queue_image_optimize(request: Request, project_id: str, article_id: str, image_id: str):
    require_hx(request)
    require_project_access(request, project_id)
    require_project_role(request, project_id)
    row = _load_image(request, project_id, article_id, image_id)
    if not row:
        return error_response(_("Image not found"))
    if not row.get("sourceFile"):
        return error_response(_("This image has no source file — generate it first"))
    active = JobRepo(request.state.pb).first(
        filter=(
            f'type="optimize_article_image" && payload.imageId="{image_id}" && {_ACTIVE_FILTER}'
        )
    )
    if active:
        return success_response(
            _("Optimization is already queued"), extra_events={"refreshArticle": True}
        )
    _queue_job(
        request,
        project_id,
        job_type="optimize_article_image",
        payload={"imageId": image_id},
        idempotency_key=f"image:optimize:{image_id}:{int(time.time())}",
        article_id=article_id,
        max_attempts=3,
    )
    return success_response(
        _("Image re-optimization queued"),
        extra_events={"refreshArticle": True, "refreshJobs": True},
    )


@router.post("/projects/{project_id}/articles/{article_id}/images/{image_id}/publish")
@hx_error("Uploading the image to WordPress failed")
def queue_image_publish(
    request: Request,
    project_id: str,
    article_id: str,
    image_id: str,
    featured: str = Form(""),
):
    """Upload ONE image to WordPress (idempotent — reuses the stored media id)."""
    require_hx(request)
    require_project_access(request, project_id)
    require_project_role(request, project_id)
    row = _load_image(request, project_id, article_id, image_id)
    if not row:
        return error_response(_("Image not found"))
    if row.get("status") != "ready":
        return error_response(_("Only ready images can be uploaded"))
    _queue_job(
        request,
        project_id,
        job_type="publish_article_image",
        payload={"imageId": image_id, "featured": safe_bool(featured)},
        idempotency_key=f"image:wp:{image_id}:{int(time.time())}",
        article_id=article_id,
        max_attempts=3,
    )
    return success_response(
        _("Image upload to WordPress queued"),
        extra_events={"refreshArticle": True, "refreshJobs": True},
    )
