"""Backfill internal links on articles published while retrieval was down.

Between 2026-10-27 and 2026-10-30 the embedding/vector providers failed, so
`_retrieval_data` handed the outline prompt no link candidates and those
articles shipped with zero internal links. This script recomputes candidates
lexically from the project's own published pages, adds them to the article
(contextually where the text allows, plus a "Related" list), updates the live
WordPress post and the local record.

Usage:
    .venv/bin/python -m app.scripts.backfill_internal_links            # dry run
    .venv/bin/python -m app.scripts.backfill_internal_links --apply
    .venv/bin/python -m app.scripts.backfill_internal_links --project <id> --limit 5 --apply
"""

from __future__ import annotations

import argparse
import asyncio
import html
import re

from app.domain.gutenberg import to_gutenberg_blocks
from app.pb import get_admin_pb
from app.providers.registry import ProviderRegistry
from app.repositories.articles import ArticleRepo
from app.repositories.projects import ProjectRepo
from app.repositories.topics import TopicRepo
from app.services.internal_linking import published_link_candidates
from app.services.link_building import insert_contextual_link
from app.services.settings import ProjectConfig

MAX_LINKS = 5
_RELATED_RE = re.compile(r"<h2[^>]*>\s*Related\s*</h2>", re.IGNORECASE)


def _has_links(article: dict) -> bool:
    outline = article.get("outline") or {}
    if outline.get("internal_links"):
        return True
    return any(
        (section.get("internal_links") or [])
        for section in outline.get("sections") or []
        if isinstance(section, dict)
    )


def _phrases(candidate: dict) -> list[str]:
    from app.services.link_building import _link_phrases

    return _link_phrases(candidate.get("keyword") or "", candidate.get("title") or "")


def _insert(html_text: str, candidate: dict) -> str:
    """Wrap the first safe occurrence; unchanged when the link already exists."""
    if candidate["url"] in html_text:
        return html_text
    return insert_contextual_link(html_text, candidate["url"], _phrases(candidate)) or html_text


def _related_html(candidates: list[dict]) -> str:
    items = "".join(
        '<li><a href="{url}">{anchor}</a></li>'.format(
            url=html.escape(candidate["url"], quote=True),
            anchor=html.escape(candidate["anchor_text"] or candidate["title"], quote=False),
        )
        for candidate in candidates
    )
    return f"<h2>Related</h2><ul>{items}</ul>"


def _with_related(html_text: str, candidates: list[dict], *, gutenberg: bool = False) -> str:
    if _RELATED_RE.search(html_text):
        return html_text
    related = _related_html(candidates)
    if gutenberg:
        related = to_gutenberg_blocks(related)
    return f"{html_text.rstrip()}\n{related}"


def _outline_with_links(article: dict, candidates: list[dict]) -> dict:
    """Article-level link set + a mirror on the last section.

    The deployed UI reads per-section links, so both shapes get the backfilled
    links; `_collect_links` deduplicates when it sees both.
    """
    outline = dict(article.get("outline") or {})
    entry = [
        {
            "title": candidate["title"],
            "url": candidate["url"],
            "anchor_text": candidate["anchor_text"],
        }
        for candidate in candidates
    ]
    sections = [dict(section) for section in outline.get("sections") or []]
    if sections:
        known = {
            str(link.get("url") or "").rstrip("/")
            for section in sections
            for link in section.get("internal_links") or []
        }
        missing = [link for link in entry if link["url"].rstrip("/") not in known]
        last = sections[-1]
        last["internal_links"] = list(last.get("internal_links") or []) + missing
        outline["sections"] = sections
    outline["internal_links"] = entry
    return outline


async def _process(pb, config: ProjectConfig, article: dict, apply: bool) -> int:
    topic = TopicRepo(pb).get(str(article.get("topicId") or "")) or {}
    query = f"{article.get('title') or ''} {topic.get('keyword') or ''}".strip()
    candidates = published_link_candidates(
        pb, config.id, query, exclude_ids={article["id"]}, max_links=MAX_LINKS
    )
    title = str(article.get("title") or "")[:60]
    if not candidates:
        print(f"  - no candidates: {title}")
        return 0

    local = _with_related(article.get("finalHtml") or "", candidates)
    for candidate in candidates:
        local = _insert(local, candidate)

    post_id = int(article.get("wordpressPostId") or 0)
    if not apply:
        print(f"  + {title}: {len(candidates)} link(s), post {post_id} (dry run)")
        for candidate in candidates:
            print(f"      {candidate['anchor_text']} -> {candidate['url']}")
        return len(candidates)

    if post_id:
        publisher = ProviderRegistry(pb).get_publisher_provider(config.project, config.settings)
        post = await publisher.get_post(post_id)
        if post is None:
            print(f"  ! post {post_id} not found for {title}")
            return 0
        live = _with_related(post.content_html or "", candidates, gutenberg=True)
        for candidate in candidates:
            live = _insert(live, candidate)
        await publisher.update_post(post_id, html=live)

    ArticleRepo(pb).update(
        article["id"],
        {
            "finalHtml": local,
            "outline": _outline_with_links(article, candidates),
        },
    )
    print(f"  + {title}: {len(candidates)} link(s), post {post_id}")
    return len(candidates)


async def _run(project_id: str, limit: int, apply: bool) -> int:
    pb = get_admin_pb()
    projects = (
        [project_id]
        if project_id
        else [record["id"] for record in ProjectRepo(pb).list_all(fields="id")]
    )
    total = 0
    for pid in projects:
        config = ProjectConfig.load(pb, pid)
        articles = [
            article
            for article in ArticleRepo(pb).list_all(
                filter=f'project="{pid}" && status="published"',
                sort="-created",
                fields="id,title,topicId,wordpressUrl,wordpressPostId,internalLinks,outline,finalHtml",
            )
            if article.get("wordpressUrl") and not _has_links(article)
        ]
        print(f"== {config.project.get('name') or pid}: {len(articles)} article(s) without links")
        for article in articles[:limit] if limit > 0 else articles:
            total += await _process(pb, config, article, apply)
    print(f"\n{'added' if apply else 'would add'} {total} internal link(s)")
    return total


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="write to PocketBase and WordPress")
    parser.add_argument("--project", default="", help="only this project id")
    parser.add_argument("--limit", type=int, default=0, help="max articles per project")
    args = parser.parse_args()
    if not args.apply:
        print("DRY RUN — pass --apply to write changes")
    asyncio.run(_run(args.project, args.limit, args.apply))


if __name__ == "__main__":
    main()
