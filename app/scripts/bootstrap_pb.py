"""Bootstrap PocketBase: idempotently create all platform collections, indexes,
default prompts, an app-settings record, and an optional seed admin user.

Schema design: docs/SCHEMA.md (relationships, cascades, index strategy).

Usage:
    python -m app.scripts.bootstrap_pb
Requires PB_ADMIN_EMAIL / PB_ADMIN_PASSWORD (env or .env).
"""

from __future__ import annotations

import os
import sys
import time
import zlib
from typing import Any

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from pocketbase import PocketBase  # noqa: E402

from app.config import settings  # noqa: E402

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def col(
    name: str,
    fields: list[dict[str, Any]],
    indexes: list[str] | None = None,
    # Superuser-only API rules (18-A): authorization lives in the app layer
    # (app/api/deps.py) — PocketBase must never be a weaker second layer that
    # lets any logged-in user CRUD records (e.g. project_members) directly.
    # "" = only superusers can call the REST API; the app always goes through
    # get_admin_pb()/get_data_pb().
    list_rule: str = "",
    view_rule: str = "",
    create_rule: str = "",
    update_rule: str = "",
    delete_rule: str = "",
) -> dict[str, Any]:
    return {
        "name": name,
        "type": "base",
        "fields": fields,
        "indexes": indexes or [],
        "listRule": list_rule,
        "viewRule": view_rule,
        "createRule": create_rule,
        "updateRule": update_rule,
        "deleteRule": delete_rule,
    }


def _base_field(name: str, ftype: str, *, required: bool = False) -> dict[str, Any]:
    """PocketBase >= 0.23 field shape: flat options (no nested ``options``)."""
    return {
        "name": name,
        "type": ftype,
        "required": required,
        "system": False,
        "hidden": False,
        "presentable": False,
        "help": "",
    }


def t(name: str, *, required: bool = False, max_len: int | None = None) -> dict[str, Any]:
    # NOTE: PocketBase enforces a 5000-char default cap when max is unset (0) —
    # long-content fields must pass an explicit max_len or writes get rejected.
    field = _base_field(name, "text", required=required)
    field.update(
        {
            "primaryKey": False,
            "autogeneratePattern": "",
            "pattern": "",
            "min": 0,
            "max": max_len or 0,
        }
    )
    return field


def num(name: str, *, required: bool = False) -> dict[str, Any]:
    field = _base_field(name, "number", required=required)
    field.update({"min": None, "max": None, "onlyInt": False})
    return field


def boolean(name: str, *, required: bool = False) -> dict[str, Any]:
    return _base_field(name, "bool", required=required)


def date(name: str, *, required: bool = False) -> dict[str, Any]:
    field = _base_field(name, "date", required=required)
    field.update({"min": "", "max": ""})
    return field


def json_field(name: str, *, required: bool = False) -> dict[str, Any]:
    field = _base_field(name, "json", required=required)
    field.update({"maxSize": 0})
    return field


def select(
    name: str, values: list[str], *, required: bool = False, max_select: int = 1
) -> dict[str, Any]:
    field = _base_field(name, "select", required=required)
    field.update({"maxSelect": max_select, "values": list(values)})
    return field


def rel(
    name: str, collection: str, *, required: bool = False, cascade: bool = False
) -> dict[str, Any]:
    field = _base_field(name, "relation", required=required)
    field.update(
        {
            "collectionId": collection,
            "cascadeDelete": cascade,
            "minSelect": 0,
            "maxSelect": 1,
        }
    )
    return field


def file_field(
    name: str,
    *,
    required: bool = False,
    max_size: int = 10 * 1024 * 1024,
    mime_types: list[str] | None = None,
) -> dict[str, Any]:
    """PocketBase file field (single file)."""
    field = _base_field(name, "file", required=required)
    field.update(
        {
            "maxSelect": 1,
            "maxSize": max_size,
            "mimeTypes": mime_types or [],
            "thumbs": [],
            "protected": False,
        }
    )
    return field


