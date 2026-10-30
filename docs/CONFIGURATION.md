# EzDistro Platform — Configuration Reference

All configuration is environment-driven (`pydantic-settings`, `app/config.py`) plus
per-project settings stored in PocketBase and editable in the UI. This document
lists every knob that changes behaviour, its default, and where it takes effect.

```text
Documentation status:  Verified against app/config.py, .env.example,
                       app/services/settings.py, app/repositories/projects.py,
                       app/providers/registry.py, app/scripts/bootstrap_pb.py
Last verified:         2026-10-25 (v1.27.3)
```

## 1. Environment variables

Copy `.env.example` to `.env`. Values live on the same line as the key; never put a
trailing comment or whitespace after a value (pydantic-settings captures everything
after `=` as the value).

Two variables are read with `os.getenv` rather than through `Settings`:
`SEED_ADMIN_EMAIL` / `SEED_ADMIN_PASSWORD` (bootstrap only) and `ENV` in the log
setup. Values that exist only in `.env` are **not** in `os.environ`, so export
those on the command line when they matter:

```bash
SEED_ADMIN_EMAIL=you@example.com SEED_ADMIN_PASSWORD=... make bootstrap
# JSON logs on a systemd host:
Environment=ENV=production   # in the unit (the .env value is not enough there)
```

### App

| Variable | Default | Effect |
|---|---|---|
| `ENV` | `dev` | `dev` \| `production`. Production hides `/docs` + `/openapi.json` + the debug router and makes a missing `SECRETS_KEY` an error on first use. JSON logs are selected from the **process** environment (`os.getenv`). |
| `APP_NAME` | `EzDistro Platform` | Product name in the UI/`/health` |
| `APP_VERSION` | `1.30.1` | Version label in the UI and `/health` — tracks the release tag |
| `SECRETS_KEY` | *(empty)* | Fernet key (32-byte urlsafe base64). Required in production: there is no startup probe, but the first encrypted-secret operation raises. Dev derives a deterministic key from `pb_url`+env. Generate: `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`. ⚠️ Rotating it invalidates all stored integration secrets (they must be re-entered) |
| `OAUTH_CALLBACK_PATH` | `/auth/oauth2/callback` | Path of the OAuth callback below `PUBLIC_APP_URL`. Change only if a reverse proxy rewrites the path |

### PocketBase

| Variable | Default | Effect |
|---|---|---|
| `PB_URL` | `http://127.0.0.1:8090` | Source-of-truth server; both processes need it |
| `PB_ADMIN_EMAIL` / `PB_ADMIN_PASSWORD` | *(empty)* | Superuser auth for worker + bootstrap (auth falls back `_superusers` → `_admins` for PB < 0.23) |
| `SEED_ADMIN_EMAIL` | `admin@ezdistro.local` | Bootstrap-only (`os.getenv`): platform admin matched by email. Bootstrap promotes this account, or creates it (`verified`) when `SEED_ADMIN_PASSWORD` is set. The admin signs in with Google/GitHub using exactly this email — the password is never a login secret and only satisfies PocketBase's required field |
| `SEED_ADMIN_PASSWORD` | *(empty)* | Bootstrap-only (`os.getenv`); only used when the seed account does not exist yet. Empty ⇒ no seed user is created |

### Sign-in (OAuth-only: Google + GitHub)

There is no password login. PocketBase users authenticate through the two
OAuth2 providers; `make bootstrap` disables password, OTP and MFA auth and
writes the provider credentials below into the `users` collection. Configure
providers in the PocketBase dashboard instead if you prefer — bootstrap leaves
an already-configured provider untouched when the env values are empty.

| Variable | Default | Effect |
|---|---|---|
| `PUBLIC_APP_URL` | `http://localhost:8000` | Canonical public origin. It builds the OAuth callback **fallback** and the `oauth_callback_url`; the live callback follows the origin the browser actually reached (`https://<host>/auth/oauth2/callback`). Billing return URLs are configured in PayRam, not here. Register the callback URL **exactly** at both providers |
| `GOOGLE_OAUTH_CLIENT_ID` / `GOOGLE_OAUTH_CLIENT_SECRET` | *(empty)* | Google Cloud OAuth 2.0 Web client. Authorized redirect URI: `https://your-app.example.com/auth/oauth2/callback` |
| `GITHUB_OAUTH_CLIENT_ID` / `GITHUB_OAUTH_CLIENT_SECRET` | *(empty)* | GitHub OAuth App (Settings → Developer settings → OAuth Apps). Authorization callback URL: the same `/auth/oauth2/callback` |

