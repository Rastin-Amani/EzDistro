"""WordPress → EZDistro content mirror + indexing reconciliation.

Existing WordPress posts become first-class rows in the SAME `articles`
collection as generated drafts (`source` distinguishes them), so the Articles
section shows one unified inventory instead of a separate "legacy content" silo.

Authority rules (see docs/SEO_RESEARCH.md):
- remote identity is `wordpressPostId` — never title/slug/URL, which can change;
- remote status/metadata always refreshes;
- remote content only overwrites an article that is NOT being worked on locally
  (draft/published mirrors). Work-in-progress articles are flagged
  `syncStatus="update_available"` and a human decides — local edits are never
  silently destroyed;
- a post deleted on WordPress keeps its local history and is flagged
  `remoteStatus="deleted"`, so it can never silently stay marked as published.

Cost model: one cheap paged inventory sweep (`id,status,modified` only) per run,
then a full fetch ONLY for posts that are new or whose remote `modified` string
changed. No full-content download of unchanged posts.
"""

from __future__ import annotations

import hashlib
import time
from typing import Any

from app.domain.chunker import strip_html
from app.jobs.context import JobContext
from app.jobs.handlers import register_job
from app.providers.base import PublisherProvider, WPPost
from app.repositories.articles import ArticleRepo
from app.repositories.jobs import JobRepo
from app.repositories.topics import TopicRepo

# States in which a mirrored article is being worked on locally: remote content
# must not clobber them (the change is flagged as an update opportunity instead).
LOCAL_WORK_STATES = frozenset(
    {
        "outline_ready",
        "generating",
        "review",
        "ready_to_publish",
        "approved",
        "sent_back",
        "publishing",
        "failed",
    }
)

# WordPress post status → EZDistro article status (only for non-work-in-progress rows).
REMOTE_STATUS_MAP = {"publish": "published", "future": "ready_to_publish"}
MAX_INDEX_DOCUMENT_JOBS = 25  # above this, one index_project job is cheaper than N jobs
CONTENT_MAX = 100_000  # articles.finalHtml cap


def _normalize(text: str) -> str:
    return " ".join((text or "").split()).lower()


def text_hash(title: str, content_html: str) -> str:
    """Deterministic fingerprint of mirrored content (title + readable text)."""
    payload = f"{_normalize(title)}\n{_normalize(strip_html(content_html))}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _remote_meta(post: WPPost) -> dict[str, Any]:
    return {
        # Raw WordPress `modified` string: the change detector compares THIS value
        # (PB normalizes the counterpart `date` field, so string equality there
        # would be unreliable across timezones).
        "modified": post.modified,
        "modifiedGmt": post.modified_gmt,
        "date": post.date,
        "author": post.author,
        "featuredMedia": post.featured_media,
        "categories": post.categories,
        "tags": post.tags,
    }


def _remote_data(post: WPPost) -> dict[str, Any]:
    """Remote-owned columns, always refreshed on sync."""
    digest = text_hash(post.title, post.content_html)
    return {
        "source": "wordpress",
        "wordpressPostId": int(post.id),
        "wordpressUrl": post.link,
        "remoteStatus": post.status,
        "remoteModified": post.modified or None,
        "remoteContentHash": digest,
        "remoteSlug": post.slug,
        "remoteExcerpt": (post.excerpt or "")[:4000],
        "remoteMeta": _remote_meta(post),
        "contentHash": digest,
    }


def _content_data(post: WPPost) -> dict[str, Any]:
    """Local content columns: only written when the local article is not in use."""
    html = post.content_html or ""
    return {
        "title": post.title or post.slug or f"WordPress post {post.id}",
        "slug": post.slug,
        "finalHtml": html[:CONTENT_MAX],
        "wordCount": len(strip_html(html).split()),
        "generatedAt": None,
    }


async def _mirror_post(
    *,
    articles: ArticleRepo,
    topics: TopicRepo,
    project_id: str,
    post: WPPost,
    existing: dict[str, Any] | None,
) -> str:
    """Create or refresh one mirrored article. Returns created|updated|unchanged|flagged."""
    data = _remote_data(post)
    digest = data["contentHash"]

    if existing is None:
        topic = topics.create(
            project=project_id,
            title=post.title or post.slug or f"WordPress post {post.id}",
            url=post.link,
            type="article",
        )
        topics.set_status(topic["id"], "published" if post.status == "publish" else "planned")
        article = articles.create(
            project=project_id,
            topic_id=topic["id"],
            title=post.title or post.slug or f"WordPress post {post.id}",
            slug=post.slug,
        )
        topics.link_article(topic["id"], article["id"])
        articles.update(article["id"], {**data, **_content_data(post), "syncStatus": "synced"})
        _set_article_remote_state(articles, article["id"], post)
        return "created"

    if existing.get("status") in LOCAL_WORK_STATES:
        # Do not overwrite work in progress — flag it instead.
        if existing.get("contentHash") != digest:
            articles.update(existing["id"], {**data, "syncStatus": "update_available"})
            return "flagged"
        if existing.get("syncStatus") != "synced":
            articles.update(existing["id"], {"syncStatus": "synced"})
        return "unchanged"

    changed = existing.get("contentHash") != digest
    payload = {**data, **_content_data(post), "syncStatus": "synced"}
    if not existing.get("metaDescription"):
        excerpt = (post.excerpt or "").strip()
        if excerpt:
            payload["metaDescription"] = excerpt[:300]
    articles.update(existing["id"], payload)
    _set_article_remote_state(articles, existing["id"], post)
    return "updated" if changed else "unchanged"


