"""InternalLinkingService + ContextBuilder.

- Internal linking: filters low-quality results, removes the current article,
  deduplicates URLs (keeps the highest-scoring), ranks by relevance and caps
  the candidate count — the LLM receives concise structured candidates, never
  a dump of retrieved documents.
- Context builder: configurable budget (max links / max characters / max
  passages) so the prompt gets only the relevant context.
"""

from __future__ import annotations

from typing import Any

from app.schemas.retrieval import LinkCandidate, RetrievalContext, RetrievalResult

DEFAULT_MAX_LINKS = 5
DEFAULT_MAX_PASSAGES = 5
DEFAULT_MAX_CHARS = 4000
MIN_LINK_SCORE = 0.0  # below this, a candidate is low-quality
# ponytail: lexical relevance floor for the no-vector fallback, tune if it
# starts offering unrelated pages (raise) or nothing (lower).
MIN_LEXICAL_SCORE = 0.1


def published_link_candidates(
    pb: Any,
    project_id: str,
    query: str,
    *,
    exclude_ids: set[str] | None = None,
    exclude_url: str = "",
    max_links: int = DEFAULT_MAX_LINKS,
) -> list[dict[str, Any]]:
    """Rank the project's own published pages lexically — no embeddings, no Qdrant.

    PocketBase already holds every published page (title + URL), so internal
    linking must not vanish when the embedding or vector provider is down. The
    result has the ``LinkCandidate`` fields plus ``keyword`` and ``postId`` so
    callers can insert and target links without another lookup.
    """
    from app.domain.keywords import similarity
    from app.repositories.articles import ArticleRepo
    from app.repositories.topics import TopicRepo

    query = (query or "").strip()
    if not query or max_links <= 0:
        return []
    excluded = exclude_ids or set()
    excluded_url = exclude_url.rstrip("/").lower()
    rows = ArticleRepo(pb).list_all(
        filter=f'project="{project_id}" && status="published"',
        fields="id,title,topicId,wordpressUrl,wordpressPostId",
    )
    keyword_by_topic = {
        str(topic["id"]): str(topic.get("keyword") or "")
        for topic in TopicRepo(pb).list_all(filter=f'project="{project_id}"', fields="id,keyword")
    }
    scored: list[tuple[float, str, str, str, str, int]] = []
    for row in rows:
        url = str(row.get("wordpressUrl") or "").strip()
        title = str(row.get("title") or "").strip()
        if not url or not title or str(row.get("id") or "") in excluded:
            continue
        if excluded_url and url.rstrip("/").lower() == excluded_url:
            continue
        topic_keyword = keyword_by_topic.get(str(row.get("topicId") or ""), "")
        score = similarity(query, f"{title} {topic_keyword}")
        if score < MIN_LEXICAL_SCORE:
            continue
        # Titles are "keyword | Real Title" — the tail reads naturally as an anchor.
        anchor = title.split("|", 1)[-1].strip() if "|" in title else title
        scored.append(
            (score, title, url, topic_keyword, anchor, int(row.get("wordpressPostId") or 0))
        )
    scored.sort(key=lambda item: item[0], reverse=True)
    return [
        {
            "title": title,
            "url": url,
            "anchor_text": anchor,
            "keyword": keyword or anchor,
            "postId": post_id,
            "relevance_score": score,
        }
        for score, title, url, keyword, anchor, post_id in scored[:max_links]
    ]


class InternalLinkingService:
    """Builds deduplicated, ranked internal-link candidates from results."""

    def build(
        self,
        results: list[RetrievalResult],
        *,
        current_url: str = "",
        current_title: str = "",
        max_links: int = DEFAULT_MAX_LINKS,
        min_score: float = MIN_LINK_SCORE,
    ) -> list[LinkCandidate]:
        if not results:
            return []

        candidates: dict[str, LinkCandidate] = {}  # url → best candidate
        for result in results:
            title = (result.title or "").strip()
            url = (result.source_url or "").strip()
            score = float(result.final_score or 0.0)
            if not title or not url:
                continue  # low quality: missing identity
            if score < min_score:
                continue  # low quality: irrelevant
            if _same_article(url, title, current_url, current_title):
                continue  # never link to the article being written

            key = url.rstrip("/").lower()
            existing = candidates.get(key)
            if existing is None or score > existing.relevance_score:
                candidates[key] = LinkCandidate(title=title, url=url, relevance_score=score)

        ranked = sorted(candidates.values(), key=lambda c: c.relevance_score, reverse=True)
        return ranked[:max_links]


class ContextBuilder:
    """Configurable prompt-context builder — compact, budgeted, never full docs."""

    def __init__(
        self,
        *,
        max_links: int = DEFAULT_MAX_LINKS,
        max_passages: int = DEFAULT_MAX_PASSAGES,
        max_chars: int = DEFAULT_MAX_CHARS,
        metadata_rules: dict[str, Any] | None = None,
    ) -> None:
        self.max_links = max(0, int(max_links))
        self.max_passages = max(0, int(max_passages))
        self.max_chars = max(0, int(max_chars))
        self.metadata_rules = metadata_rules or {}

    def build(
        self,
        query: str,
        results: list[RetrievalResult],
        *,
        current_url: str = "",
        current_title: str = "",
        linking: InternalLinkingService | None = None,
    ) -> RetrievalContext:
        linking = linking or InternalLinkingService()
        links = linking.build(
            results,
            current_url=current_url,
            current_title=current_title,
            max_links=self.max_links,
        )
        passages = self._budgeted_passages(
            results, max_passages=self.max_passages, max_chars=self.max_chars
        )
        used = sum(len(p) for p in passages)
        return RetrievalContext(
            query=query,
            links=links,
            passages=passages,
            used_chars=used,
            results=list(results),
        )

    def format_for_prompt(self, context: RetrievalContext) -> str:
        """Human-readable compact context block for LLM prompts."""
        return context.format_for_prompt()

    def _budgeted_passages(
        self, results: list[RetrievalResult], *, max_passages: int, max_chars: int
    ) -> list[str]:
        if max_passages <= 0 or max_chars <= 0:
            return []
        passages: list[str] = []
        remaining = max_chars
        per_passage_budget = max(120, max_chars // max(1, max_passages))
        for result in results[:max_passages]:
            snippet = (result.snippet or "").strip()
            if not snippet:
                continue
            if len(snippet) > per_passage_budget:
                snippet = snippet[: per_passage_budget - 1] + "…"
            if len(snippet) > remaining:
                snippet = snippet[: max(1, remaining - 1)] + "…"
            if len(snippet) <= 0:
                break
            passages.append(snippet)
            remaining -= len(snippet)
            if remaining <= 0:
                break
        return passages


def _same_article(url: str, title: str, current_url: str, current_title: str) -> bool:
    if current_url:
        norm = lambda u: (u or "").rstrip("/").lower()  # noqa: E731
        if norm(url) == norm(current_url):
            return True
    if current_title:
        return (title or "").strip().lower() == (current_title or "").strip().lower()
    return False