> In Google Cloud Console the redirect URI belongs on the **same** OAuth client
> whose id/secret are stored in PocketBase. The provider dashboard shows the
> client id and secret once; PocketBase redacts the secret on read, so bootstrap
> resends providers without it and PocketBase merges by provider name.

**Setting up the two apps**

1. **Google** — Google Cloud Console → APIs & Services → Credentials → *Create
   credentials* → *OAuth client ID* → *Web application*. Add
   `https://your-app.example.com/auth/oauth2/callback` under **Authorized
   redirect URIs**. Copy the client id/secret to `GOOGLE_OAUTH_CLIENT_ID` /
   `GOOGLE_OAUTH_CLIENT_SECRET` (or paste them in PocketBase → Collections →
   `users` → Options → OAuth2 → Google).
2. **GitHub** — GitHub → Settings → Developer settings → OAuth Apps → *New OAuth
   App*. Homepage URL: `https://your-app.example.com`. Authorization callback
   URL: `https://your-app.example.com/auth/oauth2/callback`. Copy the client
   id/secret to `GITHUB_OAUTH_CLIENT_ID` / `GITHUB_OAUTH_CLIENT_SECRET` (or add
   the provider in the PocketBase dashboard).
3. Run `make bootstrap`. It prints
   `users collection: sign-in providers = google, github` and disables every
   other auth method. The login page only renders providers that PocketBase
   reports as enabled, so a missing credential shows one button, not a broken
   one.

### Qdrant (defaults)

| Variable | Default | Effect |
|---|---|---|
| `QDRANT_URL` | `http://127.0.0.1:6333` | Used when a project has no vector_store integration of its own |
| `QDRANT_API_KEY` | *(empty)* | Optional |

### SSRF guard

| Variable | Default | Effect |
|---|---|---|
| `ALLOW_PRIVATE_NETWORKS` | `0` | `1` permits private/loopback targets — needed only to run against localhost Qdrant/WordPress/PocketBase in development. Nothing blocks it in production, so keep `0` there |

### Worker process

| Variable | Default | Effect |
|---|---|---|
| `WORKERS` | `4` | **Container entrypoint only**: how many worker processes the image starts. Ignored when you run `make worker` |
| `WORKER_RESTART_DELAY` | `5` | Container entrypoint only: seconds before a crashed worker slot is respawned |
| `WORKER_ID` | auto (`uuid4` hex[:12]) | Claim identity in leases/logs. The container entrypoint overrides it to `worker-1…N` on purpose |
| `POLL_INTERVAL_SECONDS` | `5` | Job-poll cadence (floor 1 s); idle backoff grows the effective interval to at most 5× |
| `LEASE_SECONDS` | `300` | Job lease duration; expired lease ⇒ another worker may reclaim |
| `HEARTBEAT_INTERVAL_SECONDS` | `15` | Lease extension cadence while running |
| `MAX_CONCURRENT_JOBS` | `4` | Global semaphore across job executions per worker |
| `SCHEDULE_POLL_INTERVAL_SECONDS` | `60` | Schedule loop cadence (floor 10 s); the stale-job reaper runs on the same loop |
| `SCHEDULE_WINDOW_MINUTES` | `15` | Defined but has **no reader** — do not rely on it |

The web process does not claim jobs; the worker does. In the container image the
entrypoint starts `WORKERS` worker processes plus uvicorn (`docker-entrypoint.sh`).

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
> **fallback**; they are resolved **per field**, so a partially filled project
> integration inherits the env value for any field it leaves blank.

| Variable | Default | Effect |
|---|---|---|
| `GOOGLE_ADS_CLIENT_ID` | — | Fallback Google Cloud OAuth client id |
| `GOOGLE_ADS_CLIENT_SECRET` | — | Fallback OAuth client secret (never exposed client-side) |
| `GOOGLE_ADS_REDIRECT_URI` | — | Fallback callback URL, e.g. `https://…/projects/google-ads/callback` (both `/auth/google-ads/callback` and `/projects/google-ads/callback` are served) |
| `GOOGLE_ADS_API_VERSION` | `v25` | Google Ads API version; versions sunset annually, keep configurable |
| `GOOGLE_ADS_LOGIN_CUSTOMER_ID` | — | Optional manager (MCC) id used as `login-customer-id` |
| `GOOGLE_ADS_DEVELOPER_TOKEN` | — | Legacy: developer tokens were sunset 2026-09-09; sent only if set, ignored by Google |
| `RESEARCH_MAX_KEYWORDS_PER_RUN` | `0` (= uncapped) | Google Ads keyword budget per run (`.env.example` suggests `5000`) |
| `RESEARCH_MAX_COMPETITOR_PAGES` | `0` (= uncapped) | Crawl budget per run (`.env.example` suggests `200`) |
| `RESEARCH_MAX_SERP_QUERIES` | `0` | Live SERP budget per run (`0` = no live SERP queries); a hard 200-keyword cap applies regardless |
| `RESEARCH_CRAWL_CONCURRENCY` | `4` | Competitor crawl parallelism |
| `RESEARCH_GOOGLE_ADS_CONCURRENCY` | `2` | Keyword Planning parallelism (tight rate limits) |

