"""Publishing pipeline (job handler "publish_article").

Safe, idempotent WordPress publishing:

- BEFORE creating a post, the article's stored WordPress post ID is checked:
  when it exists → UPDATE the existing post (content refresh, never a
  duplicate); only when absent → CREATE.
- Every attempt is recorded in append-only `publishing_runs` with a request
  ID, attempt number, WordPress post ID, URL, HTTP response status, and
  timestamps; failures are recorded there too.
- Publishing is only possible from `approved` (or a `failed` retry when no
  successful publish exists).
- `unpublish` safely sets the post to private (WP has no true unpublish).
"""

from __future__ import annotations

import re
import time
import uuid
from typing import Any

from app.domain.article_html import slugify
from app.i18n import _
from app.jobs.context import JobContext
from app.jobs.handlers import register_job
from app.providers.base import ProviderError
from app.repositories.articles import ArticleRepo
from app.repositories.jobs import JobRepo
from app.repositories.publishing_runs import PublishingRunRepo
from app.repositories.topics import TopicRepo

PUBLISH_ACTIONS = ("publish", "update", "unpublish")

# WordPress themes render the post title as their own <h1>; drop the
# article's leading <h1> (added by build_article_html) so the published
# page has exactly one header instead of a duplicated one.
_LEADING_H1 = re.compile(r"^\s*<h1[^>]*>.*?</h1>\s*", re.DOTALL)


def strip_leading_h1(html: str) -> str:
    return _LEADING_H1.sub("", html, count=1)


