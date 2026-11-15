"""Re-queue articles that a transient provider outage failed permanently.

An image job that burned its attempts against a `503 no_healthy_sellers` blip,
plus the publish that gave up waiting for that cover, is not a content defect —
the article is fine and the provider is back. This resets exactly those
blockers, the same set the article page's **Resume** button requeues. Articles
that failed for a content reason (validation, a dead section) have no
image/publish blocker and are left alone for a human.

Usage:
    .venv/bin/python -m app.scripts.requeue_failed_work            # dry run
    .venv/bin/python -m app.scripts.requeue_failed_work --apply
    .venv/bin/python -m app.scripts.requeue_failed_work --project <id> --apply
"""

from __future__ import annotations

import argparse

from app.api.articles import IMAGE_JOB_TYPES, find_failed_jobs
from app.domain.images import IMAGE_MIN_ATTEMPTS, image_job_attempts
from app.pb import get_admin_pb
from app.repositories.articles import ArticleRepo
from app.repositories.jobs import JobRepo
from app.repositories.projects import ProjectRepo

RESUME_TYPES = set(IMAGE_JOB_TYPES) | {"publish_article"}
PUBLISH_ATTEMPTS = 40


def _attempts_for(job: dict) -> int:
    """Give the re-queued job the retry budget the current code expects.

    `reset_for_retry` keeps the old `maxAttempts`; a job created (and failed)
    under a 3-attempt budget would die again on the next blip.
    """
    current = int(job.get("maxAttempts") or 0)
    if job.get("type") == "publish_article":
        return max(PUBLISH_ATTEMPTS, current)
    return max(image_job_attempts(current), IMAGE_MIN_ATTEMPTS)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="write the change (default: dry run)")
    parser.add_argument("--project", default="", help="only this project id")
    args = parser.parse_args()

    pb = get_admin_pb()
    articles = ArticleRepo(pb)
    jobs = JobRepo(pb)
    requeued = 0
    for project in ProjectRepo(pb).list_records(filter='status="active"', per_page=100):
        project_id = str(project.get("id") or "")
        if args.project and project_id != args.project:
            continue
        rows = articles.list_records(
            filter=f'project="{project_id}" && status="failed"', per_page=200
        )
        for article in rows:
            blockers = [
                job for job in find_failed_jobs(pb, article) if job.get("type") in RESUME_TYPES
            ]
            if not blockers:
                continue
            requeued += 1
            types = ", ".join(sorted({str(job.get("type")) for job in blockers}))
            print(f"{project.get('name')}: {article.get('id')} [{types}] {article.get('title')}")
            if args.apply:
                for job in blockers:
                    job_id = str(job["id"])
                    jobs.reset_for_retry(job_id)
                    jobs.update(job_id, {"maxAttempts": _attempts_for(job)})
    verb = "requeued" if args.apply else "would requeue (use --apply)"
    print(f"\n{requeued} article(s) {verb}")


if __name__ == "__main__":
    main()