> **Note:** the code defaults for the keyword/crawl caps are `0` (no cap). The
> commented values in `.env.example` (`5000`/`200`) are *recommended* production
> caps, not the defaults.

Autocomplete harvest, CSV/XLSX import, competitor-only research and clustering
all work without Google Ads. See [SEO_RESEARCH.md](SEO_RESEARCH.md).

### Billing & payments (optional; off by default)

Self-hosted deployments with their own provider keys are unmetered. See
[BILLING.md](BILLING.md) for the flow and the reconciliation runbook.

| Variable | Default | Effect |
|---|---|---|
| `BILLING_ENABLED` | `false` | Turns on the `/billing` page **and** managed-AI metering. With `true` and no LLM integration on a project, generation is refused when credits run out |
| `BILLING_PROVIDER` | `payram` | Payments adapter name |
| `BILLING_CURRENCY` | `USD` | Fallback label when a product row has no `currency`; PayRam charges USD |
| `BILLING_ARTICLE_COST` | `1` | Managed-AI credits per article generation |
| `BILLING_OVERPAYMENT_POLICY` | `record` | Recorded but **not read** by current code (overpayments are always recorded, never granted) |
| `BILLING_CHECKOUT_COOLDOWN_SECONDS` | `5` | Minimum seconds between two checkout creations for one user |
| `PAYRAM_BASE_URL` | *(empty)* | PayRam Site URL (dashboard → Settings → Site URL) |
| `PAYRAM_API_KEY` | *(empty)* | Project API key (server-side only) |
| `PAYRAM_WEBHOOK_SECRET` | *(empty)* | Distinct HMAC signing secret; defaults to the API key |
| `PAYRAM_TIMEOUT_SECONDS` | `20` | Provider call timeout |

### Analytics (optional; off unless a website ID is set)

| Variable | Default | Effect |
|---|---|---|
| `UMAMI_WEBSITE_ID` | *(empty)* | Umami website ID. Empty → no tracker is rendered and no event is emitted |
| `UMAMI_SCRIPT_URL` | `https://cloud.umami.is/script.js` | Tracker URL; self-hosted points at `https://stats.example.com/script.js` |
| `UMAMI_DOMAINS` | *(empty)* | Comma-separated browser hostnames allowed to report; keeps localhost/staging out of production data |

Events, funnels and the reporting checklist are in
[analytics/umami-measurement-plan.md](analytics/umami-measurement-plan.md).

### Feature flags & misc

| Variable | Default | Effect |
|---|---|---|
| `OLLAMA_ENABLED` | `0` | `1` exposes Ollama (`/v1`) as a selectable LLM provider |
| `PROVIDER_EVENTS_ENABLED` | `0` | `1` records every provider call as a `provider_call` job event |

Defined but currently **unused**: `PAGE_SIZE` (`settings.page_size`, default 25 —
no reader; `.env.example` correctly says not to set it). Code constants worth
knowing: stats cache 15 s, metrics flush interval 30 s, provider adapter retries
(3 attempts, base delay 2 s), index checkpoint every 10 documents, WP fetch page
size 100, Google Ads Keyword Planning page size 10 000.

## 2. Per-project settings (project_settings collection)

Editable in the project's **Website setup** tab (and **Advanced** for the
specialist panels); consumed by jobs through `ProjectConfig`. A project without a
settings row gets `DEFAULT_SETTINGS` from `app/repositories/projects.py`; blank
fields fall back to the runtime defaults below.

### AI models (per role)

Roles: `outline`, `section`, optional `meta` and `review`. Resolution order per role:

```
project override (outlineProvider/Model/Temperature/MaxTokens/Timeout)
  → global default (app_settings.value.llm.<role>)
    → legacy fields (defaultLlmProvider/defaultLlmModel)
      → hardcoded fallbacks: provider openai_compat · temp 0.7 · 4096 tok · 120 s
```

