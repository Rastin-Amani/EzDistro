"""Article revision service — snapshot & rollback.

Every meaningful change (generation, manual edit, pre-regeneration checkpoint)
appends a full snapshot revision; the previous version is NEVER destroyed.
Rollback restores an article + its sections from a snapshot and records a
`rollback` revision so history remains complete.
"""

from __future__ import annotations

from typing import Any

from app.repositories.articles import ArticleRepo, SectionRepo
from app.repositories.revisions import ArticleRevisionRepo


class RevisionService:
    def __init__(self, pb: Any) -> None:
        self._pb = pb
        self._revisions = ArticleRevisionRepo(pb)
        self._articles = ArticleRepo(pb)
        self._sections = SectionRepo(pb)

    # ---------------------------------------------------------------------------
    def snapshot(
        self, article: dict[str, Any], kind: str, note: str = "", created_by: str = ""
    ) -> dict[str, Any]:
        """Append a full snapshot of the article + its sections."""
        article_id = article["id"]
        sections = self._sections.list_for_article(article_id)
        snapshot = {
            "title": article.get("title") or "",
            "slug": article.get("slug") or "",
            "metaDescription": article.get("metaDescription") or "",
            "finalHtml": article.get("finalHtml") or "",
            "wordCount": article.get("wordCount") or 0,
            "seoScore": article.get("seoScore") or 0,
            "outlineVersion": article.get("outlineVersion") or 0,
            "outline": article.get("outline") or {},
            "status": article.get("status") or "draft",
            "sections": [
                {
                    "position": s.get("position") or 0,
                    "heading": s.get("heading") or "",
                    "contentBrief": s.get("contentBrief") or "",
                    "content": s.get("content") or "",
                    "status": s.get("status") or "pending",
                    "provider": s.get("provider") or "",
                    "model": s.get("model") or "",
                    "promptVersion": s.get("promptVersion") or 0,
                    "generationLatency": s.get("generationLatency") or 0,
                    "tokenUsage": s.get("tokenUsage") or {},
                }
                for s in sections
            ],
        }
        revision = self._revisions.next_revision(article_id)
        return self._revisions.add(
            article=article_id,
            revision=revision,
            kind=kind,
            snapshot=snapshot,
            note=note,
            created_by=created_by,
        )

    # ---------------------------------------------------------------------------
    def list(self, article_id: str, *, per_page: int = 50) -> list[dict[str, Any]]:
        return self._revisions.list_for_article(article_id, per_page=per_page)

    def get(self, article_id: str, revision_id: str) -> dict[str, Any] | None:
        revision = self._revisions.get(revision_id)
        if revision and revision.get("article") != article_id:
            return None
        return revision

    # ---------------------------------------------------------------------------
    def rollback(self, article_id: str, revision_id: str, created_by: str = "") -> dict[str, Any]:
        """Restore article + sections from a revision snapshot (history preserved)."""
        revision = self._revisions.get(revision_id)
        if not revision or revision.get("article") != article_id:
            raise ValueError("revision not found")
        snapshot = revision.get("snapshot") or {}
        article = self._articles.get(article_id)
        if not article:
            raise ValueError("article not found")

        # checkpoint the current state before overwriting (never destroy)
        self.snapshot(
            article,
            "rollback",
            note=f"rollback to revision {revision.get('revision')} ({revision.get('kind')})",
            created_by=created_by,
        )

        self._articles.update(
            article_id,
            {
                "title": snapshot.get("title") or article.get("title") or "",
                "slug": snapshot.get("slug") or article.get("slug") or "",
                "metaDescription": snapshot.get("metaDescription") or "",
                "finalHtml": snapshot.get("finalHtml") or "",
                "wordCount": snapshot.get("wordCount") or 0,
                "seoScore": snapshot.get("seoScore") or 0,
                "outlineVersion": snapshot.get("outlineVersion") or 0,
                "outline": snapshot.get("outline") or {},
                "status": snapshot.get("status") or "review",
            },
        )

        # restore section rows (replace contents; keep row ids where possible)
        snapshot_sections = snapshot.get("sections") or []
        rows = self._sections.list_for_article(article_id)
        for i, snap in enumerate(snapshot_sections):
            payload = {
                "position": i,
                "heading": snap.get("heading") or "",
                "contentBrief": snap.get("contentBrief") or "",
                "content": snap.get("content") or "",
                "status": snap.get("status") or "pending",
                "provider": snap.get("provider") or "",
                "model": snap.get("model") or "",
                "promptVersion": snap.get("promptVersion") or 0,
                "generationLatency": snap.get("generationLatency") or 0,
                "tokenUsage": snap.get("tokenUsage") or {},
            }
            if i < len(rows):
                self._sections.update(rows[i]["id"], payload)
            else:
                created = self._sections.create(
                    article=article_id,
                    position=i,
                    heading=str(payload["heading"]),
                    content_brief=str(payload["contentBrief"]),
                )
                self._sections.update(created["id"], payload)
        for stale in rows[len(snapshot_sections) :]:
            self._sections.delete(stale["id"])

        return self._articles.get(article_id) or {}
