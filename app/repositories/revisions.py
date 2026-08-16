"""Article revisions repository — append-only snapshot history."""

from __future__ import annotations

from typing import Any

from app.repositories.base import BaseRepo

REVISION_KINDS = ("generated", "manual", "checkpoint", "rollback")


class ArticleRevisionRepo(BaseRepo):
    collection = "article_revisions"

    def add(
        self,
        *,
        article: str,
        revision: int,
        kind: str,
        snapshot: dict[str, Any],
        note: str = "",
        created_by: str = "",
    ) -> dict[str, Any]:
        if kind not in REVISION_KINDS:
            raise ValueError(f"invalid revision kind: {kind}")
        return super().create(
            {
                "article": article,
                "revision": revision,
                "kind": kind,
                "snapshot": snapshot,
                "note": note,
                "createdBy": created_by,
            }
        )

    def next_revision(self, article: str) -> int:
        latest = self.first(filter=f'article="{article}"', sort="-revision")
        return int((latest or {}).get("revision") or 0) + 1

    def list_for_article(
        self, article_id: str, *, page: int = 1, per_page: int = 50
    ) -> list[dict[str, Any]]:
        return self.list_records(
            filter=f'article="{article_id}"', sort="-revision", page=page, per_page=per_page
        )

    def get(self, record_id: str) -> dict[str, Any] | None:
        return super().get(record_id)

    def get_by_revision(self, article: str, revision: int) -> dict[str, Any] | None:
        return self.first(filter=f'article="{article}" && revision={revision}')
