"""Reaper — nothing may sit in an in-progress state without a live job.

An artifact loses its job in three ways:

1. the worker is killed mid-flight — the engine reclaims the expired lease, so
   that case is already covered;
2. the job fails permanently — the engine settles its artifact to `failed`
   (see ``JobEngine._settle_artifact``);
3. the job record is deleted (a manual cleanup, a runaway delete) — nothing
   re-reads the artifact, so the article stays at `generating` and the topic at
   `writing` forever with no way forward. That is the case this pass covers.

It also finishes what a permanent failure started when the failure predates the
engine's settle step. A **live job always wins**: nothing here touches an
artifact while any job of its pipeline is pending, retrying or running.

Every action is idempotent — recovery jobs carry a time-window idempotency key
and each artifact is recovered at most ``MAX_RECOVERIES`` times, so N workers
polling at once cannot double-drive a pipeline and a broken article cannot loop
forever.
"""

from __future__ import annotations

import datetime as dt
import time
from typing import Any

import structlog

from app.repositories.articles import ArticleRepo, SectionRepo
from app.repositories.jobs import JobRepo, now_utc, pb_dt
from app.repositories.projects import ProjectRepo
from app.repositories.topics import TopicRepo

logger = structlog.get_logger("worker.reaper")

# A job that could still move the artifact. Anything else (completed, failed,
# cancelled) is history.
LIVE = '(status="pending" || status="retrying" || status="running")'

# How long an artifact may sit unchanged before it counts as abandoned. The
# live-job check above is what actually protects running pipelines; this only
# keeps the reaper from racing a job that is about to be created.
STALE_MINUTES = 15

# Recover an article at most this many times before parking it as `failed`:
# a genuinely broken pipeline must not be retried forever.
MAX_RECOVERIES = 2

# One recovery per artifact per window, so concurrent workers write the same
# idempotency key and the second create is a no-op.
WINDOW_SECONDS = 300

# A section list goes into a filter as one OR term each — keep it bounded.
MAX_SECTION_REFS = 30


def reconcile_stuck(pb: Any, *, stale_minutes: int = STALE_MINUTES) -> dict[str, int]:
    """Resume or park artifacts whose job is gone. Returns what it did."""
    cutoff = pb_dt(now_utc() - dt.timedelta(minutes=stale_minutes))
    counts: dict[str, int] = {}
    for project in ProjectRepo(pb).list_records(filter='status="active"', per_page=50):
        project_id = str(project.get("id") or "")
        if not project_id:
            continue
        try:
            _reap_project(pb, project_id, cutoff, counts)
        except Exception:
            logger.exception("reaper.project.error", project=project_id)
    return counts


def _reap_project(pb: Any, project_id: str, cutoff: str, counts: dict[str, int]) -> None:
    articles = ArticleRepo(pb)
    jobs = JobRepo(pb)
    topics = TopicRepo(pb)
    live = _live_refs(jobs, project_id)

    stuck_articles = articles.list_records(
        filter=f'project="{project_id}" && (status="generating" || status="publishing")',
        sort="updated",
        per_page=50,
    )
    for article in stuck_articles:
        if not _is_stale(article, cutoff):
            break  # sorted by updated: the rest are younger
        try:
            _recover_article(pb, articles, jobs, topics, article, live, counts)
        except Exception:
            logger.exception("reaper.article.error", article=article.get("id"))

    stuck_topics = topics.list_records(
        filter=(
            f'project="{project_id}"'
            ' && (status="queued" || status="writing" || status="outline_ready")'
        ),
        sort="updated",
        per_page=50,
    )
    for topic in stuck_topics:
        if not _is_stale(topic, cutoff):
            break
        try:
            _recover_topic(pb, articles, topics, topic, live, counts)
        except Exception:
            logger.exception("reaper.topic.error", topic=topic.get("id"))


def _live_refs(jobs: JobRepo, project_id: str) -> set[str]:
    """Ids that live jobs point at, in any of the ways a stage links itself.

    One query for the whole project and matching in memory: a filter of
    ``entityId OR payload.articleId OR payload.topicId OR payload.sectionId``
    would be a nested-filter dance, and the live set is small (pending,
    retrying and running jobs of one project).
    """
    refs: set[str] = set()
    rows = jobs.list_records(filter=f'project="{project_id}" && {LIVE}', per_page=500)
    for row in rows:
        entity_id = str(row.get("entityId") or "")
        if entity_id:
            refs.add(entity_id)
        payload = row.get("payload") or {}
        if isinstance(payload, dict):
            for key in ("articleId", "topicId", "sectionId"):
                value = str(payload.get(key) or "")
                if value:
                    refs.add(value)
    return refs


def _article_refs(pb: Any, article: dict[str, Any]) -> set[str]:
    """Everything a live job could reference while this article is being made.

    The initial write job points at the *topic*, the section jobs at their
    section — checking only the article id would read a healthy pipeline as
    abandoned and start a second one on top of it.
    """
    refs = {str(article["id"])}
    topic_id = str(article.get("topicId") or "")
    if topic_id:
        refs.add(topic_id)
    for row in SectionRepo(pb).list_for_article(str(article["id"]))[:MAX_SECTION_REFS]:
        refs.add(str(row["id"]))
    return refs


