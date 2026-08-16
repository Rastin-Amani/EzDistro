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
from typing import Any

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from pocketbase import PocketBase  # noqa: E402

from app.config import settings  # noqa: E402

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
AUTH_RULE = "@request.auth.id != ''"


def col(
    name: str,
    fields: list[dict[str, Any]],
    indexes: list[str] | None = None,
    list_rule: str = AUTH_RULE,
    view_rule: str = AUTH_RULE,
    create_rule: str = AUTH_RULE,
    update_rule: str = AUTH_RULE,
    delete_rule: str = AUTH_RULE,
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


def t(name: str, *, required: bool = False, unique: bool = False) -> dict[str, Any]:
    opts: dict[str, Any] = {"min": None, "max": None, "pattern": ""}
    if unique:
        opts["unique"] = True
    return {"name": name, "type": "text", "required": required, "options": opts}


def num(name: str, *, required: bool = False) -> dict[str, Any]:
    return {
        "name": name,
        "type": "number",
        "required": required,
        "options": {"min": None, "max": None, "step": None},
    }


def boolean(name: str, *, required: bool = False) -> dict[str, Any]:
    return {"name": name, "type": "bool", "required": required, "options": {}}


def date(name: str, *, required: bool = False) -> dict[str, Any]:
    return {"name": name, "type": "date", "required": required, "options": {"min": "", "max": ""}}


def json_field(name: str, *, required: bool = False) -> dict[str, Any]:
    return {"name": name, "type": "json", "required": required, "options": {"maxSize": 0}}


def select(
    name: str, values: list[str], *, required: bool = False, max_select: int = 1
) -> dict[str, Any]:
    return {
        "name": name,
        "type": "select",
        "required": required,
        "options": {"maxSelect": max_select, "values": values},
    }


def rel(
    name: str, collection: str, *, required: bool = False, cascade: bool = False
) -> dict[str, Any]:
    return {
        "name": name,
        "type": "relation",
        "required": required,
        "options": {
            "collectionId": collection,
            "cascadeDelete": cascade,
            "minSelect": None,
            "maxSelect": 1,
        },
    }


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
            json_field("indexing"),
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
                ["llm", "embedding", "reranker", "vector_store", "publisher"],
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
            rel("articleId", "articles"),  # no cascade; cleared in app code
        ],
        indexes=[
            "CREATE INDEX idx_topics_project_status_priority ON topics (project, status, priority)",
            "CREATE INDEX idx_topics_project_status ON topics (project, status)",
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
                    "publishing",
                    "published",
                    "failed",
                ],
                required=True,
            ),
            num("outlineVersion"),
            json_field("outline"),
            json_field("validation"),
            t("finalHtml"),
            t("generatedContent"),
            num("lastGeneratedRevision"),
            t("reviewNote"),
            t("metaDescription"),
            num("seoScore"),
            num("wordCount"),
            date("generatedAt"),
            date("publishedAt"),
            num("wordpressPostId"),
            t("wordpressUrl"),
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
            num("position", required=True),
            t("heading", required=True),
            t("contentBrief"),
            json_field("internalLinks"),
            t("content"),
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
        "تو یک نویسنده ارشد محتوای سئو هستی. متن‌های تو دقیق، ساختاریافته و بی‌نقص هستند. "
        "همیشه به زبان فارسی روان و استاندارد بنویس. هرگز اطلاعات نادرست یا حدسی منتشر نکن؛ "
        "فقط بر اساس منابع ارائه‌شده مطلب بنویس و اگر اطلاعاتی در منابع نیست، نگو."
    ),
    "outline_system": (
        "تو یک استراتژیست محتوای سئو هستی. برای موضوع داده‌شده رئوس مطالب (outline) حرفه‌ای "
        "تولید می‌کنی. خروجی تو همیشه JSON معتبر است و هیچ چیز خارج از JSON نمی‌نویسی."
    ),
    "outline_user": (
        "برای موضوع زیر، یک رئوس مطالب حرفه‌ای برای مقاله سئو تولید کن.\n"
        "## موضوع مقاله\n"
        "عنوان: {{ topic.title }}\n"
        "کلمه کلیدی: {{ topic.keyword }}\n"
        "نوع: {{ topic.type }}\n"
        "زبان: {{ language }}\n\n"
        "## قوانین سئو\n{{ seo_rules }}\n\n"
        "## قوانین لینک‌سازی داخلی\n{{ internal_linking_rules }}\n\n"
        "## مقالات مرتبط برای لینک داخلی\n{{ internal_links }}\n\n"
        "## زمینه بازیابی (فقط نتایج مرتبط)\n{{ retrieved_context }}\n\n"
        "خروجی باید فقط یک JSON معتبر با این ساختار باشد (کلیدها دقیقاً همین باشند):\n"
        "{\n"
        '  "title": "عنوان پیشنهادی مقاله",\n'
        '  "slug": "slug-انگلیسی-کوتاه",\n'
        '  "sections": [\n'
        '    {"heading": "تیتر بخش", "content_brief": "خلاصه محتوای این بخش در ۲ تا ۴ جمله", '
        '"internal_links": [{"title": "عنوان مقاله مرتبط", "url": "https://example.com/related", "anchor_text": "متن لینک"}]},\n'
        "    ...\n"
        "  ]\n"
        "}\n"
        "قوانین: ۴ تا ۸ بخش اصلی با تیتر h2؛ content_brief باید تمام نکات کلیدی بخش را پوشش دهد؛ "
        "سرفصل‌ها مختصر و حاوی کلمه کلیدی؛ اگر مقاله مرتبطی وجود ندارد internal_links را خالی بگذار. "
        "هیچ توضیحی خارج از JSON ننویس."
    ),
    "section_system": (
        "تو یک نویسنده بخش‌های مقاله سئو هستی. هر بخش را به‌صورت HTML معتبر و بدون هیچ توضیح "
        "اضافه‌ای تولید می‌کنی."
    ),
    "section_user": (
        "یک بخش از مقاله سئو را بنویس.\n"
        "خروجی باید فقط HTML معتبر باشد: تیتر بخش با <h2> و محتوا با <p>، <ul>، <ol>، <strong>، <em> و <a>.\n"
        "هیچ استایل inline، تگ <html>، <body> یا تیتر <h1> استفاده نکن. بدون توضیح اضافه، فقط HTML.\n"
        "پاراگراف‌ها کوتاه باشند (۲ تا ۴ جمله) و کلمه کلیدی به‌طور طبیعی در متن تکرار شود."
    ),
    "seo_rules": (
        "قوانین سئو: کلمه کلیدی اصلی در عنوان، تیترها و اولین پاراگراف بیاید؛ تراکم کلمه کلیدی طبیعی باشد "
        "(نه تقلبی)؛ از تیترهای سلسله‌مراتب h2/h3 استفاده کن؛ لینک‌های داخلی مرتبط اضافه کن؛ "
        "متا توضیحات جذاب و کوتاه بنویس؛ متن را برای خواندن سریع (اسکن) ساختاربندی کن."
    ),
    "internal_linking": (
        "در صورت وجود مقالات مرتبط، یک بخش «مطالب مرتبط» با لینک داخلی به آن‌ها در انتهای مقاله اضافه کن. "
        "لینک‌ها فقط به آدرس‌های تأییدشده در لیست مقالات موجود باشند."
    ),
    "validation": (
        "خروجی زیر از یک مدل زبانی گرفته شده و باید JSON معتبر با ساختار مشخص‌شده باشد.\n"
        "اگر خطا یا متن اضافه دارد، فقط نسخه اصلاح‌شده JSON را برگردان (بدون توضیح).\n"
        "ساختار مورد انتظار:\n"
        '{"title": string, "slug": string, "sections": [{"heading": string, "content_brief": string, "internal_links": [{"title": string, "url": string, "anchor_text": string}]}]}\n'
        "خروجی معیوب:\n"
        "{{ raw_output }}"
    ),
}