There is **no hardcoded model fallback**: when neither the role, the global
default, nor the connection carries a model, the provider raises a permanent
error. The connection's own `model` column (Connections tab) is used when the
role model is empty.

Per-role retry JSON `{max_attempts, backoff_base}` is used for provider call
retries. The project `retryPolicy` below controls job-level attempts for the
write pipeline; most other enqueue sites hardcode 3 attempts.

The **AI models** tab offers model discovery from the provider API (OpenAI-compat
`GET /models`, Gemini `GET /models`; 10-minute TTL) via **Fetch**, and the
**Test** action lives on each integration row. Editing the global
defaults is admin-only.

### Embedding & indexing

| Setting | Runtime default | Notes |
|---|---|---|
| embeddingProvider / embeddingModel / embeddingDimensions | `openai_compat` / *(empty → `text-embedding-3-small`)* / `1536` | The provider is fixed to the OpenAI-compatible adapter; a global default is **not** consulted. Changing the **model** moves to a new Qdrant namespace; changing **dimensions** requires a re-index into the same namespace |
| chunkSize | `500` words | floor 50 at use site |
| chunkOverlap | `100` words | clamped below chunkSize |
| separatorStrategy | `auto` | `paragraph` / `sentence` / `word` also available |
| maxChunkCount | `0` = unlimited | caps chunks per document |

### Retrieval & context budget

| Setting | Runtime default | Notes |
|---|---|---|
| retrievalTopK | `20` | candidate pool from Qdrant (always filtered by project) |
| similarityThreshold | `0.35` | minimum vector score; `0` disables the filter |
| rerankingEnabled | off | requires an active reranker integration |
| rerankerProvider / rerankerModel / rerankerTopN | `cohere_compat` / `rerank-v4.0` / `8` | |
| contextMaxLinks / contextMaxPassages / contextMaxChars | `5` / `5` / `4000` | prompt-context budget |

### Generation & validation

| Setting | Runtime default | Notes |
|---|---|---|
| minArticleWords | `300` | whole-article gate during assembly |
| generationConcurrency | `2` | Read into `ProjectConfig.generation.section_concurrency`; section generation currently runs under the job/LLM concurrency caps, so raising it alone does not add parallelism |

### Retry policy

JSON `retryPolicy`: `{max_attempts: 3, backoff_base: 30, backoff_max: 3600}`.
Backoff = `min(base × 2^(attempts−1), cap)` with ±50 % jitter; provider
`Retry-After` always wins as a lower bound. The write pipeline reads
`max_attempts`; other job types use their enqueue-time attempt budget.

### Localization & brand (multilingual engine prompt context)

| Setting | Runtime default | Notes |
|---|---|---|
| targetLocale / targetCountry / targetAudience | `en-US` / `US` / empty | market + locale + reader injected into every engine prompt; a blank locale/market falls back to the default, and the writing language derives from `targetLocale` |
| brandName | empty (= project name) | |
| preferredTerminology / forbiddenTerminology | empty | comma-separated wording rules |
| urlPolicy | empty | slug convention for non-Latin scripts |
| productContext | empty | brand offering in 1–2 sentences |
| landingPageUrl | empty | page fetched by the **Analyse page** action |
| productProfile | `{}` | structured profile produced by `analyze_landing_page`, injected into prompts |

### Publishing

| Setting | Values | Notes |
|---|---|---|
| publishingMode | `publish` (schema default) \| `draft` | Status used when creating/updating WP posts. The save form falls back to `draft` only when nothing is stored |
| defaultCategory | empty | pinned WordPress category id; blank = the article picks from the site's live taxonomy |
| autosave | `{enabled: false, interval_minutes: 5}` | Editor autosave descriptor |
| autoPublish | `{enabled: false, min_score: 90, max_attempts: 3}` | Schedule-generated articles publish straight to WordPress above the score, else rewrite up to `max_attempts` and fall back to review |

### Image generation (project **Article images** tab)

Model ids are data (persisted here), never hardcoded in the pipeline.

| Setting | Runtime default | Notes |
|---|---|---|
| imageCoverProvider / imageCoverModel | `gemini` / `gemini-3-pro-image` | cover slot |
| imageInteriorProvider / imageInteriorModel | `bfl` / `flux-2-klein-9b` | interior slots |
| imageFallbackProvider / imageFallbackModel | empty | empty = no fallback; the fallback may be a different model on the same connection |
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

### Indexing routine