def _set_article_remote_state(articles: ArticleRepo, article_id: str, post: WPPost) -> None:
    """Reflect the remote publish state (and published date) on the local row."""
    if post.status == "publish":
        articles.mark_published(article_id, int(post.id), post.link)
        if post.date:
            articles.update(article_id, {"publishedAt": post.date})
    else:
        articles.set_status(article_id, REMOTE_STATUS_MAP.get(post.status, "draft"))
        articles.update(article_id, {"wordpressPostId": int(post.id)})


def _queue_indexing(ctx: JobContext, post_ids: list[int], *, force: bool) -> str:
    """Hand changed posts to the EXISTING indexing pipeline."""
    if not post_ids:
        return ""
    jobs = JobRepo(ctx.pb)
    if len(post_ids) > MAX_INDEX_DOCUMENT_JOBS:
        job = jobs.create(
            project=ctx.project_id,
            type="index_project",
            payload={"trigger": "manual", "full": False, "force": force},
            idempotency_key=f"index_project:sync:{ctx.project_id}:{int(time.time())}",
        )
        return job["id"]
    for post_id in post_ids:
        jobs.create(
            project=ctx.project_id,
            type="index_document",
            payload={"sourceId": str(post_id), "force": force},
            entity_type="article",
            entity_id=str(post_id),
            idempotency_key=f"index_document:sync:{ctx.project_id}:{post_id}:{int(time.time() // 60)}",
        )
    return ""


async def sync_posts(
    ctx: JobContext,
    publisher: PublisherProvider,
    *,
    force: bool = False,
) -> dict[str, Any]:
    """Mirror WordPress posts into `articles`; queue indexing for what changed."""
    project_id = ctx.project_id
    articles = ArticleRepo(ctx.pb)
    topics = TopicRepo(ctx.pb)

    # 1. Cheap remote inventory: identity + status + modified only.
    remote: dict[int, WPPost] = {}
    after_id: int | None = None
    ctx.progress(2, stage="wordpress_sync", message="Listing WordPress posts…")
    while True:
        await ctx.check_cancelled()
        batch = await publisher.list_posts(
            per_page=100,
            after_id=after_id,
            status="any",
            fields=["id", "status", "modified"],
        )
        if not batch:
            break
        for item in batch:
            remote[int(item.id)] = item
        after_id = max(int(item.id) for item in batch)
        ctx.progress(
            2,
            stage="wordpress_sync",
            message=f"Found {len(remote)} WordPress posts…",
            current=len(remote),
            total=len(remote),
        )
    ctx.info("wordpress inventory collected", {"posts": len(remote)})

    # 2. Local mirrors (projected columns — never load article bodies in bulk).
    local_rows = articles.list_all(
        filter=f'project="{project_id}" && source="wordpress"',
        fields="id,title,status,wordpressPostId,remoteStatus,remoteMeta,contentHash,metaDescription",
    )
    local: dict[int, dict[str, Any]] = {
        int(row["wordpressPostId"]): row for row in local_rows if row.get("wordpressPostId")
    }

    # 3. Deletions / remote-unpublished-missing posts.
    deleted = 0
    for post_id, row in local.items():
        if post_id in remote:
            continue
        if row.get("remoteStatus") == "deleted":
            continue
        articles.update(row["id"], {"remoteStatus": "deleted", "syncStatus": "remote_deleted"})
        deleted += 1

    # 4. Full fetch only for new / remotely-modified posts.
    pending: list[int] = []
    for post_id, item in remote.items():
        known_row = local.get(post_id)
        if force or known_row is None:
            pending.append(post_id)
            continue
        known = (known_row.get("remoteMeta") or {}).get("modified") or ""
        if (item.modified or "") != known:
            pending.append(post_id)

    created = updated = unchanged = flagged = failed = 0
    index_ids: list[int] = []
    for position, post_id in enumerate(pending, start=1):
        await ctx.check_cancelled()
        post = await publisher.get_post(post_id)
        if post is None:  # deleted between the inventory sweep and now
            failed += 1
            continue
        try:
            outcome = await _mirror_post(
                articles=articles,
                topics=topics,
                project_id=project_id,
                post=post,
                existing=local.get(post_id),
            )
        except Exception as exc:  # one bad post must not abort the whole sync
            failed += 1
            ctx.warning(f"wordpress post {post_id} failed to sync", {"error": str(exc)})
            continue
        if outcome == "created":
            created += 1
        elif outcome == "updated":
            updated += 1
        elif outcome == "flagged":
            flagged += 1
        else:
            unchanged += 1
        if outcome in ("created", "updated") and post.status == "publish":
            index_ids.append(post_id)
        ctx.progress(
            5 + int(60 * position / max(1, len(pending))),
            stage="wordpress_sync",
            message=f"Syncing post {position}/{len(pending)}…",
            current=position,
            total=len(pending),
        )

    index_job = _queue_indexing(ctx, index_ids, force=force)
    totals = {
        "remote": len(remote),
        "scanned": len(pending),
        "created": created,
        "updated": updated,
        "unchanged": unchanged,
        "flagged": flagged,
        "deleted": deleted,
        "failed": failed,
        "indexed": len(index_ids),
        "indexJobId": index_job,
    }
    ctx.progress(68, stage="wordpress_sync", message="WordPress sync complete")
    return totals


@register_job("wordpress_sync")
async def handle_wordpress_sync(ctx: JobContext) -> dict[str, Any]:
    publisher = ctx.registry.get_publisher_provider(ctx.config.project, ctx.config.settings)
    if publisher is None:
        raise ValueError("no WordPress publisher integration is configured for this project")
    payload = ctx.payload()
    return await sync_posts(ctx, publisher, force=bool(payload.get("force")))
