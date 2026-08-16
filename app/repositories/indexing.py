"""Documents + index runs repositories.

Documents = source documents indexed into Qdrant (vectors live in Qdrant; only
metadata lives here). Index runs are resumable via `lastSourceId` checkpoint.
"""

from __future__ import annotations

from typing import Any

from app.repositories.base import BaseRepo
from app.repositories.jobs import now_utc, pb_dt

RUN_STATUSES = ("running", "succeeded", "failed", "cancelled")
DOC_STATUSES = ("pending", "indexed", "failed", "deleted")
SOURCE_TYPES = ("wordpress", "url", "manual")


class IndexRunRepo(BaseRepo):
    collection = "index_runs"

    def start(
        self,
        *,
        project: str,
        job: str,
        trigger: str = "manual",
        last_source_id: str = "",
    ) -> dict[str, Any]:
        return super().create(
            {
                "project": project,
                "job": job or "",
                "status": "running",
                "trigger": trigger,
                "totalDocuments": 0,
                "processedDocuments": 0,
                "unchangedDocuments": 0,
                "changedDocuments": 0,
                "indexedDocuments": 0,
                "skippedDocuments": 0,
                "failedDocuments": 0,
                "elapsedSeconds": 0,
                "lastSourceId": last_source_id,
                "startedAt": pb_dt(now_utc()),
            }
        )

    def update_progress(self, run_id: str, **fields: Any) -> None:
        self.update(run_id, fields)

    def finish(self, run_id: str, status: str, error: dict[str, Any] | None = None) -> None:
        run = self.get(run_id) or {}
        elapsed = 0.0
        started = run.get("startedAt")
        if started:
            try:
                import datetime as dt

                s = (
                    started
                    if isinstance(started, dt.datetime)
                    else dt.datetime.fromisoformat(str(started).replace("Z", "+00:00"))
                )
                e = now_utc()
                elapsed = round((e - s).total_seconds(), 1)
            except (TypeError, ValueError):
                elapsed = 0.0
        payload: dict[str, Any] = {
            "status": status,
            "finishedAt": pb_dt(now_utc()),
            "elapsedSeconds": elapsed,
        }
        if error:
            payload["error"] = error
        self.update(run_id, payload)

    def latest(self, project_id: str) -> dict[str, Any] | None:
        return self.first(filter=f'project="{project_id}"', sort="-created")

    def list_for_project(
        self, project_id: str, *, page: int = 1, per_page: int = 25
    ) -> list[dict[str, Any]]:
        return self.list_records(
            filter=f'project="{project_id}"', sort="-created", page=page, per_page=per_page
        )


class DocumentRepo(BaseRepo):
    collection = "documents"

    def upsert_metadata(
        self,
        *,
        project: str,
        source_type: str,
        source_id: str,
        title: str,
        source_url: str,
        content_hash: str,
        embedding_provider: str,
        embedding_model: str,
        embedding_dimensions: int,
        chunk_count: int,
        run_id: str,
    ) -> dict[str, Any]:
        if source_type not in SOURCE_TYPES:
            raise ValueError(f"invalid source_type: {source_type}")
        existing = self.first(
            filter=f'project="{project}" && sourceType="{source_type}" && sourceId="{source_id}"'
        )
        payload = {
            "title": title,
            "sourceUrl": source_url,
            "contentHash": content_hash,
            "embeddingProvider": embedding_provider,
            "embeddingModel": embedding_model,
            "embeddingDimensions": embedding_dimensions,
            "chunkCount": chunk_count,
            "indexStatus": "pending",  # flipped to indexed only after vectors exist
            "indexedAt": pb_dt(now_utc()),
            "lastRun": run_id,
        }
        if existing:
            return self.update(existing["id"], payload)
        return super().create(
            {"project": project, "sourceType": source_type, "sourceId": source_id, **payload}
        )

    def by_source(self, project_id: str, source_type: str, source_id: str) -> dict[str, Any] | None:
        return self.first(
            filter=f'project="{project_id}" && sourceType="{source_type}" && sourceId="{source_id}"'
        )

    def list_for_project(
        self, project_id: str, *, status: str | None = None, page: int = 1, per_page: int = 25
    ) -> list[dict[str, Any]]:
        f = f'project="{project_id}"'
        if status:
            f += f' && indexStatus="{status}"'
        return self.list_records(filter=f, sort="-updated", page=page, per_page=per_page)

    def count_indexed(self, project_id: str) -> int:
        return self.count(filter=f'project="{project_id}" && indexStatus="indexed"')

    def set_status(self, record_id: str, status: str) -> dict[str, Any]:
        if status not in DOC_STATUSES:
            raise ValueError(f"invalid document status: {status}")
        return self.update(record_id, {"indexStatus": status})