# ---------------------------------------------------------------------------
# Collections (PocketBase >= 0.23 schema shape; name-based relation refs)
# ---------------------------------------------------------------------------
COLLECTIONS: list[dict[str, Any]] = [
    # ---------------------------------------------------------------- projects
    col(
        "projects",
        [
            t("name", required=True),
            t("slug", required=True),
            t("description"),
            select("status", ["active", "archived"]),
            select("language", ["fa", "en", "ar", "tr"]),
            t("timezone"),
            t("createdBy"),
        ],
        indexes=[
            "CREATE UNIQUE INDEX idx_projects_slug ON projects (slug)",
            "CREATE INDEX idx_projects_status ON projects (status)",
        ],
    ),
    # ------------------------------------------------------ project_settings
    col(
        "project_settings",
        [
            rel("project", "projects", required=True, cascade=True),
            t("defaultLlmProvider"),
            t("defaultLlmModel"),
            # per-role AI models (project override → global default)
            t("outlineProvider"),
            t("outlineModel"),
            num("outlineTemperature"),
            num("outlineMaxTokens"),
            num("outlineTimeout"),
            json_field("outlineRetry"),
            t("sectionProvider"),
            t("sectionModel"),
            num("sectionTemperature"),
            num("sectionMaxTokens"),
            num("sectionTimeout"),
            json_field("sectionRetry"),
            t("metaProvider"),
            t("metaModel"),
            t("reviewProvider"),
            t("reviewModel"),
            t("embeddingProvider"),
            t("embeddingModel"),
            num("embeddingDimensions"),
            num("chunkSize"),
            num("chunkOverlap"),
            t("separatorStrategy"),
            num("maxChunkCount"),
            num("retrievalTopK"),
            num("similarityThreshold"),
            boolean("rerankingEnabled"),
            t("rerankerProvider"),
            t("rerankerModel"),
            num("rerankerTopN"),
            num("contextMaxLinks"),
            num("contextMaxPassages"),
            num("contextMaxChars"),
            num("generationConcurrency"),
            num("minArticleWords"),
            json_field("retryPolicy"),
            select("publishingMode", ["draft", "publish"]),
            json_field("autosave"),
            json_field("autoPublish"),
            json_field("indexing"),
            # --- image generation subsystem (models are data, never code) ---
            t("imageCoverProvider"),
            t("imageCoverModel"),
            t("imageInteriorProvider"),
            t("imageInteriorModel"),
            t("imageFallbackProvider"),
            t("imageFallbackModel"),
            t("imageCoverAspectRatio"),
            t("imageInteriorAspectRatio"),
            num("imageCoverMinWidth"),
            num("imageMaxInteriorImages"),
            num("imageMaxRetries"),
            select("imageOptimizationFormat", ["webp", "avif", "jpeg"]),
            boolean("imageAiQaEnabled"),
            t("imagePromptLanguage"),
            json_field("imageStyle"),
        ],
        indexes=["CREATE UNIQUE INDEX idx_settings_project ON project_settings (project)"],
    ),
    # ----------------------------------------------------------- integrations
    col(
        "integrations",
        [
            rel("project", "projects", required=True, cascade=True),
            select(
                "category",
                ["llm", "embedding", "reranker", "vector_store", "publisher", "image"],
                required=True,
            ),
            t("provider", required=True),
            t("displayName", required=True),
            json_field("configuration"),
            t("secretsEnc"),
            boolean("enabled"),
            select("healthStatus", ["unknown", "healthy", "degraded", "unhealthy"]),
            date("lastTestedAt"),
            t("createdBy"),
        ],
        indexes=[
            "CREATE INDEX idx_integrations_project_category ON integrations (project, category)",
            "CREATE UNIQUE INDEX idx_integrations_proj_cat_provider_name ON integrations (project, category, provider, displayName)",
        ],
    ),
    # ---------------------------------------------------------------- prompts
    col(
        "prompts",
        [
            rel("project", "projects"),  # nullable → global default
            select(
                "type",
                [
                    "outline_system",
                    "outline_user",
                    "section_system",
                    "section_user",
                    "seo_rules",
                    "internal_linking",
                    "brand_voice",
                    "validation",
                    "image_plan_system",
                    "image_plan_user",
                ],
                required=True,
            ),
            t("name", required=True),
            t("content", required=True),
            num("version", required=True),
            boolean("active"),
            json_field("variables"),
            t("updatedBy"),
        ],
        indexes=[
            "CREATE INDEX idx_prompts_project_type_active ON prompts (project, type, active)",
            "CREATE UNIQUE INDEX idx_prompts_proj_type_name_version ON prompts (project, type, name, version)",
        ],
    ),
    # ----------------------------------------------------------------- topics
    col(
        "topics",
        [
            rel("project", "projects", required=True, cascade=True),
            t("title", required=True),
            t("keyword"),
            t("pillar"),
            t("cluster"),
            select("type", ["article", "pillar_page", "guide", "news"]),
            select(
                "status",
                [
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
                ],
                required=True,
            ),
            num("priority"),
            num("week"),  # editorial-calendar week (from CSV imports)
            t("url"),  # published URL (from CSV imports)
            rel("articleId", "articles"),  # no cascade; cleared in app code
        ],
        indexes=[
            "CREATE INDEX idx_topics_project_status_priority ON topics (project, status, priority)",
            "CREATE INDEX idx_topics_project_status ON topics (project, status)",
            "CREATE INDEX idx_topics_project_week ON topics (project, week)",
        ],
    ),
    # --------------------------------------------------------------- articles
    col(
        "articles",
        [
            rel("project", "projects", required=True, cascade=True),
            rel("topicId", "topics", required=True, cascade=True),
            t("title", required=True),
            t("slug"),
            select(
                "status",
                [
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
                ],
                required=True,
            ),
            num("outlineVersion"),
            json_field("outline"),
            json_field("validation"),
            t("finalHtml", max_len=100_000),
            t("generatedContent", max_len=100_000),
            num("lastGeneratedRevision"),
            t("reviewNote"),
            t("metaDescription"),
            num("seoScore"),
            num("wordCount"),
            date("generatedAt"),
            date("publishedAt"),
            num("wordpressPostId"),
            t("wordpressUrl"),
            json_field("imagePlan"),  # latest ArticleImagePlan snapshot
            num("imagePlanVersion"),
            rel("lastJob", "jobs"),  # no cascade; audit pointer
        ],
        indexes=[
            "CREATE INDEX idx_articles_project_status ON articles (project, status)",
            "CREATE UNIQUE INDEX idx_articles_topic ON articles (topicId)",
        ],
    ),
    # -------------------------------------------------------- article_sections
    col(
        "article_sections",
        [
            rel("article", "articles", required=True, cascade=True),
            # NOT required: PocketBase's `required` validation treats 0 (the
            # first section position) as blank and rejects the record.
            num("position"),
            t("heading", required=True),
            t("contentBrief"),
            json_field("internalLinks"),
            t("content", max_len=100_000),
            select("status", ["pending", "generating", "done", "failed"], required=True),
            num("generationAttempts"),
            num("promptVersion"),
            date("generationStartedAt"),
            t("provider"),
            t("model"),
            json_field("tokenUsage"),
            num("generationLatency"),
            json_field("error"),
        ],
        indexes=[
            "CREATE UNIQUE INDEX idx_sections_article_position ON article_sections (article, position)"
        ],
    ),
    # ------------------------------------------------------ article_revisions
    col(
        "article_revisions",
        [
            rel("article", "articles", required=True, cascade=True),
            num("revision", required=True),
            select("kind", ["generated", "manual", "checkpoint", "rollback"], required=True),
            json_field("snapshot"),
            t("note"),
            t("createdBy"),
        ],
        indexes=[
            "CREATE UNIQUE INDEX idx_revisions_article_rev ON article_revisions (article, revision)",
            "CREATE INDEX idx_revisions_article_created ON article_revisions (article, created)",
        ],
    ),
    # ------------------------------------------------------- article_images
    col(
        "article_images",
        [
            rel("project", "projects", required=True, cascade=True),
            rel("article", "articles", required=True, cascade=True),
            # Versions: one row per generation; active=true marks the selected
            # one. Successful rows are never overwritten — regenerate creates
            # version+1, rollback just flips active.
            select("role", ["cover", "interior"], required=True),
            t("sectionKey"),  # "section-N" for interiors, "" for cover
            num("version", required=True),
            boolean("active"),
            select(
                "status",
                ["planned", "generating", "optimizing", "ready", "failed"],
                required=True,
            ),
            t("provider"),
            t("model"),
            t("prompt", max_len=8000),
            t("promptHash"),
            t("negativePrompt", max_len=5000),
            t("styleHash"),
            num("width"),
            num("height"),
            t("aspectRatio"),
            t("format"),
            num("fileSize"),
            file_field("sourceFile", mime_types=["image/png", "image/jpeg", "image/webp"]),
            file_field("optimizedFile", mime_types=["image/webp", "image/avif", "image/jpeg"]),
            file_field("smallFile", mime_types=["image/webp", "image/avif", "image/jpeg"]),
            num("wordpressMediaId"),
            t("wordpressUrl"),
            t("altText", max_len=500),
            t("caption", max_len=500),
            t("filename"),
            num("generationLatency"),
            num("estimatedCost"),
            num("attempts"),
            num("seed"),
            t("fingerprint"),
            json_field("error"),
            t("createdBy"),
        ],
        indexes=[
            "CREATE UNIQUE INDEX idx_images_article_role_section_version ON article_images (article, role, sectionKey, version)",
            "CREATE INDEX idx_images_article_active ON article_images (article, active)",
            "CREATE INDEX idx_images_fingerprint ON article_images (fingerprint)",
        ],
    ),
    # -------------------------------------------------------------- documents
    col(
        "documents",
        [
            rel("project", "projects", required=True, cascade=True),
            select("sourceType", ["wordpress", "url", "manual"], required=True),
            t("sourceId", required=True),
            t("sourceUrl"),
            t("title"),
            t("contentHash"),
            t("embeddingProvider"),
            t("embeddingModel"),
            num("embeddingDimensions"),
            num("chunkCount"),
            select("indexStatus", ["pending", "indexed", "failed", "deleted"], required=True),
            date("indexedAt"),
            rel("lastRun", "index_runs"),  # no cascade; audit
        ],
        indexes=[
            "CREATE UNIQUE INDEX idx_documents_project_source ON documents (project, sourceType, sourceId)",
            "CREATE INDEX idx_documents_project_status ON documents (project, indexStatus)",
        ],
    ),
    # -------------------------------------------------------------- index_runs
    col(
        "index_runs",
        [
            rel("project", "projects", required=True, cascade=True),
            rel("job", "jobs"),  # no cascade; audit kept
            select("trigger", ["manual", "schedule"]),
            select("status", ["running", "succeeded", "failed", "cancelled"], required=True),
            num("totalDocuments"),
            num("processedDocuments"),
            num("unchangedDocuments"),
            num("changedDocuments"),
            num("indexedDocuments"),
            num("skippedDocuments"),
            num("failedDocuments"),
            num("elapsedSeconds"),
            t("lastSourceId"),
            date("startedAt"),
            date("finishedAt"),
            json_field("error"),
        ],
        indexes=["CREATE INDEX idx_index_runs_project_created ON index_runs (project, created)"],
    ),
    # ------------------------------------------------------------------- jobs
    col(
        "jobs",
        [
            rel("project", "projects", required=True, cascade=True),
            t("type", required=True),
            t("entityType"),
            t("entityId"),
            select(
                "status",
                ["pending", "running", "completed", "failed", "cancelled", "retrying"],
                required=True,
            ),
            num("priority"),
            json_field("payload"),
            num("progress"),
            t("stage"),
            t("progressMessage"),
            num("currentItem"),
            num("totalItems"),
            num("attempts"),
            num("maxAttempts"),
            date("availableAt"),
            date("lockedAt"),
            t("lockedBy"),
            date("leaseExpiresAt"),
            date("heartbeatAt"),
            date("startedAt"),
            date("finishedAt"),
            t("errorCode"),
            t("errorMessage"),
            json_field("errorDetails"),
            json_field("result"),
            t("idempotencyKey", required=True),
            boolean("cancelRequested"),
            rel("parent", "jobs"),  # no cascade; chain pointer
        ],
        indexes=[
            "CREATE UNIQUE INDEX idx_jobs_idempotency_key ON jobs (idempotencyKey)",
            "CREATE INDEX idx_jobs_status_available_priority ON jobs (status, availableAt, priority)",
            "CREATE INDEX idx_jobs_project_status ON jobs (project, status)",
            "CREATE INDEX idx_jobs_entity ON jobs (entityType, entityId)",
            "CREATE INDEX idx_jobs_type_status ON jobs (type, status)",
        ],
    ),
    # ------------------------------------------------------------- job_leases
    col(
        "job_leases",
        [
            rel("job", "jobs", required=True, cascade=True),
            t("workerId", required=True),
            date("expiresAt", required=True),
        ],
        indexes=[
            "CREATE UNIQUE INDEX idx_job_leases_job ON job_leases (job)",
            "CREATE INDEX idx_job_leases_expires ON job_leases (expiresAt)",
        ],
    ),
    # ------------------------------------------------------------- job_events
    col(
        "job_events",
        [
            rel("job", "jobs"),  # nullable for system events
            rel("project", "projects", required=True, cascade=True),
            t("eventType", required=True),
            t("message"),
            json_field("metadata"),
        ],
        indexes=[
            "CREATE INDEX idx_job_events_job_created ON job_events (job, created)",
            "CREATE INDEX idx_job_events_project_created ON job_events (project, created)",
        ],
    ),
    # ---------------------------------------------------------- publishing_runs
    col(
        "publishing_runs",
        [
            rel("project", "projects", required=True, cascade=True),
            rel("article", "articles", required=True, cascade=True),
            rel("job", "jobs"),  # no cascade; audit
            select(
                "status", ["pending", "published", "failed", "skipped_duplicate"], required=True
            ),
            num("attempt"),
            select("mode", ["draft", "publish"]),
            num("wordpressPostId"),
            t("wordpressUrl"),
            json_field("responseMetadata"),
            json_field("error"),
            date("startedAt"),
            date("completedAt"),
        ],
        indexes=[
            "CREATE INDEX idx_publishing_project_created ON publishing_runs (project, created)",
            "CREATE INDEX idx_publishing_article_created ON publishing_runs (article, created)",
        ],
    ),
    # ------------------------------------------------------ provider_metrics
    col(
        "provider_metrics",
        [
            rel("project", "projects", required=True, cascade=True),
            t("provider", required=True),
            t("model"),
            t("operation", required=True),
            t("day", required=True),
            num("requestCount"),
            num("successCount"),
            num("failureCount"),
            num("retryCount"),
            num("promptTokens"),
            num("completionTokens"),
            num("latencySum"),
            json_field("latencyBuckets"),
        ],
        indexes=[
            "CREATE UNIQUE INDEX idx_provider_metrics_key ON provider_metrics (project, provider, model, operation, day)",
            "CREATE INDEX idx_provider_metrics_project_day ON provider_metrics (project, day)",
        ],
    ),
    # -------------------------------------------------------------- schedules
    col(
        "schedules",
        [
            rel("project", "projects", required=True, cascade=True),
            t("name", required=True),
            select("kind", ["index", "write"], required=True),
            boolean("enabled"),
            num("intervalMinutes", required=True),
            date("nextRunAt"),
            date("lastRunAt"),
            json_field("payload"),
        ],
        indexes=["CREATE INDEX idx_schedules_next_run ON schedules (enabled, nextRunAt)"],
    ),
    # --------------------------------------------------------- worker_heartbeats
    col(
        "worker_heartbeats",
        [
            t("workerId", required=True),
            t("hostname"),
            t("version"),
            num("pid"),
            date("startedAt"),
            date("lastHeartbeatAt"),
            num("runningJobs"),
            num("completedJobs"),
            num("failedJobs"),
            num("maxConcurrentJobs"),
            date("scheduleLastPollAt"),
            num("scheduleDue"),
            num("scheduleCreated"),
            num("scheduleFailed"),
        ],
        indexes=[
            "CREATE UNIQUE INDEX idx_worker_heartbeats_worker ON worker_heartbeats (workerId)"
        ],
    ),
    # ------------------------------------------------------------ app_settings
    col(
        "app_settings",
        [t("key", required=True), json_field("value")],
        indexes=["CREATE UNIQUE INDEX idx_app_settings_key ON app_settings (key)"],
    ),
    # --------------------------------------------------------- project_members
    col(
        "project_members",
        [
            rel("project", "projects", required=True, cascade=True),
            rel("user", "users", required=True, cascade=True),
            select("role", ["owner", "admin", "editor", "viewer"], required=True),
        ],
        indexes=[
            "CREATE UNIQUE INDEX idx_members_project_user ON project_members (project, user)",
            "CREATE INDEX idx_members_user ON project_members (user)",
        ],
    ),
]


