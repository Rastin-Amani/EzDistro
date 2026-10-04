# EzDistro Platform — Configuration Reference

All configuration is environment-driven (`pydantic-settings`, `app/config.py`) plus
per-project settings stored in PocketBase and editable in the UI. This document
lists every knob, its default, and where it takes effect.

```text
Documentation status:  Verified against app/config.py, .env.example,
                       app/services/settings.py, app/scripts/bootstrap_pb.py
                       (research engine)
Last verified:         2026-10-04
```

## 1. Environment variables

Copy `.env.example` to `.env`. Values live on the same line as the key; avoid trailing
whitespace/comments after empty values (pydantic-settings captures everything after
`=`).

### App

| Variable | Default | Effect |
|---|---|---|
| `ENV` | `dev` | `dev` \| `production`. Production: JSON logs, requires `SECRETS_KEY`, hides `/docs` + `/openapi.json` + debug router |
| `APP_VERSION` | `0.2.0` | Shown in UI footer / `/health` |
| `SECRETS_KEY` | *(empty)* | Fernet key (32-byte urlsafe base64). **Required in production** — startup fails without it. Dev derives a deterministic key from `pb_url`+env. Generate: `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`. ⚠️ Rotating it invalidates all stored integration secrets (they must be re-entered) |

### PocketBase

| Variable | Default | Effect |
|---|---|---|
| `PB_URL` | `https://db.ezdistro.space` | Source-of-truth server; both processes need it |
| `PB_ADMIN_EMAIL` / `PB_ADMIN_PASSWORD` | *(empty)* | Superuser auth for worker + bootstrap (auth falls back `_superusers` → `_admins` for PB < 0.23) |
| `SEED_ADMIN_EMAIL` | `admin@ezdistro.local` | Platform admin user created by bootstrap when password set |
| `SEED_ADMIN_PASSWORD` | *(empty)* | Empty ⇒ no seed user is created |

### Qdrant (defaults)

| Variable | Default | Effect |
|---|---|---|
| `QDRANT_URL` | `http://127.0.0.1:6333` | Used when a project has no vector_store integration of its own |
| `QDRANT_API_KEY` | *(empty)* | Optional |

### SSRF guard

| Variable | Default | Effect |
|---|---|---|
| `ALLOW_PRIVATE_NETWORKS` | `0` | `1` permits private/loopback targets — needed only to run against localhost Qdrant/WordPress/PocketBase in development |

### Worker process

| Variable | Default | Effect |
|---|---|---|
| `WORKER_ID` | auto (`uuid4` hex[:12]) | Claim identity in leases/logs |
| `POLL_INTERVAL_SECONDS` | `5` | Job-poll cadence (floor 1 s) |
| `LEASE_SECONDS` | `300` | Job lease duration; expired lease ⇒ another worker may reclaim |
| `HEARTBEAT_INTERVAL_SECONDS` | `15` | Lease extension cadence while running |
| `MAX_CONCURRENT_JOBS` | `4` | Global semaphore across job executions per worker |
| `SCHEDULE_POLL_INTERVAL_SECONDS` | `60` | Schedule loop cadence (floor 10 s) |

Defined but currently unused: `schedule_window_minutes` (config field with no reader).
Do not rely on it.

### Provider concurrency (rate limits per worker)

| Variable | Default | Effect |
|---|---|---|
| `LLM_CONCURRENCY` | `4` | Max concurrent LLM calls across all jobs on this worker |
| `EMBEDDING_CONCURRENCY` | `4` | Max concurrent embedding batches |
| `PUBLISH_CONCURRENCY` | `2` | Max concurrent WordPress publishes |

### SEO research engine (optional feature)

> **Google Ads OAuth client credentials are configured per project on the
> project's Connections tab** (integration category `google_ads`), not in the
> environment — they may differ per project. The vars below are an optional
> **fallback** used only when a project has no enabled Google Ads connection
> (e.g. one shared client across all projects).