`indexing` JSON: `{schedule_enabled: false, schedule_interval_minutes: 1440,
wp_status: "publish"}`. Per-project index schedules live in the `schedules`
collection and are edited on the **Automation** page.

## 3. Integrations (credentials)

Managed in the project **Connections** tab; one row per
`(project, category, provider, displayName)`:

| Category | Providers selectable | Secret stored |
|---|---|---|
| llm | `openai_compat`, `gemini`, `custom`, `ollama` (flag) | API key |
| embedding | `openai_compat` | API key (OpenAI-compatible; a legacy `cohere` value maps to this adapter) |
| reranker | `cohere_compat` | API key |
| vector_store | `qdrant` | optional API key |
| publisher | `wordpress` | application password |
| image | `gemini`, `bfl`, `openai_compat` | API key |
| serp (research) | `serper` | API key (optional; the registry also has a credential-free `none` adapter) |
| google_ads (research) | `google_ads` | OAuth client secret (+ optional legacy developer token) |

Non-secret config (base URL, username, model) lives in the plain `configuration`
JSON (plus the dedicated `model` column); secrets are Fernet-encrypted into
`secretsEnc` and never rendered beyond a masked preview. The **first enabled**
integration per category feeds the provider registry (order: created). "Test
connection" pings the provider and stores health status.

`keyword_source` (`autocomplete`) is a provider-table category without a
credentials row — it needs no integration.

## 4. Global defaults (app_settings singleton)

Seeded by bootstrap (`key="default"`). Only LLM role resolution and the embedding
placeholder values are consulted today:

```json
{
  "default_llm_provider": "openai_compat",
  "default_embedding_provider": "openai_compat",
  "default_embedding_model": "",
  "default_embedding_dimensions": 0,
  "heartbeat_interval": 15,
  "llm": {
    "outline": {"provider": "openai_compat", "model": "",
                 "temperature": 0.7, "max_tokens": 4096, "timeout": 120},
    "section": { "… same …" },
    "meta":    {"provider": "", "model": ""},
    "review":  {"provider": "", "model": ""}
  }
}
```

Editable via the AI models tab's **Global defaults (admin only)** form. The
seeded models are intentionally empty: each project picks its own model on its
LLM connection.

## 5. Prompts

Twenty-seven prompt types per project (falling back to global rows), all seeded via
`DEFAULT_PROMPTS` in `app/scripts/bootstrap_pb.py` (`PROMPT_TYPES` in
`app/repositories/prompts.py` is the single source of the list):
`brand_voice`, `seo_content_contract`, `research_system`, `research_user`,
`outline_system`, `outline_user`, `section_system`, `section_user`,
`internal_linking`, `metadata_system`, `metadata_user`, `article_qa_system`,
`article_qa_user`, `article_repair_system`, `article_repair_user`,
`image_plan_system`, `image_plan_user`, `output_validation`,
`content_refresh_system`, `cluster_system`, `cluster_user`, `opportunity_system`,
`opportunity_user`, `category_system`, `category_user` — plus legacy aliases
`seo_rules` (resolves the contract) and `validation` (generic JSON repair) kept so
existing projects and tests keep working.

The **Writing rules** tab exposes 21 of these; `cluster_*`, `opportunity_*` and
`category_*` are global/internal prompt types and are not editable per project in
that tab.

The image-plan prompts accept `{{ article.title }}`,
`{{ article.content }}`, `{{ topic.keyword }}`, `{{ language }}`,
`{{ locale }}`, `{{ prompt_language }}`, `{{ sections }}`,
`{{ style_profile }}`, `{{ max_interior_images }}`; the outline repair pass
uses `{{ output_schema }}` + `{{ raw_output }}`.

Variables usable inside prompts (unknown names are rejected before any LLM call)
are listed by `variable_labels()` (`app/domain/prompt_render.py`, 71 entries),
including `{{ project.name }}` · `{{ topic.title }}` · `{{ topic.keyword }}` ·
`{{ article.title }}` · `{{ article.content }}` · `{{ section.heading }}` ·
`{{ retrieved_context }}` · `{{ internal_links }}` · `{{ seo_rules }}` ·
`{{ brand_voice }}` · `{{ language }}` · `{{ locale }}` · `{{ audience }}` ·
`{{ product_profile }}` · `{{ raw_output }}` + `{{ output_schema }}` (output
validation only) — plus research/intent, section-detail, neighbour, QA and image
planning variables.

`{{ project.language }}` is derived from the project's `targetLocale` (`en-US` → `en`),
not a separate project column.

Every save appends a new version; activating an old version is the rollback path.
Bootstrap seeds English global defaults for all types.