def _is_stale(record: dict[str, Any], cutoff: str) -> bool:
    # PocketBase timestamps are "YYYY-MM-DD HH:MM:SS.mmmZ" — lexicographic
    # comparison is chronological for a fixed format, which this one is.
    return str(record.get("updated") or "") < cutoff


def _count(counts: dict[str, int], key: str) -> None:
    counts[key] = counts.get(key, 0) + 1
    logger.info("reaper.recovered", action=key)


def _recover_article(
    pb: Any,
    articles: ArticleRepo,
    jobs: JobRepo,
    topics: TopicRepo,
    article: dict[str, Any],
    live: set[str],
    counts: dict[str, int],
) -> None:
    article_id = str(article["id"])
    refs = _article_refs(pb, article)
    # `live` is the cheap whole-project pre-filter; the precise query only runs
    # for an artifact that looks abandoned, which is exactly when accuracy
    # matters (the live set is capped, and a busy project can overflow it).
    if refs & live or _has_live_job(pb, refs):
        return
    if str(article.get("status")) == "publishing":
        _requeue_publish(jobs, articles, article, counts)
        return

    sections = SectionRepo(pb).list_for_article(article_id)
    done = [row for row in sections if row.get("status") == "done"]
    recovered = _recovery_count(jobs, article_id)
    if done and recovered < MAX_RECOVERIES:
        jobs.create(
            project=str(article.get("project") or ""),
            type="assemble_article",
            payload={
                "articleId": article_id,
                "sectionIds": [str(row["id"]) for row in sections],
                # no schedule trigger: a recap must not silently publish or
                # rewrite — it finishes the article, then normal rules apply
                "trigger": "",
            },
            idempotency_key=f"reap:assemble:{article_id}:{_window()}",
            max_attempts=max(10, 3 * len(sections) + 30),  # generous: waits for sections
            entity_type="article",
            entity_id=article_id,
        )
        _count(counts, "assemble_requeued")
        return

    # Nothing left to assemble (or it kept failing): park it where a human can
    # see it and resume it, instead of leaving it "in progress" forever.
    articles.set_status(article_id, "failed")
    _count(counts, "article_parked")
    topic_id = str(article.get("topicId") or "")
    topic = topics.get(topic_id) if topic_id else None
    if topic and topic.get("status") in ("queued", "writing", "outline_ready"):
        topics.set_status(topic_id, "failed")


def _requeue_publish(
    jobs: JobRepo, articles: ArticleRepo, article: dict[str, Any], counts: dict[str, int]
) -> None:
    article_id = str(article["id"])
    # `approved` is what the publish schedule would have set: publishing must
    # never start from a half-open state.
    articles.set_status(article_id, "approved")
    jobs.create(
        project=str(article.get("project") or ""),
        type="publish_article",
        payload={"articleId": article_id, "action": "publish", "trigger": "reaper"},
        idempotency_key=f"reap:publish:{article_id}:{_window()}",
        max_attempts=40,  # publishing waits for the article's images
        entity_type="article",
        entity_id=article_id,
    )
    _count(counts, "publish_requeued")


def _recover_topic(
    pb: Any,
    articles: ArticleRepo,
    topics: TopicRepo,
    topic: dict[str, Any],
    live: set[str],
    counts: dict[str, int],
) -> None:
    topic_id = str(topic["id"])
    if topic_id in live or _has_live_job(pb, {topic_id}):
        return
    # A topic whose article is alive is mid-pipeline: the article's own
    # recovery owns it. Replanning here would let the write schedule produce a
    # second article for the same topic.
    article = _article_for_topic(pb, articles, topic)
    if article is not None:
        if article.get("status") not in ("failed", "draft"):
            return
        refs = _article_refs(pb, article)
        if refs & live or _has_live_job(pb, refs):
            return
    topics.set_status(topic_id, "planned")
    _count(counts, "topic_replanned")


def _has_live_job(pb: Any, refs: set[str]) -> bool:
    """Is any live job pointing at one of these ids, whichever way it links?"""
    if not refs:
        return False
    terms: list[str] = []
    for ref in sorted(refs):
        terms.append(f'entityId="{ref}"')
        terms.append(f'payload.articleId="{ref}"')
        terms.append(f'payload.topicId="{ref}"')
        terms.append(f'payload.sectionId="{ref}"')
    return JobRepo(pb).first(filter=f"({LIVE}) && ({' || '.join(terms)})") is not None


def _article_for_topic(
    pb: Any, articles: ArticleRepo, topic: dict[str, Any]
) -> dict[str, Any] | None:
    """The topic's article, whether or not the topic carries the link back.

    The write handler links the topic as soon as it creates the article, but a
    topic left over from an interrupted first write may not have it yet.
    """
    linked = str(topic.get("articleId") or "")
    if linked:
        found = articles.get(linked)
        if found is not None:
            return found
    return articles.first(filter=f'topicId="{topic["id"]}"')


def _recovery_count(jobs: JobRepo, article_id: str) -> int:
    """How many times the reaper already resumed this article's assembly."""
    prefix = f"reap:assemble:{article_id}:"
    rows = jobs.list_records(
        filter=f'type="assemble_article" && entityId="{article_id}"', per_page=50
    )
    return sum(1 for row in rows if str(row.get("idempotencyKey") or "").startswith(prefix))


def _window() -> int:
    return int(time.time() // WINDOW_SECONDS)