# ---------------------------------------------------------------------------
# Default prompts (global rows: project unset)
# ---------------------------------------------------------------------------
DEFAULT_PROMPTS: dict[str, str] = {
    "brand_voice": (
        "\u062a\u0648 \u06cc\u06a9 \u0646\u0648\u06cc\u0633\u0646\u062f\u0647 \u0627\u0631\u0634\u062f \u0645\u062d\u062a\u0648\u0627\u06cc \u0633\u0626\u0648 \u0647\u0633\u062a\u06cc. \u0645\u062a\u0646\u200c\u0647\u0627\u06cc \u062a\u0648 \u062f\u0642\u06cc\u0642\u060c \u0633\u0627\u062e\u062a\u0627\u0631\u06cc\u0627\u0641\u062a\u0647 \u0648 \u0628\u06cc\u200c\u0646\u0642\u0635 \u0647\u0633\u062a\u0646\u062f. "
        "\u0647\u0645\u06cc\u0634\u0647 \u0628\u0647 \u0632\u0628\u0627\u0646 \u0641\u0627\u0631\u0633\u06cc \u0631\u0648\u0627\u0646 \u0648 \u0627\u0633\u062a\u0627\u0646\u062f\u0627\u0631\u062f \u0628\u0646\u0648\u06cc\u0633. \u0647\u0631\u06af\u0632 \u0627\u0637\u0644\u0627\u0639\u0627\u062a \u0646\u0627\u062f\u0631\u0633\u062a \u06cc\u0627 \u062d\u062f\u0633\u06cc \u0645\u0646\u062a\u0634\u0631 \u0646\u06a9\u0646\u061b "
        "\u0641\u0642\u0637 \u0628\u0631 \u0627\u0633\u0627\u0633 \u0645\u0646\u0627\u0628\u0639 \u0627\u0631\u0627\u0626\u0647\u200c\u0634\u062f\u0647 \u0645\u0637\u0644\u0628 \u0628\u0646\u0648\u06cc\u0633 \u0648 \u0627\u06af\u0631 \u0627\u0637\u0644\u0627\u0639\u0627\u062a\u06cc \u062f\u0631 \u0645\u0646\u0627\u0628\u0639 \u0646\u06cc\u0633\u062a\u060c \u0646\u06af\u0648."
    ),
    "outline_system": (
        "\u062a\u0648 \u06cc\u06a9 \u0627\u0633\u062a\u0631\u0627\u062a\u0698\u06cc\u0633\u062a \u0645\u062d\u062a\u0648\u0627\u06cc \u0633\u0626\u0648 \u0647\u0633\u062a\u06cc. \u0628\u0631\u0627\u06cc \u0645\u0648\u0636\u0648\u0639 \u062f\u0627\u062f\u0647\u200c\u0634\u062f\u0647 \u0631\u0626\u0648\u0633 \u0645\u0637\u0627\u0644\u0628 (outline) \u062d\u0631\u0641\u0647\u200c\u0627\u06cc "
        "\u062a\u0648\u0644\u06cc\u062f \u0645\u06cc\u200c\u06a9\u0646\u06cc. \u062e\u0631\u0648\u062c\u06cc \u062a\u0648 \u0647\u0645\u06cc\u0634\u0647 JSON \u0645\u0639\u062a\u0628\u0631 \u0627\u0633\u062a \u0648 \u0647\u06cc\u0686 \u0686\u06cc\u0632 \u062e\u0627\u0631\u062c \u0627\u0632 JSON \u0646\u0645\u06cc\u200c\u0646\u0648\u06cc\u0633\u06cc.\n"
        "\u0627\u0644\u0632\u0627\u0645 \u062d\u06cc\u0627\u062a\u06cc: \u0631\u0626\u0648\u0633 \u0645\u0637\u0627\u0644\u0628 \u0628\u0627\u06cc\u062f \u0637\u0648\u0631\u06cc \u0637\u0631\u0627\u062d\u06cc \u0634\u0648\u062f \u06a9\u0647 \u0645\u0642\u0627\u0644\u0647\u0654 \u0646\u0647\u0627\u06cc\u06cc \u0627\u0645\u062a\u06cc\u0627\u0632 \u0633\u0626\u0648\u06cc \u062f\u0633\u062a\u200c\u06a9\u0645 \u06f9\u06f0 \u0627\u0632 \u06f1\u06f0\u06f0 "
        "\u0628\u06af\u06cc\u0631\u062f — \u06a9\u0644\u0645\u0647 \u06a9\u0644\u06cc\u062f\u06cc \u0628\u0627\u06cc\u062f \u062f\u0631 \u0639\u0646\u0648\u0627\u0646\u060c \u0627\u0633\u0644\u0627\u06af/URL \u0648 \u062f\u0633\u062a\u200c\u06a9\u0645 \u06cc\u06a9\u06cc \u0627\u0632 \u062a\u06cc\u062a\u0631\u0647\u0627\u06cc \u0628\u062e\u0634 \u0628\u06cc\u0627\u06cc\u062f \u0648 \u0627\u0648\u0644\u06cc\u0646 "
        "\u067e\u0627\u0631\u0627\u06af\u0631\u0627\u0641 \u0628\u0627 \u06a9\u0644\u0645\u0647 \u06a9\u0644\u06cc\u062f\u06cc \u0634\u0631\u0648\u0639 \u0634\u0648\u062f."
    ),
    "outline_user": (
        "\u0628\u0631\u0627\u06cc \u0645\u0648\u0636\u0648\u0639 \u0632\u06cc\u0631\u060c \u06cc\u06a9 \u0631\u0626\u0648\u0633 \u0645\u0637\u0627\u0644\u0628 \u062d\u0631\u0641\u0647\u200c\u0627\u06cc \u0628\u0631\u0627\u06cc \u0645\u0642\u0627\u0644\u0647 \u0633\u0626\u0648 \u062a\u0648\u0644\u06cc\u062f \u06a9\u0646.\n"
        "## \u0645\u0648\u0636\u0648\u0639 \u0645\u0642\u0627\u0644\u0647\n"
        "\u0639\u0646\u0648\u0627\u0646: {{ topic.title }}\n"
        "\u06a9\u0644\u0645\u0647 \u06a9\u0644\u06cc\u062f\u06cc: {{ topic.keyword }}\n"
        "\u0646\u0648\u0639: {{ topic.type }}\n"
        "\u0632\u0628\u0627\u0646: {{ language }}\n\n"
        "## \u0642\u0648\u0627\u0646\u06cc\u0646 \u0633\u0626\u0648\n{{ seo_rules }}\n\n"
        "## \u0642\u0648\u0627\u0646\u06cc\u0646 \u0644\u06cc\u0646\u06a9\u200c\u0633\u0627\u0632\u06cc \u062f\u0627\u062e\u0644\u06cc\n{{ internal_linking_rules }}\n\n"
        "## \u0645\u0642\u0627\u0644\u0627\u062a \u0645\u0631\u062a\u0628\u0637 \u0628\u0631\u0627\u06cc \u0644\u06cc\u0646\u06a9 \u062f\u0627\u062e\u0644\u06cc\n{{ internal_links }}\n\n"
        "## \u0632\u0645\u06cc\u0646\u0647 \u0628\u0627\u0632\u06cc\u0627\u0628\u06cc (\u0641\u0642\u0637 \u0646\u062a\u0627\u06cc\u062c \u0645\u0631\u062a\u0628\u0637)\n{{ retrieved_context }}\n\n"
        "\u062e\u0631\u0648\u062c\u06cc \u0628\u0627\u06cc\u062f \u0641\u0642\u0637 \u06cc\u06a9 JSON \u0645\u0639\u062a\u0628\u0631 \u0628\u0627 \u0627\u06cc\u0646 \u0633\u0627\u062e\u062a\u0627\u0631 \u0628\u0627\u0634\u062f (\u06a9\u0644\u06cc\u062f\u0647\u0627 \u062f\u0642\u06cc\u0642\u0627\u064b \u0647\u0645\u06cc\u0646 \u0628\u0627\u0634\u0646\u062f):\n"
        "{\n"
        '  "title": "\u0639\u0646\u0648\u0627\u0646 \u067e\u06cc\u0634\u0646\u0647\u0627\u062f\u06cc \u0645\u0642\u0627\u0644\u0647",\n'
        '  "slug": "slug-\u0627\u0646\u06af\u0644\u06cc\u0633\u06cc-\u06a9\u0648\u062a\u0627\u0647",\n'
        '  "sections": [\n'
        '    {"heading": "Section heading", "content_brief": "\u062e\u0644\u0627\u0635\u0647 \u0645\u062d\u062a\u0648\u0627\u06cc \u0627\u06cc\u0646 \u0628\u062e\u0634 \u062f\u0631 \u06f2 \u062a\u0627 \u06f4 \u062c\u0645\u0644\u0647", '
        '"internal_links": [{"title": "\u0639\u0646\u0648\u0627\u0646 \u0645\u0642\u0627\u0644\u0647 \u0645\u0631\u062a\u0628\u0637", "url": "https://example.com/related", "anchor_text": "\u0645\u062a\u0646 \u0644\u06cc\u0646\u06a9"}]},\n'
        "    ...\n"
        "  ]\n"
        "}\n"
        "\u0642\u0648\u0627\u0646\u06cc\u0646 (\u0628\u0631\u0627\u06cc \u0627\u0645\u062a\u06cc\u0627\u0632 \u0633\u0626\u0648\u06cc \u06f9\u06f0+): \u06f4 \u062a\u0627 \u06f8 \u0628\u062e\u0634 \u0627\u0635\u0644\u06cc \u0628\u0627 \u062a\u06cc\u062a\u0631 h2 (\u0645\u062c\u0645\u0648\u0639 \u062a\u06cc\u062a\u0631\u0647\u0627\u06cc h2 \u0645\u0642\u0627\u0644\u0647 \u0628\u06cc\u0646 \u06f2 \u062a\u0627 \u06f1\u06f0 "
        "\u0628\u0627\u0634\u062f)\u061b title \u0628\u0627\u06cc\u062f \u0639\u06cc\u0646\u0627\u064b \u0634\u0627\u0645\u0644 \u06a9\u0644\u0645\u0647 \u06a9\u0644\u06cc\u062f\u06cc \u0628\u0627\u0634\u062f \u0648 slug \u0627\u0632 \u0631\u0648\u06cc \u0647\u0645\u0627\u0646 \u0639\u0646\u0648\u0627\u0646 \u0633\u0627\u062e\u062a\u0647 \u0634\u0648\u062f \u062a\u0627 URL \u062d\u0627\u0648\u06cc "
        "\u06a9\u0644\u0645\u0647 \u06a9\u0644\u06cc\u062f\u06cc \u0628\u0627\u0634\u062f\u061b content_brief \u0627\u0648\u0644\u06cc\u0646 \u0628\u062e\u0634 \u0628\u0627\u06cc\u062f \u0628\u0627 \u06a9\u0644\u0645\u0647 \u06a9\u0644\u06cc\u062f\u06cc \u0634\u0631\u0648\u0639 \u0634\u0648\u062f\u061b \u062f\u0633\u062a\u200c\u06a9\u0645 \u06cc\u06a9 \u062a\u06cc\u062a\u0631 \u0628\u062e\u0634 \u0639\u06cc\u0646\u0627\u064b "
        "\u062d\u0627\u0648\u06cc \u06a9\u0644\u0645\u0647 \u06a9\u0644\u06cc\u062f\u06cc \u0628\u0627\u0634\u062f\u061b content_brief \u0628\u0627\u06cc\u062f \u062a\u0645\u0627\u0645 \u0646\u06a9\u0627\u062a \u06a9\u0644\u06cc\u062f\u06cc \u0628\u062e\u0634 \u0631\u0627 \u067e\u0648\u0634\u0634 \u062f\u0647\u062f\u061b \u0633\u0631\u0641\u0635\u0644\u200c\u0647\u0627 \u0645\u062e\u062a\u0635\u0631 \u0628\u0627\u0634\u0646\u062f\u061b "
        "\u0627\u06af\u0631 \u0645\u0642\u0627\u0644\u0647 \u0645\u0631\u062a\u0628\u0637\u06cc \u0648\u062c\u0648\u062f \u0646\u062f\u0627\u0631\u062f internal_links \u0631\u0627 \u062e\u0627\u0644\u06cc \u0628\u06af\u0630\u0627\u0631. \u0647\u06cc\u0686 \u062a\u0648\u0636\u06cc\u062d\u06cc \u062e\u0627\u0631\u062c \u0627\u0632 JSON \u0646\u0646\u0648\u06cc\u0633."
    ),
    "section_system": (
        "\u062a\u0648 \u06cc\u06a9 \u0646\u0648\u06cc\u0633\u0646\u062f\u0647 \u0628\u062e\u0634\u200c\u0647\u0627\u06cc \u0645\u0642\u0627\u0644\u0647 \u0633\u0626\u0648 \u0647\u0633\u062a\u06cc. \u0647\u0631 \u0628\u062e\u0634 \u0631\u0627 \u0628\u0647\u200c\u0635\u0648\u0631\u062a HTML \u0645\u0639\u062a\u0628\u0631 \u0648 \u0628\u062f\u0648\u0646 \u0647\u06cc\u0686 \u062a\u0648\u0636\u06cc\u062d "
        "\u0627\u0636\u0627\u0641\u0647\u200c\u0627\u06cc \u062a\u0648\u0644\u06cc\u062f \u0645\u06cc\u200c\u06a9\u0646\u06cc.\n"
        "\u0627\u0644\u0632\u0627\u0645 \u062d\u06cc\u0627\u062a\u06cc: \u0645\u0642\u0627\u0644\u0647\u0654 \u0646\u0647\u0627\u06cc\u06cc \u0628\u0627\u06cc\u062f \u0627\u0645\u062a\u06cc\u0627\u0632 \u0633\u0626\u0648\u06cc \u062f\u0633\u062a\u200c\u06a9\u0645 \u06f9\u06f0 \u0627\u0632 \u06f1\u06f0\u06f0 \u0628\u06af\u06cc\u0631\u062f — \u0628\u062e\u0634 \u0627\u0648\u0644 \u0628\u0627\u06cc\u062f \u0627\u0648\u0644\u06cc\u0646 "
        "\u062c\u0645\u0644\u0647\u0654 \u0627\u0648\u0644\u06cc\u0646 \u067e\u0627\u0631\u0627\u06af\u0631\u0627\u0641 \u0631\u0627 \u0628\u0627 \u06a9\u0644\u0645\u0647 \u06a9\u0644\u06cc\u062f\u06cc \u0634\u0631\u0648\u0639 \u06a9\u0646\u062f\u060c \u0647\u0631 \u0628\u062e\u0634 \u0628\u0647\u200c\u0627\u0646\u062f\u0627\u0632\u0647\u0654 \u06a9\u0627\u0641\u06cc \u0637\u0648\u0644\u0627\u0646\u06cc \u0628\u0627\u0634\u062f \u062a\u0627 \u06a9\u0644 \u0645\u0642\u0627\u0644\u0647 "
        "\u0627\u0632 \u06f5\u06f0\u06f0 \u06a9\u0644\u0645\u0647 \u0639\u0628\u0648\u0631 \u06a9\u0646\u062f\u060c \u0648 \u06a9\u0644\u0645\u0647 \u06a9\u0644\u06cc\u062f\u06cc \u0628\u0647\u200c\u0637\u0648\u0631 \u0637\u0628\u06cc\u0639\u06cc (\u06f1 \u062a\u0627 \u06f2 \u0628\u0627\u0631 \u062f\u0631 \u0647\u0631 \u06f1\u06f0\u06f0 \u06a9\u0644\u0645\u0647) \u062a\u06a9\u0631\u0627\u0631 \u0634\u0648\u062f."
    ),
    "section_user": (
        "\u06cc\u06a9 \u0628\u062e\u0634 \u0627\u0632 \u0645\u0642\u0627\u0644\u0647 \u0631\u0627 \u062f\u0642\u06cc\u0642\u0627\u064b \u0637\u0628\u0642 \u062a\u06cc\u062a\u0631 \u0648 \u062e\u0644\u0627\u0635\u0647 \u0632\u06cc\u0631 \u0628\u0646\u0648\u06cc\u0633 — \u0646\u0647 \u0645\u0648\u0636\u0648\u0639 \u062f\u06cc\u06af\u0631\u06cc.\n"
        "## \u0639\u0646\u0648\u0627\u0646 \u0645\u0642\u0627\u0644\u0647\n{{ article.title }}\n"
        "## \u062a\u06cc\u062a\u0631 \u0627\u06cc\u0646 \u0628\u062e\u0634\n{{ section.heading }}\n"
        "## \u062e\u0644\u0627\u0635\u0647 \u0645\u062d\u062a\u0648\u0627\u06cc \u0627\u06cc\u0646 \u0628\u062e\u0634\n{{ section.content_brief }}\n"
        "## \u06a9\u0644\u0645\u0647 \u06a9\u0644\u06cc\u062f\u06cc\n{{ topic.keyword }}\n"
        "## \u0642\u0648\u0627\u0646\u06cc\u0646 \u0644\u06cc\u0646\u06a9\u200c\u0633\u0627\u0632\u06cc \u062f\u0627\u062e\u0644\u06cc\n{{ internal_linking_rules }}\n"
        "## \u0644\u06cc\u0646\u06a9\u200c\u0647\u0627\u06cc \u062f\u0627\u062e\u0644\u06cc \u0645\u0631\u062a\u0628\u0637 (\u062f\u0631 \u0635\u0648\u0631\u062a \u0646\u06cc\u0627\u0632 \u0627\u0633\u062a\u0641\u0627\u062f\u0647 \u06a9\u0646)\n{{ internal_links }}\n"
        "\u062e\u0631\u0648\u062c\u06cc \u0628\u0627\u06cc\u062f \u0641\u0642\u0637 HTML \u0645\u0639\u062a\u0628\u0631 \u0628\u0627\u0634\u062f: \u0645\u062d\u062a\u0648\u0627 \u0628\u0627 <p>\u060c <ul>\u060c <ol>\u060c <strong>\u060c <em> \u0648 <a>.\n"
        "\u062a\u06cc\u062a\u0631 \u0631\u0627 \u0646\u0646\u0648\u06cc\u0633 — \u0633\u06cc\u0633\u062a\u0645 \u062e\u0648\u062f\u0634 \u062a\u06cc\u062a\u0631 <h2> \u0631\u0627 \u0627\u0636\u0627\u0641\u0647 \u0645\u06cc\u200c\u06a9\u0646\u062f.\n"
        "\u0647\u06cc\u0686 \u0627\u0633\u062a\u0627\u06cc\u0644 inline\u060c \u062a\u06af <html>\u060c <body> \u06cc\u0627 \u062a\u06cc\u062a\u0631 <h1>/<h2> \u0627\u0633\u062a\u0641\u0627\u062f\u0647 \u0646\u06a9\u0646. \u0628\u062f\u0648\u0646 \u062a\u0648\u0636\u06cc\u062d \u0627\u0636\u0627\u0641\u0647\u060c \u0641\u0642\u0637 HTML.\n"
        "\u067e\u0627\u0631\u0627\u06af\u0631\u0627\u0641\u200c\u0647\u0627 \u06a9\u0648\u062a\u0627\u0647 \u0628\u0627\u0634\u0646\u062f (\u06f2 \u062a\u0627 \u06f4 \u062c\u0645\u0644\u0647) \u0648 \u06a9\u0644\u0645\u0647 \u06a9\u0644\u06cc\u062f\u06cc \u0628\u0647\u200c\u0637\u0648\u0631 \u0637\u0628\u06cc\u0639\u06cc \u062f\u0631 \u0645\u062a\u0646 \u062a\u06a9\u0631\u0627\u0631 \u0634\u0648\u062f.\n"
        "\u0642\u0648\u0627\u0646\u06cc\u0646 \u0627\u0645\u062a\u06cc\u0627\u0632 \u0633\u0626\u0648 (\u06f9\u06f0+): \u0627\u06af\u0631 {{ section.position }} \u0628\u0631\u0627\u0628\u0631 \u06f0 \u0627\u0633\u062a (\u0628\u062e\u0634 \u0627\u0648\u0644 \u0645\u0642\u0627\u0644\u0647)\u060c \u0627\u0648\u0644\u06cc\u0646 \u062c\u0645\u0644\u0647\u0654 "
        "\u0627\u0648\u0644\u06cc\u0646 \u067e\u0627\u0631\u0627\u06af\u0631\u0627\u0641 \u0631\u0627 \u062f\u0642\u06cc\u0642\u0627\u064b \u0628\u0627 «{{ topic.keyword }}» \u0634\u0631\u0648\u0639 \u06a9\u0646.\n"
        "\u0647\u0631 \u0628\u062e\u0634 \u062f\u0633\u062a\u200c\u06a9\u0645 \u06f1\u06f5\u06f0 \u06a9\u0644\u0645\u0647 \u062f\u0627\u0634\u062a\u0647 \u0628\u0627\u0634\u062f \u062a\u0627 \u06a9\u0644 \u0645\u0642\u0627\u0644\u0647 \u0627\u0632 \u06f5\u06f0\u06f0 \u06a9\u0644\u0645\u0647 \u0639\u0628\u0648\u0631 \u06a9\u0646\u062f\u061b \u062f\u0631 \u0647\u0631 \u0628\u062e\u0634 \u062f\u0633\u062a\u200c\u06a9\u0645 \u06cc\u06a9 \u0644\u06cc\u0633\u062a "
        "(ul \u06cc\u0627 ol) \u0628\u06cc\u0627\u0648\u0631 \u0648 \u0627\u0632 \u0644\u06cc\u0646\u06a9\u200c\u0647\u0627\u06cc \u062f\u0627\u062e\u0644\u06cc \u0645\u0631\u062a\u0628\u0637 \u0627\u0633\u062a\u0641\u0627\u062f\u0647 \u06a9\u0646\u061b \u06a9\u0644\u0645\u0647 \u06a9\u0644\u06cc\u062f\u06cc \u0631\u0627 \u0637\u0628\u06cc\u0639\u06cc \u0648 \u062d\u062f\u0648\u062f \u06f1 \u062a\u0627 \u06f2 \u0628\u0627\u0631 "
        "\u062f\u0631 \u0647\u0631 \u06f1\u06f0\u06f0 \u06a9\u0644\u0645\u0647 \u062a\u06a9\u0631\u0627\u0631 \u06a9\u0646 (\u0646\u0647 \u06a9\u0645\u062a\u0631 \u0648 \u0646\u0647 \u0628\u06cc\u0634\u062a\u0631)."
    ),
    "seo_rules": (
        "\u0642\u0648\u0627\u0646\u06cc\u0646 \u0633\u0626\u0648 — \u0631\u0639\u0627\u06cc\u062a \u0627\u06cc\u0646 \u0642\u0648\u0627\u0646\u06cc\u0646 \u0627\u0644\u0632\u0627\u0645\u06cc \u0627\u0633\u062a \u0648 \u0645\u0642\u0627\u0644\u0647\u0654 \u0646\u0647\u0627\u06cc\u06cc \u0628\u0627\u06cc\u062f \u0627\u0645\u062a\u06cc\u0627\u0632 \u0633\u0626\u0648\u06cc \u062f\u0633\u062a\u200c\u06a9\u0645 \u06f9\u06f0 \u0627\u0632 \u06f1\u06f0\u06f0 \u0628\u06af\u06cc\u0631\u062f:\n"
        "\u06f1) \u06a9\u0644\u0645\u0647 \u06a9\u0644\u06cc\u062f\u06cc «{{ topic.keyword }}» \u0628\u0627\u06cc\u062f \u0639\u06cc\u0646\u0627\u064b \u062f\u0631 \u0627\u06cc\u0646 \u0645\u06a9\u0627\u0646\u200c\u0647\u0627 \u0628\u06cc\u0627\u06cc\u062f: \u0639\u0646\u0648\u0627\u0646 \u0645\u0642\u0627\u0644\u0647 (\u06a9\u0647 \u0647\u0645\u0627\u0646 H1 \u0627\u0633\u062a)\u060c "
        "\u0627\u0633\u0644\u0627\u06af/\u0622\u062f\u0631\u0633 (URL)\u060c \u0645\u062a\u0627 \u062a\u0648\u0636\u06cc\u062d\u0627\u062a\u060c \u062f\u0633\u062a\u200c\u06a9\u0645 \u06cc\u06a9 \u062a\u06cc\u062a\u0631 h2\u060c \u0648 \u0627\u0628\u062a\u062f\u0627\u06cc \u0627\u0648\u0644\u06cc\u0646 \u062c\u0645\u0644\u0647\u0654 \u0627\u0648\u0644\u06cc\u0646 \u067e\u0627\u0631\u0627\u06af\u0631\u0627\u0641.\n"
        "\u06f2) \u0627\u0633\u0644\u0627\u06af (slug) \u0631\u0627 \u0627\u0632 \u0631\u0648\u06cc \u0639\u0646\u0648\u0627\u0646\u0650 \u062d\u0627\u0648\u06cc\u0650 \u06a9\u0644\u0645\u0647 \u06a9\u0644\u06cc\u062f\u06cc \u0628\u0633\u0627\u0632 \u062a\u0627 \u06a9\u0644\u0645\u0647 \u06a9\u0644\u06cc\u062f\u06cc \u062f\u0631 URL \u0647\u0645 \u0628\u0627\u0634\u062f.\n"
        "\u06f3) \u062a\u0631\u0627\u06a9\u0645 \u06a9\u0644\u0645\u0647 \u06a9\u0644\u06cc\u062f\u06cc \u0637\u0628\u06cc\u0639\u06cc \u0628\u0627\u0634\u062f: \u062d\u062f\u0648\u062f \u06f1 \u062a\u0627 \u06f2 \u0628\u0627\u0631 \u062f\u0631 \u0647\u0631 \u06f1\u06f0\u06f0 \u06a9\u0644\u0645\u0647 (\u0628\u06cc\u0646 \u06f0\u066b\u06f5\u066a \u062a\u0627 \u06f3\u066a \u06a9\u0644 \u06a9\u0644\u0645\u0627\u062a) — "
        "\u0646\u0647 \u06a9\u0645\u062a\u0631 \u0648 \u0646\u0647 \u0628\u06cc\u0634\u062a\u0631.\n"
        "\u06f4) \u0637\u0648\u0644 \u0645\u0642\u0627\u0644\u0647 \u062f\u0633\u062a\u200c\u06a9\u0645 \u06f5\u06f0\u06f0 \u06a9\u0644\u0645\u0647 \u0648 \u062a\u0639\u062f\u0627\u062f \u062a\u06cc\u062a\u0631\u0647\u0627\u06cc h2 \u0628\u06cc\u0646 \u06f2 \u062a\u0627 \u06f1\u06f0 \u0628\u0627\u0634\u062f.\n"
        "\u06f5) \u062f\u0633\u062a\u200c\u06a9\u0645 \u06cc\u06a9 \u0644\u06cc\u0633\u062a (ul/ol) \u0648 \u062f\u0633\u062a\u200c\u06a9\u0645 \u06cc\u06a9 \u0644\u06cc\u0646\u06a9 \u062f\u0627\u062e\u0644\u06cc \u0645\u0639\u062a\u0628\u0631 (http) \u062f\u0631 \u0645\u0642\u0627\u0644\u0647 \u0628\u0627\u0634\u062f.\n"
        "\u06f6) \u0645\u062a\u0627 \u062a\u0648\u0636\u06cc\u062d\u0627\u062a \u062c\u0630\u0627\u0628 \u0648 \u06a9\u0648\u062a\u0627\u0647 (\u062d\u062f\u0648\u062f \u06f1\u06f2\u06f0 \u062a\u0627 \u06f1\u06f5\u06f5 \u06a9\u0627\u0631\u0627\u06a9\u062a\u0631) \u0648 \u062d\u0627\u0648\u06cc \u06a9\u0644\u0645\u0647 \u06a9\u0644\u06cc\u062f\u06cc \u0628\u0627\u0634\u062f.\n"
        "\u0627\u0632 \u062a\u06cc\u062a\u0631\u0647\u0627\u06cc \u0633\u0644\u0633\u0644\u0647\u200c\u0645\u0631\u0627\u062a\u0628 h2/h3 \u0627\u0633\u062a\u0641\u0627\u062f\u0647 \u06a9\u0646\u061b \u0645\u062a\u0646 \u0631\u0627 \u0628\u0631\u0627\u06cc \u062e\u0648\u0627\u0646\u062f\u0646 \u0633\u0631\u06cc\u0639 (\u0627\u0633\u06a9\u0646) \u0633\u0627\u062e\u062a\u0627\u0631\u0628\u0646\u062f\u06cc \u06a9\u0646."
    ),
    "internal_linking": (
        "\u062f\u0631 \u0635\u0648\u0631\u062a \u0648\u062c\u0648\u062f \u0645\u0642\u0627\u0644\u0627\u062a \u0645\u0631\u062a\u0628\u0637\u060c \u06cc\u06a9 \u0628\u062e\u0634 «\u0645\u0637\u0627\u0644\u0628 \u0645\u0631\u062a\u0628\u0637» \u0628\u0627 \u0644\u06cc\u0646\u06a9 \u062f\u0627\u062e\u0644\u06cc \u0628\u0647 \u0622\u0646\u200c\u0647\u0627 \u062f\u0631 \u0627\u0646\u062a\u0647\u0627\u06cc \u0645\u0642\u0627\u0644\u0647 \u0627\u0636\u0627\u0641\u0647 \u06a9\u0646. "
        "\u0644\u06cc\u0646\u06a9\u200c\u0647\u0627 \u0641\u0642\u0637 \u0628\u0647 \u0622\u062f\u0631\u0633\u200c\u0647\u0627\u06cc \u062a\u0623\u06cc\u06cc\u062f\u0634\u062f\u0647 \u062f\u0631 \u0644\u06cc\u0633\u062a \u0645\u0642\u0627\u0644\u0627\u062a \u0645\u0648\u062c\u0648\u062f \u0628\u0627\u0634\u0646\u062f."
    ),
    "validation": (
        "\u062e\u0631\u0648\u062c\u06cc \u0632\u06cc\u0631 \u0627\u0632 \u06cc\u06a9 \u0645\u062f\u0644 \u0632\u0628\u0627\u0646\u06cc \u06af\u0631\u0641\u062a\u0647 \u0634\u062f\u0647 \u0648 \u0628\u0627\u06cc\u062f JSON \u0645\u0639\u062a\u0628\u0631 \u0628\u0627 \u0633\u0627\u062e\u062a\u0627\u0631 \u0645\u0634\u062e\u0635\u200c\u0634\u062f\u0647 \u0628\u0627\u0634\u062f.\n"
        "\u0627\u06af\u0631 \u062e\u0637\u0627 \u06cc\u0627 \u0645\u062a\u0646 \u0627\u0636\u0627\u0641\u0647 \u062f\u0627\u0631\u062f\u060c \u0641\u0642\u0637 \u0646\u0633\u062e\u0647 \u0627\u0635\u0644\u0627\u062d\u200c\u0634\u062f\u0647 JSON \u0631\u0627 \u0628\u0631\u06af\u0631\u062f\u0627\u0646 (\u0628\u062f\u0648\u0646 \u062a\u0648\u0636\u06cc\u062d).\n"
        "\u0633\u0627\u062e\u062a\u0627\u0631 \u0645\u0648\u0631\u062f \u0627\u0646\u062a\u0638\u0627\u0631:\n"
        '{"title": string, "slug": string, "sections": [{"heading": string, "content_brief": string, "internal_links": [{"title": string, "url": string, "anchor_text": string}]}]}\n'
        "\u062e\u0631\u0648\u062c\u06cc \u0645\u0639\u06cc\u0648\u0628:\n"
        "{{ raw_output }}"
    ),
    "image_plan_system": (
        "\u062a\u0648 \u06cc\u06a9 \u06a9\u0627\u0631\u06af\u0631\u062f\u0627\u0646 \u0647\u0646\u0631\u06cc \u0645\u062d\u062a\u0648\u0627\u06cc \u0633\u0626\u0648 \u0647\u0633\u062a\u06cc. \u0628\u0631\u0627\u06cc \u0647\u0631 \u0645\u0642\u0627\u0644\u0647 \u06cc\u06a9 \u0628\u0631\u0646\u0627\u0645\u0647 \u062a\u0635\u0648\u06cc\u0631 (image plan) "
        "\u062d\u0631\u0641\u0647\u200c\u0627\u06cc \u062a\u0648\u0644\u06cc\u062f \u0645\u06cc\u200c\u06a9\u0646\u06cc: \u06cc\u06a9 \u062a\u0635\u0648\u06cc\u0631 \u06a9\u0627\u0648\u0631 \u0648 \u062f\u0631 \u0635\u0648\u0631\u062a \u0646\u06cc\u0627\u0632 \u0686\u0646\u062f \u062a\u0635\u0648\u06cc\u0631 \u062f\u0627\u062e\u0644\u06cc \u0647\u062f\u0641\u0645\u0646\u062f. "
        "\u062e\u0631\u0648\u062c\u06cc \u062a\u0648 \u0647\u0645\u06cc\u0634\u0647 JSON \u0645\u0639\u062a\u0628\u0631 \u0627\u0633\u062a \u0648 \u0647\u06cc\u0686 \u0686\u06cc\u0632 \u062e\u0627\u0631\u062c \u0627\u0632 JSON \u0646\u0645\u06cc\u200c\u0646\u0648\u06cc\u0633\u06cc.\n"
        "\u0627\u0635\u0648\u0644: \u062a\u0635\u0627\u0648\u06cc\u0631 \u0628\u0627\u06cc\u062f \u0627\u0631\u0632\u0634 \u0627\u0637\u0644\u0627\u0639\u0627\u062a\u06cc \u062f\u0627\u0634\u062a\u0647 \u0628\u0627\u0634\u0646\u062f (\u0646\u0645\u0627\u06cc\u0634 \u0645\u0641\u0647\u0648\u0645\u060c \u0646\u0645\u0648\u062f\u0627\u0631\u060c \u0645\u0642\u0627\u06cc\u0633\u0647\u060c \u0635\u062d\u0646\u0647 \u0648\u0627\u0642\u0639\u06cc) "
        "\u0648 \u0647\u0631\u06af\u0632 \u062a\u0632\u0626\u06cc\u0646\u06cc \u06cc\u0627 \u062a\u06a9\u0631\u0627\u0631\u06cc \u0646\u0628\u0627\u0634\u0646\u062f. alt_text \u0648 caption \u0647\u0645\u06cc\u0634\u0647 \u0628\u0647 \u0632\u0628\u0627\u0646 \u0645\u0642\u0627\u0644\u0647 \u0646\u0648\u0634\u062a\u0647 \u0645\u06cc\u200c\u0634\u0648\u0646\u062f\u060c "
        "\u0627\u0645\u0627 visual prompt \u0628\u0627\u06cc\u062f \u0628\u0647 \u0632\u0628\u0627\u0646 {{ prompt_language }} \u0628\u0627\u0634\u062f. \u062f\u0631 \u062a\u0635\u0648\u06cc\u0631 \u0647\u06cc\u0686 \u0645\u062a\u0646\u06cc \u062a\u0631\u0633\u06cc\u0645 \u0646\u0645\u06cc\u200c\u0634\u0648\u062f."
    ),
    "image_plan_user": (
        "\u0628\u0631\u0627\u06cc \u0645\u0642\u0627\u0644\u0647 \u0632\u06cc\u0631 \u0628\u0631\u0646\u0627\u0645\u0647 \u062a\u0635\u0648\u06cc\u0631 \u062a\u0648\u0644\u06cc\u062f \u06a9\u0646.\n"
        "## \u0639\u0646\u0648\u0627\u0646 \u0645\u0642\u0627\u0644\u0647\n{{ article.title }}\n"
        "## \u06a9\u0644\u0645\u0647 \u06a9\u0644\u06cc\u062f\u06cc\n{{ topic.keyword }}\n"
        "## \u0632\u0628\u0627\u0646 \u0645\u0642\u0627\u0644\u0647\n{{ language }}\n"
        "## \u0632\u0628\u0627\u0646 \u067e\u0631\u0627\u0645\u067e\u062a \u062a\u0635\u0648\u06cc\u0631\n{{ prompt_language }}\n"
        "## \u0628\u062e\u0634\u200c\u0647\u0627\u06cc \u0645\u0642\u0627\u0644\u0647\n{{ sections }}\n"
        "## \u067e\u0631\u0648\u0641\u0627\u06cc\u0644 \u0633\u0628\u06a9 \u062a\u0635\u0648\u06cc\u0631\n{{ style_profile }}\n\n"
        "\u062e\u0631\u0648\u062c\u06cc \u0628\u0627\u06cc\u062f \u0641\u0642\u0637 \u06cc\u06a9 JSON \u0645\u0639\u062a\u0628\u0631 \u0628\u0627 \u0627\u06cc\u0646 \u0633\u0627\u062e\u062a\u0627\u0631 \u0628\u0627\u0634\u062f:\n"
        "{\n"
        '  "images": [\n'
        '    {"role": "cover", "purpose": "\u0646\u0645\u0627\u06cc\u0634 \u0645\u0641\u0647\u0648\u0645 \u0627\u0635\u0644\u06cc \u0645\u0642\u0627\u0644\u0647", "prompt": "visual prompt ({{ prompt_language }})", '
        '"aspect_ratio": "16:9", "alt_text": "\u062a\u0648\u0636\u06cc\u062d alt \u0641\u0627\u0631\u0633\u06cc \u0637\u0628\u06cc\u0639\u06cc \u0648 \u063a\u06cc\u0631\u06a9\u0644\u06cc\u0634\u0647\u200c\u0627\u06cc", "caption": "\u0628\u0631\u06cc\u062f\u0647\u200c\u0627\u06cc \u062a\u0648\u0635\u06cc\u0641\u06cc \u06cc\u0627 \u062e\u0627\u0644\u06cc"},\n'
        '    {"role": "interior", "section_key": "section-1", "purpose": "\u0686\u0631\u0627 \u0627\u06cc\u0646 \u0628\u062e\u0634 \u0628\u0647 \u062a\u0635\u0648\u06cc\u0631 \u0646\u06cc\u0627\u0632 \u062f\u0627\u0631\u062f", '
        '"prompt": "visual prompt ({{ prompt_language }})", "aspect_ratio": "16:9", '
        '"alt_text": "\u062a\u0648\u0636\u06cc\u062d alt \u0641\u0627\u0631\u0633\u06cc", "caption": "\u0628\u0631\u06cc\u062f\u0647\u200c\u0627\u06cc \u062a\u0648\u0635\u06cc\u0641\u06cc \u06cc\u0627 \u062e\u0627\u0644\u06cc"}\n'
        "  ]\n"
        "}\n"
        "\u0642\u0648\u0627\u0646\u06cc\u0646: \u062f\u0642\u06cc\u0642\u0627\u064b \u06cc\u06a9 cover \u0627\u0644\u0632\u0627\u0645\u06cc \u0627\u0633\u062a (\u0646\u0633\u0628\u062a \u06f1\u06f6:\u06f9). \u062d\u062f\u0627\u06a9\u062b\u0631 {{ max_interior_images }} \u062a\u0635\u0648\u06cc\u0631 \u062f\u0627\u062e\u0644\u06cc — "
        "\u0641\u0642\u0637 \u0628\u0631\u0627\u06cc \u0628\u062e\u0634\u200c\u0647\u0627\u06cc\u06cc \u06a9\u0647 \u0645\u0641\u0647\u0648\u0645 \u0628\u0635\u0631\u06cc \u0645\u0647\u0645\u06cc \u062f\u0627\u0631\u0646\u062f (\u0646\u0645\u0648\u062f\u0627\u0631\u060c \u0645\u0631\u0627\u062d\u0644\u060c \u0645\u0642\u0627\u06cc\u0633\u0647\u060c \u0633\u0627\u062e\u062a\u0627\u0631). \u062a\u0635\u0627\u0648\u06cc\u0631 \u062a\u0632\u0626\u06cc\u0646\u06cc \u0648 \u062a\u06a9\u0631\u0627\u0631\u06cc \u0645\u0645\u0646\u0648\u0639. "
        "spacing: \u062a\u0635\u0627\u0648\u06cc\u0631 \u062f\u0627\u062e\u0644\u06cc \u0646\u0628\u0627\u06cc\u062f \u062f\u0631 \u0628\u062e\u0634\u200c\u0647\u0627\u06cc \u0645\u062a\u0648\u0627\u0644\u06cc \u067e\u0634\u062a\u200c\u0633\u0631\u0647\u0645 \u0628\u06cc\u0627\u06cc\u0646\u062f \u0645\u06af\u0631 \u0627\u06cc\u0646\u06a9\u0647 \u0648\u0627\u0642\u0639\u0627\u064b \u0644\u0627\u0632\u0645 \u0628\u0627\u0634\u0646\u062f. "
        "alt_text \u062a\u0648\u0635\u06cc\u0641 \u0637\u0628\u06cc\u0639\u06cc \u0635\u062d\u0646\u0647 \u0627\u0633\u062a (\u062a\u06a9\u0631\u0627\u0631 \u0639\u0646\u0648\u0627\u0646 \u0645\u0642\u0627\u0644\u0647 \u06cc\u0627 \u067e\u0631 \u06a9\u0631\u062f\u0646 \u06a9\u0644\u0645\u0647 \u06a9\u0644\u06cc\u062f\u06cc \u0645\u0645\u0646\u0648\u0639) \u0648 \u0647\u06cc\u0686\u200c\u06af\u0627\u0647 \u0645\u062a\u0646 \u067e\u0631\u0627\u0645\u067e\u062a \u0631\u0627 \u0644\u0648 \u0646\u0645\u06cc\u200c\u062f\u0647\u062f. "
        "\u062f\u0631 \u067e\u0631\u0627\u0645\u067e\u062a \u062a\u0635\u0648\u06cc\u0631\u06cc \u0647\u06cc\u0686 \u0646\u0648\u0634\u062a\u0647/\u062d\u0631\u0648\u0641/\u06a9\u0644\u0645\u0647\u200c\u0627\u06cc \u062f\u0627\u062e\u0644 \u062a\u0635\u0648\u06cc\u0631 \u0646\u0645\u06cc\u200c\u062e\u0648\u0627\u0647\u06cc\u0645. \u0647\u06cc\u0686 \u062a\u0648\u0636\u06cc\u062d\u06cc \u062e\u0627\u0631\u062c \u0627\u0632 JSON \u0646\u0646\u0648\u06cc\u0633."
    ),
}


