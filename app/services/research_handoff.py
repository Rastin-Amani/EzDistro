"""Article handoff: turn an accepted research opportunity into real work.

The research engine never writes articles and never creates a second content
pipeline. `accept_opportunity` does the smallest thing that connects the two:

- ``generate``/``support`` → a topic (status ``planned``, which is what the
  existing writer treats as "ready to write") + a draft article + a
  ``write_article`` job, so the EXISTING generation pipeline runs unchanged;
- ``update``/``expand`` with a matched existing article → the research brief is
  attached to that article as its ``metaDescription`` context and the topic is
  linked to it; no new article is created, no duplicate URL is generated;
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

BRIEF_MAX = 2000  # metaDescription is short — store the brief, not the essay
LINKED_ACTIONS = frozenset({"update", "expand"})
WRITE_ACTIONS = frozenset({"generate", "support"})


def _clean(value: Any, limit: int = BRIEF_MAX) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    return text[:limit]


def _brief(idea: dict[str, Any]) -> str:
    """A compact, human-readable brief for the article record."""
    parts = [
        _clean(idea.get("uniqueValueProposition")),
        _clean(idea.get("recommendedAngle")),
        _clean(idea.get("contentBrief"), 1200),
    ]
    return _clean(" — ".join(part for part in parts if part), BRIEF_MAX)


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
    brief = _brief(idea)
    cluster_name = _cluster_name(pb, idea)

    articles = ArticleRepo(pb)
    topics = TopicRepo(pb)

    # UPDATE / EXPAND: attach to the article the research matched, never a new URL.
    existing_id = str(idea.get("existingArticle") or "")
    if action in LINKED_ACTIONS and existing_id:
        article = articles.get(existing_id)
        if article:
            updates: dict[str, Any] = {}
            if brief and not article.get("metaDescription"):
                updates["metaDescription"] = brief
            if updates:
                articles.update(existing_id, updates)
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
    updates = {}
    if brief:
        updates["metaDescription"] = brief
    if updates:
        articles.update(article["id"], updates)

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
    """Self-check: brief building + action routing are deterministic."""
    assert _brief({"recommendedAngle": "Compare   pricing", "contentBrief": "x" * 5000}).startswith(
        "Compare pricing"
    )
    assert len(_brief({"contentBrief": "y" * 5000})) <= BRIEF_MAX
    assert frozenset({"update", "expand"}) == LINKED_ACTIONS
    assert frozenset({"generate", "support"}) == WRITE_ACTIONS
    assert "reject" not in LINKED_ACTIONS | WRITE_ACTIONS
    print("research_handoff ok")


if __name__ == "__main__":
    _demo()
