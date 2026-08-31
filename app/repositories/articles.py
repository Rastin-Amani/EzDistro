"""Articles + article_sections repositories.

The article outline IS its ordered section rows (heading + contentBrief); no
separate outline blob is stored. `outlineVersion` bumps on each (re)build.
"""

from __future__ import annotations

from typing import Any

from app.repositories.base import BaseRepo

ARTICLE_STATUSES = (
    "draft",
    "outline_ready",
    "generating",
    "review",
    "ready_to_publish",
    "approved",
    "sent_back",
    "publishing",
    "published",
    "failed",
)
SECTION_STATUSES = ("pending", "generating", "done", "failed")


class ArticleRepo(BaseRepo):
    collection = "articles"

    def create(
        self,
        *,
        project: str,
        topic_id: str,
        title: str,
        slug: str = "",
    ) -> dict[str, Any]:
        return super().create(
            {
                "project": project,
                "topicId": topic_id,
                "title": title,
                "slug": slug,
                "status": "draft",
                "outlineVersion": 0,
            }
        )

    def list_for_project(
        self, project_id: str, *, status: str | None = None, page: int = 1, per_page: int = 25
    ) -> list[dict[str, Any]]:
        f = f'project="{project_id}"'
        if status:
            f += f' && status="{status}"'
        return self.list_records(filter=f, sort="-created", page=page, per_page=per_page)

    def by_topic(self, topic_id: str) -> dict[str, Any] | None:
        return self.first(filter=f'topicId="{topic_id}"')

    def set_status(self, article_id: str, status: str) -> dict[str, Any]:
        if status not in ARTICLE_STATUSES:
            raise ValueError(f"invalid article status: {status}")
        return self.update(article_id, {"status": status})

    def set_outline_ready(
        self,
        article_id: str,
        outline_version: int,
        outline_snapshot: dict[str, Any],
        title: str,
        slug: str,
    ) -> dict[str, Any]:
        """Persist the validated outline snapshot (immutable after save)."""
        return self.update(
            article_id,
            {
                "status": "outline_ready",
                "outlineVersion": outline_version,
                "outline": outline_snapshot,
                "title": title,
                "slug": slug,
            },
        )

    def set_validation(self, article_id: str, report: dict[str, Any]) -> dict[str, Any]:
        return self.update(article_id, {"validation": report})

    def set_approved(self, article_id: str) -> dict[str, Any]:
        return self.update(article_id, {"status": "approved"})

    def send_back(self, article_id: str, note: str) -> dict[str, Any]:
        return self.update(article_id, {"status": "sent_back", "reviewNote": note})

    def record_generated(self, article_id: str, html: str, revision: int) -> dict[str, Any]:
        """Track the last generated version (distinct from current content)."""
        return self.update(
            article_id, {"generatedContent": html, "lastGeneratedRevision": revision}
        )

    def set_current_content(
        self, article_id: str, html: str, word_count: int, seo_score: int
    ) -> dict[str, Any]:
        return self.update(
            article_id, {"finalHtml": html, "wordCount": word_count, "seoScore": seo_score}
        )

    def set_final_content(
        self,
        article_id: str,
        html: str,
        word_count: int,
        seo_score: int,
        meta_description: str,
    ) -> dict[str, Any]:
        from app.repositories.jobs import now_utc, pb_dt

        return self.update(
            article_id,
            {
                "finalHtml": html,
                "wordCount": word_count,
                "seoScore": seo_score,
                "metaDescription": meta_description,
                "generatedAt": pb_dt(now_utc()),
                "status": "review",
            },
        )

    def mark_published(
        self,
        article_id: str,
        wordpress_post_id: int,
        wordpress_url: str,
    ) -> dict[str, Any]:
        from app.repositories.jobs import now_utc, pb_dt

        return self.update(
            article_id,
            {
                "wordpressPostId": wordpress_post_id,
                "wordpressUrl": wordpress_url,
                "publishedAt": pb_dt(now_utc()),
                "status": "published",
            },
        )

    def set_image_plan(self, article_id: str, plan: dict[str, Any], version: int) -> dict[str, Any]:
        """Persist the validated image plan (immutable snapshot per version)."""
        return self.update(article_id, {"imagePlan": plan, "imagePlanVersion": version})


class SectionRepo(BaseRepo):
    collection = "article_sections"

    def create(
        self,
        *,
        article: str,
        position: int,
        heading: str,
        content_brief: str = "",
        internal_links: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        return super().create(
            {
                "article": article,
                "position": position,
                "heading": heading,
                "contentBrief": content_brief,
                "internalLinks": internal_links or [],
                "status": "pending",
                "generationAttempts": 0,
            }
        )

    def list_for_article(self, article_id: str) -> list[dict[str, Any]]:
        return self.list_records(filter=f'article="{article_id}"', sort="position")

    def set_status(self, section_id: str, status: str, **extra: Any) -> dict[str, Any]:
        if status not in SECTION_STATUSES:
            raise ValueError(f"invalid section status: {status}")
        return self.update(section_id, {"status": status, **extra})

    def mark_generating(self, section_id: str) -> dict[str, Any]:
        from app.repositories.jobs import now_utc, pb_dt

        current = self.get(section_id) or {}
        attempts = int(current.get("generationAttempts") or 0) + 1
        return self.update(
            section_id,
            {
                "status": "generating",
                "generationAttempts": attempts,
                "generationStartedAt": pb_dt(now_utc()),
            },
        )

    def mark_done(
        self,
        section_id: str,
        content: str,
        *,
        provider: str = "",
        model: str = "",
        token_usage: dict[str, Any] | None = None,
        latency_ms: int = 0,
        prompt_version: int = 0,
    ) -> dict[str, Any]:
        return self.update(
            section_id,
            {
                "status": "done",
                "content": content,
                "provider": provider,
                "model": model,
                "tokenUsage": token_usage or {},
                "generationLatency": latency_ms,
                "promptVersion": prompt_version,
                "error": {},
            },
        )

    def mark_failed(self, section_id: str, error: dict[str, Any]) -> dict[str, Any]:
        return self.update(section_id, {"status": "failed", "error": error})