# ---------------------------------------------------------------------------
# Bootstrap
# ---------------------------------------------------------------------------
def import_collections(pb: PocketBase) -> None:
    specs = [dict(c) for c in COLLECTIONS]  # type: ignore[var-annotated]
    # PocketBase 0.23 does NOT auto-add the system fields (id/created/updated)
    # when a collection is created with an explicit `fields` array — the list
    # replaces them. Fresh databases need them appended (indexes and every
    # `-created` sort reference them); but a LIVE database whose collections
    # already carry the system fields must NOT receive them again, or the
    # import fails with "duplicate column name: created". So: append each
    # system field only when it is missing from BOTH the spec and the live
    # collection.
    try:
        by_name = {c.name: c.id for c in pb.collections.get_full_list()}
    except Exception:
        by_name = {}
    live_fields: dict[str, set[str]] = {}
    try:
        for c in pb.collections.get_full_list():
            live_fields[c.name] = {str(f.get("name")) for f in c.fields}
    except Exception:
        live_fields = {}

    system_specs = {
        "id": {
            "name": "id",
            "type": "text",
            "required": False,
            "system": True,
            "hidden": False,
            "presentable": False,
            "primaryKey": True,
            "autogeneratePattern": "[a-z0-9]{15}",
            "pattern": "",
            "min": 0,
            "max": 15,
        },
        "created": {
            "name": "created",
            "type": "date",
            "required": False,
            "system": True,
            "hidden": False,
            "presentable": False,
            "onCreate": True,
        },
        "updated": {
            "name": "updated",
            "type": "date",
            "required": False,
            "system": True,
            "hidden": False,
            "presentable": False,
            "onUpdate": True,
        },
    }
    for spec in specs:
        names = {f.get("name") for f in spec.get("fields", [])}
        existing = live_fields.get(spec["name"], set())
        for sys_name in ("id", "created", "updated"):
            if sys_name not in names and sys_name not in existing:
                spec.setdefault("fields", []).append(system_specs[sys_name])

    # Relation fields must reference either an existing collection ID (live
    # DB) or a batch id (`pbc_…`) when the batch creates the collections from
    # scratch — name references are rejected by PocketBase >= 0.23.
    batch_ids = {s["name"]: f"pbc_{zlib.crc32(('base' + s['name']).encode()):08x}" for s in specs}
    for spec in specs:
        spec["id"] = by_name.get(spec["name"]) or batch_ids[spec["name"]]
        for field in spec.get("fields", []):
            if field.get("type") == "relation":
                ref = str(field.get("collectionId") or "")
                if ref in by_name:
                    field["collectionId"] = by_name[ref]
                elif ref in batch_ids:
                    field["collectionId"] = batch_ids[ref]
    try:
        pb.collections.import_collections(collections=specs, delete_missing=False)  # type: ignore[arg-type]
    except Exception as exc:
        raise RuntimeError(
            f"collection import failed: {exc}. "
            "Is PocketBase reachable and the admin credentials valid? "
            "Requires PocketBase >= 0.23."
        ) from exc