@register_job("publish_article")
async def handle_publish_article(ctx: JobContext) -> dict[str, Any]:
    article_id = (ctx.payload().get("articleId") or "").strip()
    action = (ctx.payload().get("action") or "publish").strip()
    if not article_id:
        raise ValueError("publish_article payload is missing articleId")
    if action not in PUBLISH_ACTIONS:
        raise ValueError(f"unknown publish action: {action}")

    articles = ArticleRepo(ctx.pb)
    runs = PublishingRunRepo(ctx.pb)

    article = articles.get(article_id)
    if not article:
        raise ValueError(f"article not found: {article_id}")

    if action == "unpublish":
        return await _unpublish(ctx, articles, runs, article, article_id)

    # Workflow gate: publishing requires approval (retries from a failed state
    # are allowed only when nothing was ever successfully published).
    if article.get("status") == "published" and action == "update":
        pass  # content refresh on a live post is always safe
    elif article.get("status") == "approved":
        pass
    elif article.get("status") == "failed" and not runs.already_published(article_id):
        pass  # retry of a failed first publish
    elif article.get("status") == "publishing" and not runs.already_published(article_id):
        # Crash-recovery: a previous attempt of THIS job died mid-publish and
        # left the article stuck in "publishing". Retrying the same job is
        # safe: publishing runs are idempotent and the WP ezdistro_article_id meta
        # lookup prevents duplicate posts. Other jobs are still refused.
        if not runs.first(filter=f'article="{article_id}" && job="{ctx.job_id}"'):
            raise ProviderError(
                "article is not approved for publishing (current state: %s)"
                % (article.get("status") or "?"),
                retryable=False,
                details={
                    "article": article_id,
                    "required_state": "approved",
                    "current_state": article.get("status"),
                },
            )
    else:
        raise ProviderError(
            "article is not approved for publishing (current state: %s)"
            % (article.get("status") or "?"),
            retryable=False,
            details={
                "article": article_id,
                "required_state": "approved",
                "current_state": article.get("status"),
            },
        )

    html = article.get("finalHtml") or ""
    if not html:
        raise ValueError("article has no content — run the writer first")

    # WP renders the post title itself — avoid the duplicated <h1>.
    html = strip_leading_h1(html)

    mode = ctx.config.publishing["mode"]
    request_id = uuid.uuid4().hex[:16]
    articles.set_status(article_id, "publishing")
    record = runs.start(
        project=ctx.project_id,
        article=article_id,
        job=ctx.job_id,
        mode=mode,
        request_id=request_id,
        attempt=runs.next_attempt(article_id),
    )

    ctx.stage_started("publishing", _("Sending to WordPress…"))
    ctx.progress(20, stage="publishing", message=_("Sending to WordPress…"))
    try:
        publisher = ctx.registry.get_publisher_provider(ctx.config.project, ctx.config.settings)

        # Images: pre-publish validation, idempotent WP media upload, placeholder
        # resolution. Missing cover blocks publishing unless the user explicitly
        # overrides (publishWithoutCover=true in the job payload).
        from app.services.images import apply_images_to_html

        html, featured_media_id = await apply_images_to_html(
            ctx,
            article,
            html,
            publisher,
            allow_missing_cover=bool(ctx.payload().get("publishWithoutCover")),
        )
        existing_wp_id = int(article.get("wordpressPostId") or 0)
        if existing_wp_id:
            # Safe publishing: UPDATE the existing post instead of creating a duplicate.
            result = await publisher.update_post(
                existing_wp_id,
                title=article.get("title") or _("Untitled"),
                html=html,
                status=mode,
                slug=article.get("slug") or slugify(article.get("title") or "post"),
                excerpt=article.get("metaDescription") or "",
            )
            ctx.info(
                "wordpress post updated (no duplicate created)",
                {"postId": existing_wp_id, "requestId": request_id},
            )
        else:
            # Crash-recovery: a previous attempt may have created the post but
            # died BEFORE the post id was stored. Find it by the article slug
            # (WP REST `slug` filter) and UPDATE it instead of duplicating.
            orphan = None
            find = getattr(publisher, "find_post_by_slug", None)
            if find is not None:
                try:
                    orphan = await find(
                        article.get("slug") or slugify(article.get("title") or "post")
                    )
                except Exception:
                    orphan = None
            if orphan is not None and orphan.id:
                result = await publisher.update_post(
                    orphan.id,
                    title=article.get("title") or _("Untitled"),
                    html=html,
                    status=mode,
                    slug=article.get("slug") or slugify(article.get("title") or "post"),
                    excerpt=article.get("metaDescription") or "",
                )
                ctx.info(
                    "orphaned post recovered via meta (updated, no duplicate)",
                    {"postId": orphan.id, "requestId": request_id},
                )
                existing_wp_id = orphan.id
            else:
                result = await publisher.create_post(
                    title=article.get("title") or _("Untitled"),
                    html=html,
                    status=mode,
                    slug=article.get("slug") or slugify(article.get("title") or "post"),
                    meta={"ezdistro_article_id": article_id, "ezdistro_request_id": request_id},
                    excerpt=article.get("metaDescription") or "",
                )
        if featured_media_id:
            # cover image → WP featured image (drives og:image in the theme)
            await publisher.set_featured_media(result.post_id, featured_media_id)
    except Exception as exc:
        runs.mark_failed(
            record["id"], {"type": type(exc).__name__, "message": str(exc), "requestId": request_id}
        )
        articles.set_status(article_id, "failed")
        raise

    runs.mark_published(
        record["id"],
        result.post_id,
        result.link,
        response_metadata={
            "requestId": request_id,
            "responseStatus": result.status_code,
            "mode": mode,
            "link": result.link,
        },
    )
    articles.mark_published(article_id, result.post_id, result.link)
    topic = article.get("topicId")
    if topic:
        TopicRepo(ctx.pb).set_status(topic, "published")

    # Make the live post available to retrieval immediately: enqueue a
    # per-document reindex of the WordPress post (idempotent by content hash).
    # Draft-mode pushes are skipped — draft content must not enter the corpus.
    if mode == "publish":
        JobRepo(ctx.pb).create(
            project=ctx.project_id,
            type="index_document",
            payload={"sourceId": str(result.post_id), "force": False},
            idempotency_key=(
                f"index:document:{ctx.project_id}:{result.post_id}:{int(time.time())}"
            ),
            max_attempts=3,
            entity_type="article",
            entity_id=article_id,
        )

    ctx.stage_completed("publishing", _("Article updated on WordPress"))
    ctx.progress(100, stage="done", message=_("Article updated on WordPress"))
    ctx.info(
        "wordpress publish finished",
        {
            "postId": result.post_id,
            "url": result.link,
            "requestId": request_id,
            "updated": bool(existing_wp_id),
        },
    )
    return {
        "articleId": article_id,
        "postId": result.post_id,
        "url": result.link,
        "requestId": request_id,
        "updated": bool(existing_wp_id),
    }


async def _unpublish(
    ctx: JobContext,
    articles: ArticleRepo,
    runs: PublishingRunRepo,
    article: dict[str, Any],
    article_id: str,
) -> dict[str, Any]:
    """Safely unpublish: set the WP post to private (reversible)."""
    wp_id = int(article.get("wordpressPostId") or 0)
    if not wp_id:
        raise ProviderError(
            "article has no WordPress post to unpublish",
            retryable=False,
            details={"article": article_id},
        )
    request_id = uuid.uuid4().hex[:16]
    record = runs.start(
        project=ctx.project_id,
        article=article_id,
        job=ctx.job_id,
        mode="unpublish",
        request_id=request_id,
        attempt=runs.next_attempt(article_id),
    )
    publisher = ctx.registry.get_publisher_provider(ctx.config.project, ctx.config.settings)
    try:
        result = await publisher.unpublish_post(wp_id)
    except Exception as exc:
        runs.mark_failed(
            record["id"], {"type": type(exc).__name__, "message": str(exc), "requestId": request_id}
        )
        raise
    runs.mark_unpublished(
        record["id"], {"requestId": request_id, "responseStatus": result.status_code}
    )
    ctx.info("wordpress post unpublished (private)", {"postId": wp_id, "requestId": request_id})
    return {"articleId": article_id, "postId": wp_id, "requestId": request_id, "unpublished": True}
