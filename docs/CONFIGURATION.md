# Seoz Platform — Configuration Reference

All configuration is environment-driven (`pydantic-settings`, `app/config.py`) plus
per-project settings stored in PocketBase and editable in the UI. This document
lists every knob, its default, and where it takes effect.

```text
Documentation status:  Verified against app/config.py, .env.example,
                       app/services/settings.py, app/scripts/bootstrap_pb.py
                       (post-v1.3.0, images pipeline)
Last verified:         2026-09-12
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
| `PB_URL` | `https://db.seoz.rastin.cloud` | Source-of-truth server; both processes need it |
| `PB_ADMIN_EMAIL` / `PB_ADMIN_PASSWORD` | *(empty)* | Superuser auth for worker + bootstrap (auth falls back `_superusers` → `_admins` for PB < 0.23) |
| `SEED_ADMIN_EMAIL` | `admin@seoz.local` | Platform admin user created by bootstrap when password set |
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

### Feature flags & misc

| Variable | Default | Effect |
|---|---|---|
| `OLLAMA_ENABLED` | `0` | `1` exposes Ollama (`/v1`) as a selectable LLM provider |
| `PROVIDER_EVENTS_ENABLED` | `0` | `1` records every provider call as a `provider_call` job event |

Not exposed via env (code constants): UI page size `25`
(`settings.page_size`), stats TTL cache 5 s, metrics flush interval 30 s,
adapter retry defaults (3 attempts, base delay 2 s), index checkpoint every 10
documents, WP fetch page size 100.

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

### Publishing

| Setting | Values | Notes |
|---|---|---|
| publishingMode | `draft` (default) \| `publish` | Status used when creating/updating WP posts |
| autosave | `{enabled, interval_minutes}` | Draft autosave descriptor |

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
| embedding | `cohere`, `openai_compat` | API key |
| reranker | `cohere_compat` | API key |
| vector_store | `qdrant` | optional API key |
| publisher | `wordpress` | application password |
| image (v1.3.0) | `gemini`, `bfl`, `openai_compat` | API key |

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

Ten prompt types per project (falling back to global rows):
`outline_system`, `outline_user`, `section_system`, `section_user`, `seo_rules`,
`internal_linking`, `brand_voice`, `validation`, `image_plan_system`,
`image_plan_user` (v1.3.0). The image-plan prompts accept `{{ article.title }}`,
`{{ topic.keyword }}`, `{{ language }}`, `{{ prompt_language }}`,
`{{ sections }}`, `{{ style_profile }}`, `{{ max_interior_images }}`,
`{{ raw_output }}` (repair pass).

Variables usable inside prompts (unknown names are rejected before any LLM call):

`{{ project.name }}` · `{{ project.slug }}` · `{{ project.language }}` ·
`{{ topic.title }}` · `{{ topic.keyword }}` · `{{ topic.pillar }}` ·
`{{ topic.cluster }}` · `{{ topic.type }}` · `{{ article.title }}` ·
`{{ article.slug }}` · `{{ article.meta_description }}` · `{{ section.heading }}` ·
`{{ section.content_brief }}` · `{{ retrieved_context }}` · `{{ internal_links }}` ·
`{{ seo_rules }}` · `{{ internal_linking_rules }}` · `{{ language }}` ·
`{{ raw_output }}` (validation prompt only).

Every save appends a new version; activating an old version is the rollback path.
Bootstrap seeds Persian global defaults for all eight types.
