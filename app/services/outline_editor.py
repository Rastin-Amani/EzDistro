"""Outline editor — explicit, versioned mutations of the article outline.

The outline snapshot is immutable per version: every user edit creates a NEW
version (version+1) and re-syncs the section rows, so generation metadata from
older versions stays auditable. Changed sections are reset to `pending`
(regeneration on demand); moved/kept sections retain their content.
"""

from __future__ import annotations

from typing import Any

from app.i18n import _
from app.repositories.articles import ArticleRepo, SectionRepo


class OutlineEditor:
    def __init__(self, pb: Any) -> None:
        self._pb = pb
        self._articles = ArticleRepo(pb)
        self._sections = SectionRepo(pb)

    # ---------------------------------------------------------------------------
    def move(self, article: dict[str, Any], position: int, direction: str) -> dict[str, Any]:
        snapshot = dict(article.get("outline") or {})
        sections = snapshot.get("sections") or []
        if not (0 <= position < len(sections)):
            raise ValueError(_("Invalid section position"))
        target = position - 1 if direction == "up" else position + 1
        if not (0 <= target < len(sections)):
            raise ValueError(_("Moving in this direction is not possible"))
        sections[position], sections[target] = sections[target], sections[position]
        return self._commit(article, snapshot)

    def add(
        self,
        article: dict[str, Any],
        heading: str,
        content_brief: str,
        internal_links: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        if not heading.strip():
            raise ValueError(_("Section heading is required"))
        snapshot = dict(article.get("outline") or {})
        sections = list(snapshot.get("sections") or [])
        sections.append(
            {
                "heading": heading.strip(),
                "content_brief": content_brief.strip(),
                "internal_links": internal_links or [],
            }
        )
        snapshot["sections"] = sections
        return self._commit(article, snapshot)

    def delete(self, article: dict[str, Any], position: int) -> dict[str, Any]:
        snapshot = dict(article.get("outline") or {})
        sections = snapshot.get("sections") or []
        if not (0 <= position < len(sections)):
            raise ValueError(_("Invalid section position"))
        del sections[position]
        snapshot["sections"] = sections
        return self._commit(article, snapshot)

    def update_brief(
        self, article: dict[str, Any], position: int, heading: str, content_brief: str
    ) -> dict[str, Any]:
        snapshot = dict(article.get("outline") or {})
        sections = snapshot.get("sections") or []
        if not (0 <= position < len(sections)):
            raise ValueError(_("Invalid section position"))
        if not heading.strip():
            raise ValueError(_("Section heading is required"))
        sections[position] = {
            **sections[position],
            "heading": heading.strip(),
            "content_brief": content_brief.strip(),
        }
        return self._commit(article, snapshot)

    # ---------------------------------------------------------------------------
    def _commit(self, article: dict[str, Any], snapshot: dict[str, Any]) -> dict[str, Any]:
        """Bump the outline version, persist the snapshot, re-sync section rows."""
        article_id = article["id"]
        version = int(article.get("outlineVersion") or 0) + 1
        snapshot.setdefault("title", article.get("title") or "")
        snapshot.setdefault("slug", article.get("slug") or "")
        self._articles.set_outline_ready(
            article_id, version, snapshot, snapshot["title"], snapshot["slug"]
        )
        self._sync_rows(article_id, snapshot)
        return self._articles.get(article_id) or {}

    def _sync_rows(self, article_id: str, snapshot: dict[str, Any]) -> None:
        """Re-sync section rows to the new outline.

        Rows are matched by HEADING first so pure moves preserve generated
        content; only rows whose heading/brief actually changed are reset to
        pending (regeneration on demand).
        """
        plans = snapshot.get("sections") or []
        rows = list(self._sections.list_for_article(article_id))
        used: set[int] = set()

        for i, plan in enumerate(plans):
            heading = str(plan.get("heading") or "")
            brief = str(plan.get("content_brief") or "")

            # prefer a row with the same heading (content preservation)
            idx = next(
                (
                    j
                    for j, r in enumerate(rows)
                    if j not in used and str(r.get("heading") or "") == heading
                ),
                None,
            )
            if idx is None:
                idx = next((j for j in range(len(rows)) if j not in used), None)

            if idx is None:
                self._sections.create(
                    article=article_id,
                    position=i,
                    heading=heading,
                    content_brief=brief,
                    internal_links=plan.get("internal_links") or [],
                )
                continue

            used.add(idx)
            row = rows[idx]
            payload: dict[str, Any] = {
                "position": i,
                "heading": heading,
                "contentBrief": brief,
                "internalLinks": plan.get("internal_links") or [],
            }
            if (
                str(row.get("heading") or "") != heading
                or str(row.get("contentBrief") or "") != brief
            ):
                # brief/heading changed → this section must be regenerated
                payload["status"] = "pending"
                payload["content"] = ""
                payload["error"] = {}
                payload["generationAttempts"] = 0
                payload["promptVersion"] = 0
            self._sections.update(row["id"], payload)

        # remove rows that no longer exist in the plan
        for j, row in enumerate(rows):
            if j not in used:
                self._sections.delete(row["id"])
