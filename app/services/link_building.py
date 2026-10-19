"""Inbound internal link building — the half the writer cannot do for itself.

A new article ships with *outbound* internal links (the sections carry them and
the assembly appends a "Related" block). What it cannot do is make the older
published pages link back to it. That is this job, and it is exactly what the
retrieval store is for: ask it which already-published pages are most related to
the new article, and add ONE contextual link to each of them.

The insertion is **text-preserving by construction** — no LLM, no rewriting. It
only ever wraps an existing occurrence of the new article's keyword or title
inside a paragraph's text node, and a text node that is already inside an anchor
is never touched. A page with no safe occurrence, or one that already links to
the target, is skipped.

The new article itself is untouched: it already links outwards.
"""

from __future__ import annotations

import re
from typing import Any

from app.jobs.context import JobContext
from app.jobs.handlers import register_job
from app.repositories.articles import ArticleRepo
from app.repositories.jobs import JobRepo
from app.repositories.topics import TopicRepo

MAX_INBOUND_PAGES = 3  # ponytail: fixed cap, make it a project setting when it bites
_TAG = re.compile(r"<[^>]+>")
_OPEN_ANCHOR = re.compile(r"<a[\s>]", re.IGNORECASE)
_CLOSE_ANCHOR = re.compile(r"</a\s*>", re.IGNORECASE)


def _phrase_hit(segment: str, phrases: list[str]) -> tuple[int, int] | None:
    """First whole-word occurrence of any phrase, longest phrase first."""
    for phrase in sorted(phrases, key=len, reverse=True):
        if not phrase.strip():
            continue
        match = re.search(rf"\b{re.escape(phrase)}\b", segment, re.IGNORECASE)
        if match:
            return match.start(), match.end()
    return None


def insert_contextual_link(html: str, url: str, phrases: list[str]) -> str | None:
    """Wrap one existing occurrence of ``phrases`` in a link to ``url``.

    Returns the new html, or None when there is no safe place for a link. Only
    text nodes outside ``<a>…</a>`` are searched, so markup cannot be corrupted,
    a link can never be nested, and no existing text is modified.
    """
    if not html or not url:
        return None
    href = url.replace("&", "&amp;").replace('"', "&quot;")
    depth = 0
    pos = 0
    for tag in _TAG.finditer(html):
        if depth == 0 and tag.start() > pos:
            hit = _phrase_hit(html[pos : tag.start()], phrases)
            if hit is not None:
                start, end = pos + hit[0], pos + hit[1]
                return f'{html[:start]}<a href="{href}">{html[start:end]}</a>{html[end:]}'
        raw = tag.group(0)
        if _OPEN_ANCHOR.match(raw):
            depth += 1
        elif _CLOSE_ANCHOR.match(raw):
            depth = max(0, depth - 1)
        pos = tag.end()
    if depth == 0 and pos < len(html):
        hit = _phrase_hit(html[pos:], phrases)
        if hit is not None:
            start, end = pos + hit[0], pos + hit[1]
            return f'{html[:start]}<a href="{href}">{html[start:end]}</a>{html[end:]}'
    return None


def _same_url(left: str, right: str) -> bool:
    return left.rstrip("/").lower() == right.rstrip("/").lower()


@register_job("build_internal_links")
async def handle_build_internal_links(ctx: JobContext) -> dict[str, Any]:
    """Add one inbound internal link to the newest article's related pages."""
    article_id = str(ctx.payload().get("articleId") or "")
    if not article_id:
        raise ValueError("build_internal_links payload is missing articleId")

    article = ArticleRepo(ctx.pb).get(article_id)
    if not article:
        raise ValueError(f"article not found: {article_id}")
    target_url = str(article.get("wordpressUrl") or "")
    if article.get("status") != "published" or not target_url:
        ctx.info("link building skipped: not published", {"article": article_id})
        return {"linked": 0, "skipped": "not published"}

    topic = TopicRepo(ctx.pb).get(str(article.get("topicId") or ""))
    keyword = str((topic or {}).get("keyword") or "")
    phrases = [p for p in (keyword, str(article.get("title") or "")) if p.strip()]
    retrieval = ctx.config.retrieval
    if not phrases or not retrieval.get("top_k"):
        ctx.info("link building skipped: nothing to match on", {"article": article_id})
        return {"linked": 0, "skipped": "no keyword/retrieval"}

    from app.schemas.retrieval import RetrievalOptions
    from app.services.retrieval import RetrievalService

    query = f"{article.get('title') or ''} {keyword}".strip()
    options = RetrievalOptions(
        candidate_count=int(retrieval["top_k"]),
        similarity_threshold=float(retrieval["similarity_threshold"]) or None,
        rerank=False,
        rerank_top_n=int(retrieval["rerank_top_n"]),
    )
    results = await RetrievalService(ctx.pb, ctx.registry).retrieve(ctx.config, query, options)

    publisher = ctx.registry.get_publisher_provider(ctx.config.project, ctx.config.settings)
    jobs = JobRepo(ctx.pb)
    linked: list[dict[str, Any]] = []
    skipped = 0
    for result in results:
        if len(linked) >= MAX_INBOUND_PAGES:
            break
        url = str(result.source_url or "")
        if not url or _same_url(url, target_url):
            continue
        try:
            post_id = int(result.metadata.get("source_id") or 0)
        except (TypeError, ValueError):
            post_id = 0
        if not post_id:
            skipped += 1
            continue
        post = await publisher.get_post(post_id)
        if post is None or post.status != "publish" or target_url in post.content_html:
            skipped += 1
            continue
        updated = insert_contextual_link(post.content_html, target_url, phrases)
        if updated is None:
            skipped += 1
            continue
        # The post is live: only the content field is sent, and only one link is
        # added. Everything else about the post is left exactly as it was.
        await publisher.update_post(post_id, html=updated)
        jobs.create(
            project=ctx.project_id,
            type="index_document",
            payload={"sourceId": str(post_id), "force": False},
            idempotency_key=f"index:document:{ctx.project_id}:{post_id}:{ctx.job_id}",
            max_attempts=3,
            entity_type="article",
            entity_id=article_id,
        )
        linked.append({"postId": post_id, "url": url})
        ctx.info("inbound link added", {"from": url, "to": target_url, "post": post_id})

    ctx.progress(100, stage="done", message=("Internal links built"))
    return {
        "articleId": article_id,
        "targetUrl": target_url,
        "linked": len(linked),
        "pages": linked,
        "skipped": skipped,
    }


def _demo() -> None:
    """Self-check: the insertion only ever adds an anchor."""
    html = "<p>Intro</p><p>Open source gym management software is great.</p>"
    out = insert_contextual_link(html, "https://gyme.cloud/a", ["gym management software"])
    assert out is not None
    assert '<a href="https://gyme.cloud/a">gym management software</a>' in out
    assert re.sub(r"<[^>]+>", "", out) == re.sub(r"<[^>]+>", "", html)
    # already inside an anchor → refuse, never nest
    nested = '<p><a href="https://x.test">gym management software</a></p>'
    assert (
        insert_contextual_link(nested, "https://gyme.cloud/a", ["gym management software"]) is None
    )
    assert insert_contextual_link(html, "https://gyme.cloud/a", ["no such phrase"]) is None
    print("link_building ok")


if __name__ == "__main__":
    _demo()
