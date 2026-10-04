"""Projects + project settings repositories (flat, domain-normalized schema)."""

from __future__ import annotations

from typing import Any

from app.repositories.base import BaseRepo


class ProjectRepo(BaseRepo):
    collection = "projects"

    def create(
        self,
        *,
        name: str,
        slug: str,
        language: str = "en",
        timezone: str = "Asia/Tehran",
        description: str = "",
        created_by: str = "",
    ) -> dict[str, Any]:
        return super().create(
            {
                "name": name,
                "slug": slug,
                "description": description,
                "status": "active",
                "language": language or "en",
                "timezone": timezone or "Asia/Tehran",
                "createdBy": created_by,
            }
        )

    def list_active(self) -> list[dict[str, Any]]:
        return self.list_records(filter='status="active"', sort="name")

    def by_slug(self, slug: str) -> dict[str, Any] | None:
        return self.first(filter=f'slug="{slug}"')

    def set_status(self, project_id: str, status: str) -> dict[str, Any]:
        return self.update(project_id, {"status": status})


# ---------------------------------------------------------------------------
# Flat settings — one scalar per concern, no JSON blobs for hot fields.
# ---------------------------------------------------------------------------
DEFAULT_SETTINGS: dict[str, Any] = {
    "defaultLlmProvider": "openai_compat",
    "defaultLlmModel": "",
    # per-role AI models — empty values resolve to global defaults
    "outlineProvider": "",
    "outlineModel": "",
    "outlineTemperature": 0,
    "outlineMaxTokens": 0,
    "outlineTimeout": 0,
    "outlineRetry": {},
    "sectionProvider": "",
    "sectionModel": "",
    "sectionTemperature": 0,
    "sectionMaxTokens": 0,
    "sectionTimeout": 0,
    "sectionRetry": {},
    "metaProvider": "",
    "metaModel": "",
    "reviewProvider": "",
    "reviewModel": "",
    "embeddingProvider": "openai_compat",
    "embeddingModel": "",
    "embeddingDimensions": 0,
    "chunkSize": 500,
    "chunkOverlap": 100,
    "separatorStrategy": "auto",
    "maxChunkCount": 0,
    "retrievalTopK": 20,
    "similarityThreshold": 0.35,
    "rerankingEnabled": False,
    "rerankerProvider": "cohere_compat",
    "rerankerModel": "rerank-v4.0",
    "rerankerTopN": 8,
    "contextMaxLinks": 5,
    "contextMaxPassages": 5,
    "contextMaxChars": 4000,
    "generationConcurrency": 2,
    "minArticleWords": 300,
    "retryPolicy": {"max_attempts": 3, "backoff_base": 30, "backoff_max": 3600},
    "publishingMode": "publish",
    # localization & brand (multilingual engine: locale/market/voice context)
    "targetLocale": "",
    "targetCountry": "",
    "targetAudience": "",
    "brandName": "",
    "preferredTerminology": "",
    "forbiddenTerminology": "",
    "urlPolicy": "",
    "productContext": "",
    # landing-page analysis → product profile consumed by every prompt
    "landingPageUrl": "",
    "productProfile": {},
    # image subsystem defaults (mirror bootstrap_pb project_settings fields)
    "imageCoverProvider": "gemini",
    "imageCoverModel": "gemini-3-pro-image",
    "imageInteriorProvider": "bfl",
    "imageInteriorModel": "flux-2-klein-9b",
    "imageFallbackProvider": "",
    "imageFallbackModel": "",
    "imageCoverAspectRatio": "16:9",
    "imageInteriorAspectRatio": "16:9",
    "imageCoverMinWidth": 1200,
    "imageMaxInteriorImages": 4,
    "imageMaxRetries": 3,
    "imageOptimizationFormat": "webp",
    "imageAiQaEnabled": False,
    "imagePromptLanguage": "en",
    "imageStyle": {},
    "autosave": {"enabled": False, "interval_minutes": 5},
    # auto-publish: schedule-generated articles publish straight to WordPress
    # when the SEO score reaches the threshold; otherwise they rewrite and
    # retry up to max_attempts before falling back to review.
    "autoPublish": {"enabled": False, "min_score": 90, "max_attempts": 3},
    "indexing": {
        "schedule_enabled": False,
        "schedule_interval_minutes": 1440,
        "wp_status": "publish",
    },
}

SETTINGS_FIELDS = tuple(DEFAULT_SETTINGS.keys())


class ProjectSettingsRepo(BaseRepo):
    collection = "project_settings"

    def get_for_project(self, project_id: str) -> dict[str, Any]:
        """Settings record with defaults merged (never None)."""
        record = self.first(filter=f'project="{project_id}"')
        if not record:
            return {"id": "", "project": project_id, **DEFAULT_SETTINGS}
        merged: dict[str, Any] = {"id": record["id"], "project": record["project"]}
        for key, default in DEFAULT_SETTINGS.items():
            value = record.get(key)
            merged[key] = default if value is None else value
        return merged

    def upsert(self, project_id: str, data: dict[str, Any]) -> dict[str, Any]:
        existing = self.first(filter=f'project="{project_id}"')
        payload = {k: v for k, v in data.items() if k in SETTINGS_FIELDS}
        if existing:
            return self.update(existing["id"], payload)
        return self.create({"project": project_id, **payload})