def ensure_users_fields(pb: PocketBase) -> None:
    """Add role + display_name to the built-in users collection if missing."""
    fields = list(pb.collections.get_one("users").fields)
    names = {f.get("name") for f in fields}
    additions = [
        {
            "name": "role",
            "type": "select",
            "required": False,
            "system": False,
            "hidden": False,
            "presentable": False,
            "help": "",
            "maxSelect": 1,
            "values": ["admin", "member"],
        },
        {
            "name": "displayName",
            "type": "text",
            "required": False,
            "system": False,
            "hidden": False,
            "presentable": False,
            "help": "",
            "primaryKey": False,
            "autogeneratePattern": "",
            "pattern": "",
            "min": 0,
            "max": 0,
        },
    ]
    added = [f for f in additions if f["name"] not in names]
    if added:
        pb.collections.update("users", {"fields": fields + added})
        print("users collection: added", [f["name"] for f in added])


def ensure_users_rules(pb: PocketBase) -> None:
    """Lock the built-in users collection to superuser-only API access (18-A).

    Only rule keys are sent (never ``fields`` — the SDK snake-cases field
    metadata on read, so round-tripping fields would corrupt them). Login and
    auth_refresh use PocketBase auth endpoints, which are not gated by these
    rules; the app reads/writes users through the superuser client. Verify
    against a live instance after bootstrap.
    """
    users = pb.collections.get_one("users")
    rules = {
        "listRule": users.list_rule,
        "viewRule": users.view_rule,
        "createRule": users.create_rule,
        "updateRule": users.update_rule,
        "deleteRule": users.delete_rule,
    }
    # None means "no rule" (public in PocketBase); "" means superuser-only.
    if any(v != "" for v in rules.values()):
        pb.collections.update(
            "users",
            {"listRule": "", "viewRule": "", "createRule": "", "updateRule": "", "deleteRule": ""},
        )
        print("users collection: API rules locked to superuser-only")