| Variable | Default | Effect |
|---|---|---|
| `GOOGLE_ADS_CLIENT_ID` | — | Fallback Google Cloud OAuth client id |
| `GOOGLE_ADS_CLIENT_SECRET` | — | Fallback OAuth client secret (never exposed client-side) |
| `GOOGLE_ADS_REDIRECT_URI` | — | Fallback callback URL, e.g. `https://…/auth/google-ads/callback` (both `/auth/google-ads/callback` and `/projects/google-ads/callback` are served) |
| `GOOGLE_ADS_API_VERSION` | `v25` | Google Ads API version; versions sunset annually, keep configurable |
| `GOOGLE_ADS_LOGIN_CUSTOMER_ID` | — | Optional manager (MCC) id used as `login-customer-id` |
| `GOOGLE_ADS_DEVELOPER_TOKEN` | — | Legacy: developer tokens were sunset 2026-09-09; sent only if set, ignored by Google |
| `RESEARCH_MAX_KEYWORDS_PER_RUN` | `0` (= uncapped) | Google Ads keyword budget per run (`.env.example` suggests `5000`) |
| `RESEARCH_MAX_COMPETITOR_PAGES` | `0` (= uncapped) | Crawl budget per run (`.env.example` suggests `200`) |
| `RESEARCH_MAX_SERP_QUERIES` | `0` | Live SERP budget per run (`0` = no live SERP queries) |
| `RESEARCH_CRAWL_CONCURRENCY` | `4` | Competitor crawl parallelism |
| `RESEARCH_GOOGLE_ADS_CONCURRENCY` | `2` | Keyword Planning parallelism (tight rate limits) |

> **Note:** the code defaults for the keyword/crawl caps are `0` (no cap). The
> commented values in `.env.example` (`5000`/`200`) are *recommended* production
> caps, not the defaults.

Google Ads credentials resolve per project via the enabled `google_ads`
integration (client id / redirect URI / API version / login customer id in
`configuration`; client secret and developer token encrypted in `secretsEnc`),
falling back to the env vars above only when no connection exists. All research
env vars are optional — the engine works without Google Ads (competitor-only)
and without a SERP provider. See [SEO_RESEARCH.md](SEO_RESEARCH.md).

### Feature flags & misc
| Variable | Default | Effect |
|---|---|---|
| `OLLAMA_ENABLED` | `0` | `1` exposes Ollama (`/v1`) as a selectable LLM provider |
| `PROVIDER_EVENTS_ENABLED` | `0` | `1` records every provider call as a `provider_call` job event |

Defined but currently **unused**: `PAGE_SIZE` (`settings.page_size`, default 25 —
no reader; `.env.example` correctly says not to set it) and `SCHEDULE_WINDOW_MINUTES`
(no reader). Not exposed via env (code constants): stats TTL cache 5 s, metrics flush
interval 30 s, adapter retry defaults (3 attempts, base delay 2 s), index checkpoint
every 10 documents, WP fetch page size 100, Google Ads Keyword Planning page size
10 000.

## 2. Per-project settings (project_settings collection)

Editable in the project's **Settings** tab; consumed by jobs through `ProjectConfig`.
Empty fields fall back to runtime defaults at read time.

### AI models (per role)

Roles: `outline`, `section`, optional `meta` and `review`. Resolution order per role:

```
project override (outlineProvider/Model/Temperature/MaxTokens/Timeout)
  → global default (app_settings.value.llm.<role>)
    → legacy fields (defaultLlmProvider/defaultLlmModel)
      → hardcoded fallbacks: openai_compat · gpt-4o-mini · temp 0.7 · 4096 tok · 120 s
```

Per-role retry JSON `{max_attempts, backoff_base}` is stored but the effective retry
budget for section jobs comes from the project `retryPolicy` below.

The **AI Models** tab also offers model discovery from the provider API (OpenAI-compat
`GET /models`, Gemini `GET /models`; 10-minute TTL) and per-role test buttons.

### Embedding & indexing

