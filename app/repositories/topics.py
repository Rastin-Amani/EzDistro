"""Topics repository — editorial pipeline state per content idea."""

from __future__ import annotations

from typing import Any

from app.repositories.base import BaseRepo

TOPIC_STATUSES = (
    "planned",
    "queued",
    "planning",
    "outline_ready",
    "writing",
    "review",
    "completed",
    "publishing",
    "published",
    "failed",
    "cancelled",
)
TOPIC_TYPES = ("article", "pillar_page", "guide", "news")


class TopicRepo(BaseRepo):
    collection = "topics"

    def create(
        self,
        *,
        project: str,
        title: str,
        keyword: str = "",
        pillar: str = "",
        cluster: str = "",
        type: str = "article",
        priority: int = 0,
        week: int | None = None,
        url: str = "",
    ) -> dict[str, Any]:
        if type not in TOPIC_TYPES:
            raise ValueError(f"invalid topic type: {type}")
        return super().create(
            {
                "project": project,
                "title": title,
                "keyword": keyword,
                "pillar": pillar,
                "cluster": cluster,
                "type": type,
                "status": "planned",
                "priority": priority,
                "week": week,
                "url": url,
            }
        )

    def list_for_project(
        self, project_id: str, *, status: str | None = None, page: int = 1, per_page: int = 25
    ) -> list[dict[str, Any]]:
        f = f'project="{project_id}"'
        if status:
            f += f' && status="{status}"'
        return self.list_records(filter=f, sort="-priority,-created", page=page, per_page=per_page)

    def search(
        self,
        project_id: str,
        *,
        status: str | None = None,
        q: str = "",
        sort: str = "-priority,-created",
        page: int = 1,
        per_page: int = 25,
    ) -> tuple[list[dict[str, Any]], int]:
        """Filtered + keyword search with pagination. Returns (rows, total)."""
        f = f'project="{project_id}"'
        if status:
            f += f' && status="{status}"'
        if q:
            term = q.strip().replace('"', '"')
            f += f' && (title ~ "{term}" || keyword ~ "{term}")'
        total = self.count(filter=f)
        rows = self.list_records(filter=f, sort=sort, page=page, per_page=per_page)
        return rows, total

    def count_by_status(self, project_id: str, status: str) -> int:
        return self.count(filter=f'project="{project_id}" && status="{status}"')

    def next_unwritten(self, project_id: str) -> dict[str, Any] | None:
        """Highest-priority topic in the write pipeline (oldest first among equals)."""
        return self.first(
            filter=f'project="{project_id}" && status="planned"',
            sort="-priority,created",
        )

    def set_status(self, topic_id: str, status: str) -> dict[str, Any]:
        if status not in TOPIC_STATUSES:
            raise ValueError(f"invalid topic status: {status}")
        return self.update(topic_id, {"status": status})

    def link_article(self, topic_id: str, article_id: str) -> dict[str, Any]:
        return self.update(topic_id, {"articleId": article_id})

    def existing_keys(self, project_id: str) -> tuple[set[str], set[str]]:
        """All normalized titles and keywords already in the project.

        Used by bulk/CSV import to skip duplicates cheaply: one bounded
        paginated read instead of a query per incoming row.
        """
        titles: set[str] = set()
        keywords: set[str] = set()
        page = 1
        while True:
            batch = self.list_records(
                filter=f'project="{project_id}"',
                sort="-created",
                page=page,
                per_page=500,
            )
            if not batch:
                break
            for t in batch:
                title = (t.get("title") or "").strip()
                if title:
                    titles.add(title)
                keyword = (t.get("keyword") or "").strip()
                if keyword:
                    keywords.add(keyword)
            if len(batch) < 500:
                break
            page += 1
        return titles, keywords