def seed_defaults(pb: PocketBase) -> None:
    # app_settings singleton
    try:
        existing = pb.collection("app_settings").get_first_list_item(
            'key="default"', {"perPage": 1}
        )
    except Exception:
        existing = None
    if not existing:
        pb.collection("app_settings").create(
            {
                "key": "default",
                "value": {
                    "default_llm_provider": "openai_compat",
                    "default_embedding_provider": "cohere",
                    "default_embedding_model": "embed-v4.0",
                    "default_embedding_dimensions": 1024,
                    "heartbeat_interval": 15,
                    "llm": {
                        "outline": {
                            "provider": "openai_compat",
                            "model": "gpt-4o-mini",
                            "temperature": 0.7,
                            "max_tokens": 4096,
                            "timeout": 120,
                        },
                        "section": {
                            "provider": "openai_compat",
                            "model": "gpt-4o-mini",
                            "temperature": 0.7,
                            "max_tokens": 4096,
                            "timeout": 120,
                        },
                        "meta": {"provider": "", "model": ""},
                        "review": {"provider": "", "model": ""},
                    },
                },
            }
        )
        print("app_settings: seeded")

    # global default prompts (project unset)
    for ptype, content in DEFAULT_PROMPTS.items():
        _upsert_prompt(pb, project_id="", ptype=ptype, name="default", content=content)
    print("prompts: global defaults seeded")


