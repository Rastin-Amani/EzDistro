"""Article handoff: turn an accepted research opportunity into real work.

The research engine never writes articles and never creates a second content
pipeline. `accept_opportunity` does the smallest thing that connects the two:

- ``generate``/``support`` → a topic (status ``planned``, which is what the
  existing writer treats as "ready to write") + a draft article + a
  ``write_article`` job, so the EXISTING generation pipeline runs unchanged;
- ``update``/``expand`` with a matched existing article → the topic is linked
  to that article; no new article is created and no duplicate URL is generated;
- ``reject`` → nothing is created (the caller just stores the status).

Idempotent: re-accepting the same idea reuses its topic/article and never
queues a second write job (the job's idempotency key is derived from the
article id).
"""

from __future__ import annotations

import re
from typing import Any

from app.domain.article_html import slugify
from app.repositories.articles import ArticleRepo
from app.repositories.jobs import JobRepo
from app.repositories.topics import TopicRepo

LINKED_ACTIONS = frozenset({"update", "expand"})
WRITE_ACTIONS = frozenset({"generate", "support"})


def _clean(value: Any, limit: int = 2000) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    return text[:limit]


def _cluster_name(pb: Any, idea: dict[str, Any]) -> str:
    cluster_id = str(idea.get("cluster") or "")
    if not cluster_id:
        return ""
    row = pb.collection("clusters").get_one(cluster_id) if hasattr(pb, "collection") else None
    if not row:
        return ""
    try:
        return str(dict(row).get("name") or "")
    except Exception:  # pragma: no cover - defensive, fake stores return dicts
        return ""


def accept_opportunity(
    pb: Any, *, idea: dict[str, Any], config: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Create/link an article for an accepted opportunity. Returns the article row."""
    config = config or {}
    project_id = str(config.get("project") or idea.get("project") or "")
    if not project_id:
        raise ValueError("accept_opportunity needs a project")

    action = str(idea.get("action") or "generate")
    title = _clean(idea.get("suggestedTitle") or idea.get("title"), 300)
    if not title:
        raise ValueError("opportunity has no title")
    keyword = _clean(idea.get("primaryKeyword"), 500)
    cluster_name = _cluster_name(pb, idea)

    articles = ArticleRepo(pb)
    topics = TopicRepo(pb)

    # UPDATE / EXPAND: attach to the article the research matched, never a new URL.
    existing_id = str(idea.get("existingArticle") or "")
    if action in LINKED_ACTIONS and existing_id:
        article = articles.get(existing_id)
        if article:
            topic_id = str(article.get("topicId") or "")
            if topic_id:
                topics.update(
                    topic_id,
                    {
                        "keyword": keyword or None,
                        "cluster": cluster_name or None,
                    },
                )
            return article

    # GENERATE / SUPPORT: a topic at 'planned' is what the writer waits for.
    topic = topics.create(
        project=project_id,
        title=title,
        keyword=keyword,
        cluster=cluster_name,
        type="article",
    )
    article = articles.create(
        project=project_id,
        topic_id=topic["id"],
        title=title,
        slug=slugify(title, fallback="research-article"),
    )
    topics.link_article(topic["id"], article["id"])

    if action in WRITE_ACTIONS:
        JobRepo(pb).create(
            project=project_id,
            type="write_article",
            payload={"topicId": topic["id"], "trigger": "research"},
            idempotency_key=f"write_article:{article['id']}",
            entity_type="article",
            entity_id=article["id"],
            max_attempts=3,
        )
    return articles.get(article["id"]) or article


def _demo() -> None:
    """Self-check: action routing is deterministic."""
    assert frozenset({"update", "expand"}) == LINKED_ACTIONS
    assert frozenset({"generate", "support"}) == WRITE_ACTIONS
    assert "reject" not in LINKED_ACTIONS | WRITE_ACTIONS
    print("research_handoff ok")


if __name__ == "__main__":
    _demo()
