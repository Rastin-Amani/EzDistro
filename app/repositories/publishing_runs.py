"""Publishing runs repository — append-only audit of every WordPress publish attempt.

The `already_published` guard is the idempotency checkpoint consulted BEFORE any
WordPress call, so retried jobs never publish duplicates.
"""

from __future__ import annotations

from typing import Any

from app.repositories.base import BaseRepo
from app.repositories.jobs import now_utc, pb_dt

PUBLISH_STATUSES = ("pending", "published", "unpublished", "failed", "skipped_duplicate")


class PublishingRunRepo(BaseRepo):
    collection = "publishing_runs"

    def start(
        self,
        *,
        project: str,
        article: str,
        job: str,
        mode: str,
        request_id: str = "",
        attempt: int = 1,
        response_metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return super().create(
            {
                "project": project,
                "article": article,
                "job": job or "",
                "mode": mode,
                "status": "pending",
                "attempt": attempt,
                "requestId": request_id,
                "responseMetadata": response_metadata or {},
                "startedAt": pb_dt(now_utc()),
            }
        )

    def already_published(self, article_id: str) -> dict[str, Any] | None:
        """Any successful publish for this article (idempotency guard)."""
        return self.first(filter=f'article="{article_id}" && status="published"')

    def next_attempt(self, article_id: str) -> int:
        attempts = self.list_records(
            filter=f'article="{article_id}"', sort="-attempt", page=1, per_page=1
        )
        return int((attempts[0].get("attempt") or 0) + 1) if attempts else 1

    def mark_published(
        self,
        record_id: str,
        wordpress_post_id: int,
        wordpress_url: str,
        response_metadata: dict[str, Any] | None = None,
    ) -> None:
        payload: dict[str, Any] = {
            "status": "published",
            "wordpressPostId": wordpress_post_id,
            "wordpressUrl": wordpress_url,
            "completedAt": pb_dt(now_utc()),
        }
        if response_metadata:
            payload["responseMetadata"] = response_metadata
        self.update(record_id, payload)

    def mark_unpublished(
        self, record_id: str, response_metadata: dict[str, Any] | None = None
    ) -> None:
        payload: dict[str, Any] = {"status": "unpublished", "completedAt": pb_dt(now_utc())}
        if response_metadata:
            payload["responseMetadata"] = response_metadata
        self.update(record_id, payload)

    def mark_skipped(self, record_id: str, reason: str) -> None:
        self.update(
            record_id,
            {
                "status": "skipped_duplicate",
                "error": {"message": reason},
                "completedAt": pb_dt(now_utc()),
            },
        )

    def mark_failed(self, record_id: str, error: dict[str, Any]) -> None:
        self.update(
            record_id, {"status": "failed", "error": error, "completedAt": pb_dt(now_utc())}
        )

    def list_for_project(
        self, project_id: str, *, page: int = 1, per_page: int = 25
    ) -> list[dict[str, Any]]:
        return self.list_records(
            filter=f'project="{project_id}"', sort="-created", page=page, per_page=per_page
        )

    def list_for_article(
        self, article_id: str, *, page: int = 1, per_page: int = 25
    ) -> list[dict[str, Any]]:
        return self.list_records(
            filter=f'article="{article_id}"', sort="-created", page=page, per_page=per_page
        )