def _upsert_prompt(
    pb: PocketBase, *, project_id: str, ptype: str, name: str, content: str, updated_by: str = ""
) -> None:
    """Append-style versioning: insert a NEW row with version+1, deactivate the old.

    Idempotent: when the latest version already has identical content, nothing
    changes — re-bootstraps never reset customized or already-current prompts.
    """
    proj_filter = 'project=""' if not project_id else f'project="{project_id}"'
    try:
        latest = pb.collection("prompts").get_first_list_item(
            f'{proj_filter} && type="{ptype}" && name="{name}"', {"sort": "-version", "perPage": 1}
        )
    except Exception:
        latest = None
    if latest is not None and (latest.content or "") == content:
        # already seeded with this exact content — leave the active row alone
        return
    try:
        active = pb.collection("prompts").get_first_list_item(
            f'{proj_filter} && type="{ptype}" && name="{name}" && active=true', {"perPage": 1}
        )
    except Exception:
        active = None
    version = int(getattr(latest, "version", 0) or 0) + 1
    if active:
        pb.collection("prompts").update(active.id, {"active": False})
    pb.collection("prompts").create(
        {
            "project": project_id or None,
            "type": ptype,
            "name": name,
            "content": content,
            "version": version,
            "active": True,
            "variables": {},
            "updatedBy": updated_by,
        }
    )