| Setting | Runtime default | Notes |
|---|---|---|
| embeddingProvider / embeddingModel / embeddingDimensions | `openai_compat` / `text-embedding-3-small` / `1536` | ⚠️ Hardcoded fallbacks differ from the seeded global values (cohere / embed-v4.0 / 1024); embeddings do NOT fall back to global defaults — set them explicitly per project. Changing model/dimensions requires a re-index into the new namespace |
| chunkSize | `500` words | floor 50 at use site |
| chunkOverlap | `100` words | clamped below chunkSize |
| separatorStrategy | `auto` | `paragraph` / `sentence` / `word` also available |
| maxChunkCount | `0` = unlimited | caps chunks per document |

### Retrieval & context budget

| Setting | Runtime default | Notes |
|---|---|---|
| retrievalTopK | `20` | candidate pool from Qdrant (always filtered by project) |
| similarityThreshold | `0` (= disabled) | minimum vector score |
| rerankingEnabled | off | requires an active reranker integration |
| rerankerProvider / rerankerModel / rerankerTopN | `cohere_compat` / `rerank-v4.0` / `8` | |
| contextMaxLinks / contextMaxPassages / contextMaxChars | `5` / `5` / `4000` | prompt-context budget |

### Generation & validation

| Setting | Runtime default | Notes |
|---|---|---|
| minArticleWords | `300` | whole-article gate during assembly |
| generationConcurrency | `2` | ⚠️ stored/editable but not read by current engine code; effective parallelism = `MAX_CONCURRENT_JOBS` × `LLM_CONCURRENCY` |

### Retry policy

JSON `retryPolicy`: `{max_attempts: 3, backoff_base: 30, backoff_max: 3600}`.
Backoff = `min(base × 2^(attempts−1), cap)` with ±50 % jitter; provider `Retry-After`
always wins as a lower bound.

### Localization & brand (multilingual engine prompt context)

| Setting | Runtime default | Notes |
|---|---|---|
| targetLocale / targetCountry / targetAudience | empty | market + locale + reader injected into every engine prompt |
| brandName | empty (= project name) | |
| preferredTerminology / forbiddenTerminology | empty | comma-separated wording rules |
| urlPolicy | empty | slug convention for non-Latin scripts |
| productContext | empty | brand offering in 1–2 sentences |

### Publishing

| Setting | Values | Notes |
|---|---|---|
| publishingMode | `draft` (default) \| `publish` | Status used when creating/updating WP posts |
| autosave | `{enabled, interval_minutes}` | Draft autosave descriptor |
| autoPublish | json | Auto-publish rule descriptor |

### Image generation (v1.3.0 — project **Images** tab)

Model ids are data (persisted here), never hardcoded in the pipeline.

| Setting | Runtime default | Notes |
|---|---|---|
| imageCoverProvider / imageCoverModel | `gemini` / `gemini-3-pro-image` | cover slot |
| imageInteriorProvider / imageInteriorModel | `bfl` / `flux-2-klein-9b` | interior slots |
| imageFallbackProvider / imageFallbackModel | empty | empty = no fallback |
| imageCoverAspectRatio / imageInteriorAspectRatio | `16:9` | e.g. `4:3` |
| imageCoverMinWidth | `1200` | SEO/Google Discover floor |
| imageMaxInteriorImages | `4` | hard cap; word-count density further limits (0/1/2/3/4) |
| imageMaxRetries | `3` | generation job attempts = `max(2, this)` |
| imageOptimizationFormat | `webp` | `webp` \| `avif` \| `jpeg` |
| imageAiQaEnabled | off | optional vision QA (extra model cost) |
| imagePromptLanguage | `en` | visual-prompt language; alt/caption always use the article language |
| imageStyle | `{}` | style profile injected into the plan prompt |

The tab also has a one-click generation test (`…/settings/images/test`) that
replicates real cover-request dimensions.

## 3. Integrations (credentials)

Managed in the project **Integrations** tab; one row per `(category, provider, name)`:

| Category | Providers selectable | Secret stored |
|---|---|---|
| llm | `openai_compat`, `gemini`, `custom`, `ollama` (flag) | API key |
| embedding | `openai_compat` | API key (OpenAI-compatible; a legacy `cohere` value maps to this adapter) |
| reranker | `cohere_compat` | API key |
| vector_store | `qdrant` | optional API key |
| publisher | `wordpress` | application password |
| image (v1.3.0) | `gemini`, `bfl`, `openai_compat` | API key |
| serp (research) | `serper`, `none` | API key (optional) |
| google_ads (research) | `google_ads` | OAuth client secret (+ optional legacy developer token) |