# ---------------------------------------------------------------------------
# Bootstrap
# ---------------------------------------------------------------------------
def import_collections(pb: PocketBase) -> None:
    try:
        specs = [c for c in COLLECTIONS]  # type: ignore[var-annotated]
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
            "options": {"maxSelect": 1, "values": ["admin", "member"]},
        },
        {
            "name": "displayName",
            "type": "text",
            "required": False,
            "options": {"min": None, "max": None, "pattern": ""},
        },
    ]
    added = [f for f in additions if f["name"] not in names]
    if added:
        pb.collections.update("users", {"fields": fields + added})
        print("users collection: added", [f["name"] for f in added])


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
    """Append-style versioning: insert a NEW row with version+1, deactivate the old."""
    proj_filter = 'project=""' if not project_id else f'project="{project_id}"'
    try:
        active = pb.collection("prompts").get_first_list_item(
            f'{proj_filter} && type="{ptype}" && name="{name}" && active=true', {"perPage": 1}
        )
    except Exception:
        active = None
    try:
        latest = pb.collection("prompts").get_first_list_item(
            f'{proj_filter} && type="{ptype}" && name="{name}"', {"sort": "-version", "perPage": 1}
        )
        version = int(getattr(latest, "version", 0) or 0) + 1
    except Exception:
        version = 1
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
    email = os.getenv("SEED_ADMIN_EMAIL", "admin@seoz.local")
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
            "displayName": "مدیر سئوز",
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
    seed_defaults(pb)
    seed_admin_user(pb)
    print("✓ bootstrap complete")


if __name__ == "__main__":
    main()
