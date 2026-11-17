"""Recovery helpers shared by the article page's Resume button and ops scripts.

An assembler that failed with "N section(s) have no job left" is *one* blocker
with its sections, not two: re-queuing only the assembler fails it again
immediately (the sections are still `failed`), and re-queuing only a section
job leaves the assembler dead. Both must restart as one step.
"""

from __future__ import annotations

from typing import Any

from app.domain.retries import IMAGE_MIN_ATTEMPTS, PUBLISH_ATTEMPTS, image_job_attempts
from app.repositories.articles import ArticleRepo, SectionRepo
from app.repositories.jobs import JobRepo


def requeue_attempts(job: dict[str, Any]) -> int:
    """Retry budget for a job being re-armed.

    `reset_for_retry` keeps `maxAttempts`; a job created under the old
    3-attempt budget would die again on the next blip, so the retry would be
    invisible. The budget is raised, never lowered.
    """
    current = int(job.get("maxAttempts") or 0)
    if job.get("type") == "publish_article":
        return max(PUBLISH_ATTEMPTS, current)
    return max(image_job_attempts(current), IMAGE_MIN_ATTEMPTS)


def requeue_failed_sections(pb: Any, article_id: str) -> int:
    """Re-arm the article's failed section stage; returns how many jobs moved.

    Failed sections go back to `pending` so a concurrently starting assembler
    waits for the live section jobs instead of failing on their old status.
    """
    sections = SectionRepo(pb)
    jobs = JobRepo(pb)
    article = ArticleRepo(pb).get(article_id) or {}
    failed_rows = [
        row for row in sections.list_for_article(article_id) if row.get("status") == "failed"
    ]
    if not failed_rows:
        return 0

    requeued = 0
    for row in failed_rows:
        section_id = str(row["id"])
        sections.set_status(section_id, "pending")
        job = jobs.first(filter=f'entityId="{section_id}" && type="generate_section"')
        if job is None:
            # A section with no job at all can never finish; the assembler
            # marks it failed and gives up, so resume has to queue one.
            job = jobs.create(
                project=str(article.get("project") or ""),
                type="generate_section",
                payload={"sectionId": section_id},
                idempotency_key=f"generate:section:{section_id}",
                entity_type="section",
                entity_id=section_id,
            )
        elif job.get("status") in ("failed", "cancelled"):
            jobs.reset_for_retry(str(job["id"]))
        requeued += 1

    for job in jobs.list_records(
        filter=(
            f'(entityId="{article_id}" || payload.articleId="{article_id}")'
            ' && type="assemble_article" && status="failed"'
        ),
        per_page=10,
    ):
        jobs.reset_for_retry(str(job["id"]))
        requeued += 1
    return requeued