Non-secret config (base URL, username, model id…) lives in the plain `configuration`
JSON; secrets are Fernet-encrypted into `secretsEnc` and never rendered beyond a
masked preview. Exactly one enabled integration per category feeds the provider
registry. "Test connection" pings the provider and stores health status.

## 4. Global defaults (app_settings singleton)

Seeded by bootstrap (`key="default"`), consulted only for LLM role resolution today:

```json
{
  "default_llm_provider": "openai_compat",
  "default_embedding_provider": "cohere",
  "default_embedding_model": "embed-v4.0",
  "default_embedding_dimensions": 1024,
  "heartbeat_interval": 15,
  "llm": {
    "outline": {"provider": "openai_compat", "model": "gpt-4o-mini",
                 "temperature": 0.7, "max_tokens": 4096, "timeout": 120},
    "section": { … same … },
    "meta":    {"provider": "", "model": ""},
    "review":  {"provider": "", "model": ""}
  }
}
```

Editable via the AI Models tab's global-defaults form.

## 5. Prompts

Twenty-five prompt types per project (falling back to global rows), all seeded via
`DEFAULT_PROMPTS` in `app/scripts/bootstrap_pb.py` (`PROMPT_TYPES` in
`app/repositories/prompts.py` is the single source of the list):
`brand_voice`, `seo_content_contract`, `research_system`, `research_user`,
`outline_system`, `outline_user`, `section_system`, `section_user`,
`internal_linking`, `metadata_system`, `metadata_user`, `article_qa_system`,
`article_qa_user`, `article_repair_system`, `article_repair_user`,
`image_plan_system`, `image_plan_user`, `output_validation`,
`content_refresh_system`, `cluster_system`, `cluster_user`, `opportunity_system`,
`opportunity_user` — plus legacy aliases `seo_rules` (resolves the contract) and
`validation` (generic JSON repair) kept so existing projects and tests keep working. The image-plan prompts accept `{{ article.title }}`,
`{{ article.content }}`, `{{ topic.keyword }}`, `{{ language }}`,
`{{ locale }}`, `{{ prompt_language }}`, `{{ sections }}`,
`{{ style_profile }}`, `{{ max_interior_images }}`; the outline repair pass
uses `{{ output_schema }}` + `{{ raw_output }}`.

Variables usable inside prompts (unknown names are rejected before any LLM call):

`{{ project.name }}` · `{{ project.slug }}` · `{{ project.language }}` ·
`{{ topic.title }}` · `{{ topic.keyword }}` · `{{ topic.pillar }}` ·
`{{ topic.cluster }}` · `{{ topic.type }}` · `{{ article.title }}` ·
`{{ article.slug }}` · `{{ article.meta_description }}` · `{{ article.content }}` ·
`{{ section.heading }}` · `{{ section.content_brief }}` · `{{ section.position }}` ·
`{{ retrieved_context }}` · `{{ internal_links }}` ·
`{{ seo_rules }}` · `{{ internal_linking_rules }}` · `{{ brand_voice }}` ·
`{{ language }}` · `{{ locale }}` · `{{ audience }}` ·
`{{ raw_output }}` + `{{ output_schema }}` (output validation only) —
plus research/intent (`{{ search_intent }}`, `{{ research }}`, `{{ entities }}`…),
section detail (`{{ section.purpose }}`, `{{ section.required_points }}`…),
neighbours (`{{ previous_section }}`, `{{ next_section }}`), QA
(`{{ qa_issues }}`), image planning (`{{ prompt_language }}`, `{{ sections }}`,
`{{ style_profile }}`, `{{ max_interior_images }}`), site/market context
(`{{ site_context }}`, `{{ url_policy }}`…) — full registry in
`variable_labels()` (`app/domain/prompt_render.py`).

Every save appends a new version; activating an old version is the rollback path.
Bootstrap seeds English global defaults for all types.