def seed_admin_user(pb: PocketBase) -> None:
    email = os.getenv("SEED_ADMIN_EMAIL", "admin@ezdistro.local")
    password = os.getenv("SEED_ADMIN_PASSWORD", "")
    if not password:
        print("SKIP: SEED_ADMIN_PASSWORD not set — no seed admin user created.")
        return
    try:
        pb.collection("users").get_first_list_item(f'email="{email}"', {"perPage": 1})
        print("seed admin already exists:", email)
        return
    except Exception:
        pass
    pb.collection("users").create(
        {
            "email": email,
            "password": password,
            "passwordConfirm": password,
            "role": "admin",
            "displayName": "\u0645\u062f\u06cc\u0631 EzDistro",
            "verified": True,
        }
    )
    print(f"seed admin created: {email} (CHANGE THE PASSWORD IMMEDIATELY!)")


def main() -> None:
    if not settings.pb_admin_email or not settings.pb_admin_password:
        print("ERROR: PB_ADMIN_EMAIL and PB_ADMIN_PASSWORD env vars are required.", file=sys.stderr)
        sys.exit(1)
    pb = PocketBase(settings.pb_url, auto_snake_case=False)
    try:
        pb.collection("_superusers").auth_with_password(
            settings.pb_admin_email, settings.pb_admin_password
        )
    except Exception:
        pb.collection("_admins").auth_with_password(
            settings.pb_admin_email, settings.pb_admin_password
        )

    import_collections(pb)
    time.sleep(0.5)
    ensure_users_fields(pb)
    ensure_users_rules(pb)
    seed_defaults(pb)
    seed_admin_user(pb)
    print("✓ bootstrap complete")


if __name__ == "__main__":
    main()
