"""Re-queue work that a transient provider outage failed permanently.

An image job that burned its attempts against a `503 no_healthy_sellers` blip,
plus the publish that gave up waiting for that cover, is not a content defect —
the article is fine and the provider is back. This resets exactly those
blockers, the same set the article page's **Resume** button requeues.

The same holds for a section stage that died on an empty completion: its
sections and the assembler that gave up on them restart together. Articles
that failed for a content reason (validation) have no failed section and are
left alone for a human.

Usage:
    .venv/bin/python -m app.scripts.requeue_failed_work            # dry run
    .venv/bin/python -m app.scripts.requeue_failed_work --apply
    .venv/bin/python -m app.scripts.requeue_failed_work --project <id> --apply
"""

from __future__ import annotations

import argparse

from app.api.articles import IMAGE_JOB_TYPES, find_failed_jobs
from app.pb import get_admin_pb
from app.repositories.articles import ArticleRepo, SectionRepo
from app.repositories.jobs import JobRepo
from app.repositories.projects import ProjectRepo
from app.repositories.topics import TopicRepo
from app.services.recovery import requeue_attempts, requeue_failed_sections

RESUME_TYPES = set(IMAGE_JOB_TYPES) | {"publish_article"}


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
            failed = find_failed_jobs(pb, article)
            blockers = [job for job in failed if job.get("type") in RESUME_TYPES]
            if blockers:
                requeued += 1
                types = ", ".join(sorted({str(job.get("type")) for job in blockers}))
                print(
                    f"{project.get('name')}: {article.get('id')} [{types}] {article.get('title')}"
                )
                if args.apply:
                    for job in blockers:
                        job_id = str(job["id"])
                        jobs.reset_for_retry(job_id)
                        jobs.update(job_id, {"maxAttempts": requeue_attempts(job)})
                continue
            # A dead section stage is one blocker with its assembler: resetting
            # the assembler alone fails it again on the same sections.
            section_count = len(
                [
                    row
                    for row in SectionRepo(pb).list_for_article(str(article["id"]))
                    if row.get("status") == "failed"
                ]
            )
            if section_count:
                requeued += 1
                print(
                    f"{project.get('name')}: {article.get('id')} [sections] "
                    f"{section_count} failed section(s) {article.get('title')}"
                )
                if args.apply:
                    requeue_failed_sections(pb, str(article["id"]))
        # A failed topic with no article behind it is a write_article that died
        # on the same gateway blip; its payload is the only pointer to it.
        failed_article_ids = {str(a["id"]) for a in rows}
        for topic in TopicRepo(pb).list_records(
            filter=f'project="{project_id}" && status="failed"', per_page=200
        ):
            if str(topic.get("articleId") or "") in failed_article_ids:
                continue
            write_jobs = jobs.list_records(
                filter=(
                    f'type="write_article" && status="failed" && payload.topicId="{topic["id"]}"'
                ),
                per_page=10,
            )
            if not write_jobs:
                continue
            requeued += 1
            print(
                f"{project.get('name')}: topic {topic.get('id')} "
                f"[write_article] {topic.get('keyword')}"
            )
            if args.apply:
                for job in write_jobs:
                    jobs.reset_for_retry(str(job["id"]))
    verb = "requeued" if args.apply else "would requeue (use --apply)"
    print(f"\n{requeued} item(s) {verb}")


if __name__ == "__main__":
    main()
