<p align="center">
  <img src="app/static/brand/ezdistro/ezdistro-app-icon.svg" width="88" height="88" alt="EzDistro logo">
</p>

<h1 align="center">EzDistro</h1>

<p align="center">
  <strong>Self-hosted SEO content automation</strong><br>
  Index your WordPress content, write retrieval-grounded articles with your own LLM,<br>
  review them, publish them — no Redis, no Celery, no n8n.
</p>

<p align="center">
  <a href="https://github.com/Rastin-Amani/EzDistro/actions/workflows/ci.yml"><img src="https://img.shields.io/github/actions/workflow/status/Rastin-Amani/EzDistro/ci.yml?branch=main&label=CI&logo=github&style=flat-square" alt="CI"></a>
  <a href="LICENSE"><img src="https://img.shields.io/github/license/Rastin-Amani/EzDistro?style=flat-square&color=blue" alt="License: Apache 2.0"></a>
  <a href="https://github.com/Rastin-Amani/EzDistro/releases"><img src="https://img.shields.io/github/v/release/Rastin-Amani/EzDistro?style=flat-square&color=0000ff" alt="Release"></a>
  <a href="https://github.com/Rastin-Amani/EzDistro/pkgs/container/ezdistro"><img src="https://img.shields.io/badge/GHCR-ezdistro-2496ED?style=flat-square&logo=docker&logoColor=white" alt="Container"></a>
  <a href="https://github.com/Rastin-Amani/EzDistro/stargazers"><img src="https://img.shields.io/github/stars/Rastin-Amani/EzDistro?style=flat-square&logo=github&logoColor=black" alt="Stars"></a>
  <a href="https://github.com/Rastin-Amani/EzDistro/commits/main"><img src="https://img.shields.io/github/last-commit/Rastin-Amani/EzDistro?style=flat-square" alt="Last commit"></a>
</p>

<p align="center">
  <a href="https://www.python.org/"><img src="https://img.shields.io/badge/Python-3.12-3776AB?style=flat-square&logo=python&logoColor=white" alt="Python 3.12"></a>
  <a href="https://fastapi.tiangolo.com/"><img src="https://img.shields.io/badge/FastAPI-009688?style=flat-square&logo=fastapi&logoColor=white" alt="FastAPI"></a>
  <a href="https://htmx.org/"><img src="https://img.shields.io/badge/HTMX-3366CC?style=flat-square&logo=htmx&logoColor=white" alt="HTMX"></a>
  <a href="https://pocketbase.io/"><img src="https://img.shields.io/badge/PocketBase-b8dbe4?style=flat-square&logo=pocketbase&logoColor=black" alt="PocketBase"></a>
  <a href="https://qdrant.tech/"><img src="https://img.shields.io/badge/Qdrant-DC244C?style=flat-square&logo=qdrant&logoColor=white" alt="Qdrant"></a>
  <a href="https://wordpress.org/"><img src="https://img.shields.io/badge/WordPress-21759B?style=flat-square&logo=wordpress&logoColor=white" alt="WordPress"></a>
  <a href="docs/OPERATIONS.md#docker"><img src="https://img.shields.io/badge/Docker-2496ED?style=flat-square&logo=docker&logoColor=white" alt="Docker"></a>
  <a href="#stack"><img src="https://img.shields.io/badge/PWA-installable-5A0FC8?style=flat-square&logo=pwa&logoColor=white" alt="PWA"></a>
</p>

<p align="center">
  <a href="docs/OPERATIONS.md"><img src="https://img.shields.io/badge/self--hosted-0000ff?style=flat-square&logo=linux&logoColor=white" alt="Self-hosted"></a>
  <a href="docs/ARCHITECTURE.md"><img src="https://img.shields.io/badge/no-Redis-DC382D?style=flat-square&logo=redis&logoColor=white" alt="No Redis"></a>
  <a href="docs/ARCHITECTURE.md"><img src="https://img.shields.io/badge/no-Celery-37814A?style=flat-square&logo=celery&logoColor=white" alt="No Celery"></a>
  <a href="docs/ARCHITECTURE.md"><img src="https://img.shields.io/badge/no-n8n-EA4B71?style=flat-square&logo=n8n&logoColor=white" alt="No n8n"></a>
  <a href="https://www.conventionalcommits.org/"><img src="https://img.shields.io/badge/Conventional%20Commits-1.0.0-fe5196?style=flat-square&logo=conventionalcommits&logoColor=white" alt="Conventional Commits"></a>
  <a href="CONTRIBUTING.md"><img src="https://img.shields.io/badge/PRs-welcome-brightgreen?style=flat-square&logo=git&logoColor=white" alt="PRs welcome"></a>
</p>

Production-grade, reusable SEO automation platform: fetches WordPress posts, chunks and
embeds them into Qdrant (Indexer), and generates fully-formed articles from topics that
are written section-by-section by an LLM and published back to WordPress (Writer).

Built as a **modular monolith**: FastAPI + HTMX + DaisyUI (PWA) front end,
PocketBase as the source of truth, Qdrant for vectors, and pluggable cloud providers for
LLM / embeddings / reranking / publishing. No Redis, no Celery, no n8n.

Architecture, data model, job-state design and provider interfaces: see [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Screenshots

**Writing workspace** — outline navigation, section editor and the checks that gate publishing, in one screen.

![Article writing workspace](.github/assets/screenshots/workspace.png)

**Review gate** — structural checks and editorial QA decide whether Publish is enabled.

![Article review and approval](.github/assets/screenshots/review.png)

**Overview** — pipeline state across every site you run, with what needs a human first.

![Dashboard overview](.github/assets/screenshots/overview.png)

**Activity** — durable jobs with live progress, retries and the reason a task stopped.

![Job activity](.github/assets/screenshots/activity.png)

---

## Features

- **Production content workspace**: 3-pane article editor (outline navigation /
  content+section editor / metadata+SEO+links+status), live section-generation
  progress with elapsed time, versioned outline editing (move/add/delete/brief),
  bulk topic management with search/filter/sort/pagination, pipeline dashboard
  with indexing + provider health, custom `ezdistro` DaisyUI theme.

- **Multi-project**: every project has its own WordPress config, Qdrant namespace,
  embedding provider/model/dimensions, LLM provider/model, prompts, SEO rules, writing
  style, language, chunking, retrieval & reranking, publishing mode, concurrency,
  schedules and internal-linking rules — all configurable from the UI.
- **Persistent async jobs on PocketBase** (no Redis):
  - atomic lease-based claiming via a unique-constrained `job_leases` collection
  - lease expiry → abandoned-job recovery
  - retries with exponential backoff + jitter, honoring provider Retry-After
  - cooperative cancellation (UI button → handler checkpoints, graceful)
  - progress % + stage + current/total items; canonical lifecycle events
    (job.created → job.claimed → job.started → job.stage_* → job.completed/failed/cancelled)
  - bounded provider concurrency (LLM / embeddings / publishing rate limits)
  - idempotency keys; no duplicated irreversible operations (publish guard)
  - 16 registered job types: the 8 content/indexing types incl. granular ones
    (generate_outline, generate_section, assemble_article, index_document,
    retry_failed_job) plus 6 image-pipeline types (plan_article_images,
    generate_cover_image, generate_interior_image, generate_article_image,
    optimize_article_image, publish_article_image) plus 2 research types
    (research_run, wordpress_sync)
- **Indexer job** (`index_run`): fetch WP posts (id-ordered, resumable) → strip HTML →
  chunk (size/overlap per project) → batch embed → upsert Qdrant (stable point ids) →
  per-post content-hash metadata; unchanged posts are skipped; crashed runs resume
  from `lastWpId`.
- **Writer job** (`write_article`): pick pending topic → retrieve related docs →
  internal-link context → LLM outline **validated by pydantic schema** → section records →
  per-section generation with bounded concurrency → sanitized HTML assembly →
  chains a `publish_article` job.
- **Publish job** (`publish_article`): WordPress REST (application passwords), draft or
  live, idempotent (consults `publishing` records before calling WP). Uploads ready
  article images to the WP media library idempotently, resolves placeholders, and
  attaches the cover as the post featured image (missing cover blocks publish unless
  overridden with `publishWithoutCover`).
- **Image pipeline** (`plan_article_images` → `generate_cover/interior_image` →
  `optimize_article_image`): LLM-planned cover + purposeful interior images (density
  capped by word count, max 4), generated via the project's image providers with
  fallback, quality-gated (dimensions/format decoded before storage), optimized to
  WebP/AVIF/JPEG + small variant, versioned per slot (regenerate creates version+1,
  never overwrites). Auto-chained from `assemble_article`; manageable from the
  article workspace **images pane** and the project **Images** settings tab (includes
  a one-click generation test).
- **SEO research engine** (`research_run`): Google Ads Keyword Planner data (OAuth,
  per-project client) or an uploaded Keyword Planner CSV/XLSX file → WordPress mirror
  (`wordpress_sync`) → competitor crawl → clustering (deterministic / embedding / LLM)
  → content gaps → transparent `GENERATE`/`UPDATE`/`EXPAND`/`SUPPORT`/`REJECT`
  opportunities → one-click handoff to the existing writer. Facts-first: it never
  invents volume/CPC/competition. Optional SERP provider; degrades gracefully without
  Google Ads, SERP or LLM. See [docs/SEO_RESEARCH.md](docs/SEO_RESEARCH.md).
- **Scheduler**: per-project interval schedules → idempotent job creation (per-window keys).
- **Security**: Fernet-encrypted credentials at rest (masked in UI, never plaintext),
  SSRF guard on all provider URLs, HTML sanitization (bleach allowlist), HX-Request
  checks on mutations, per-project access control.
- **Observability**: structured logs (structlog) with correlation ids (web) / job ids
  (worker); per-job and per-project event feeds in the UI.

## Stack

| Layer | Technology |
|---|---|
| Web | FastAPI, Jinja2, HTMX 2, Alpine.js (minimal), DaisyUI 5, Tailwind 4, PWA |
| Source of truth | PocketBase (≥ 0.23) — schema: [docs/SCHEMA.md](docs/SCHEMA.md) |
| Vectors | Qdrant (HTTP) |
| LLM / embeddings / rerank / images | Provider registry: OpenAI-compatible chat (generate/json/stream), native Gemini, **Cohere Embed v4.0** (search_document/search_query), Cohere-compatible rerank, image generation (`gemini` / `bfl` FLUX / `openai_compat`) — selectable per project from the UI |
| Publishing | WordPress REST API |
| Worker | Standalone asyncio process (`python -m app.workers.worker`) |

## Data model

The PocketBase schema is designed around the product domain (not the old n8n
workflows): **38 collections** covering projects, flat project settings
(incl. image-generation and multilingual config), configurable integrations
(encrypted secrets; categories incl. `image`, `serp`, `google_ads`), versioned
prompts (incl. `image_plan_*`, `research_*`, `cluster_*`, `opportunity_*`),
topics with a full editorial pipeline, articles + sections (the outline IS the
ordered section rows; WordPress-mirror fields for `wordpress_sync`), article
revisions (append-only history), article images (versioned generations +
optimized files), indexed documents, index runs, the job engine (jobs / atomic
leases / append-only job events), publishing runs (append-only publish audit),
provider metrics, schedules, worker heartbeats and project memberships (plus the
built-in `users` collection extended with `role`/`displayName`), **plus 13 SEO
research collections** (`google_ads_connections`, `google_ads_customers`,
`research_runs`, `research_seeds`, `keywords`, `keyword_metrics`,
`keyword_volumes`, `clusters`, `competitor_pages`, `content_gaps`,
`article_ideas`, `serp_queries`, `serp_results`), **plus 5 billing collections**
(`billing_products`, `billing_invoices`, `billing_payment_events`,
`billing_usage`, `credit_transactions`).
Relationships, cascades, uniqueness and the indexing strategy:
[docs/SCHEMA.md](docs/SCHEMA.md).

## Quick start

```bash
# 1. Python + JS deps
pip install -r requirements.txt
npm install && npm run css:build

# 2. Configure
cp .env.example .env            # PB_URL, PB_ADMIN_EMAIL/PASSWORD, SECRETS_KEY,
                                # PUBLIC_APP_URL + Google/GitHub OAuth credentials

# 3. Bootstrap PocketBase collections (idempotent)
make bootstrap

# 4. Run the two processes (independently)
make web                        # uvicorn app.main:app --port 8000
make worker                     # python -m app.workers.worker
```

### Container image

Prebuilt images for `linux/amd64` and `linux/arm64` are published to GitHub
Container Registry from `main` and from every release tag — no local build needed:

```bash
docker pull ghcr.io/rastin-amani/ezdistro:latest
docker run --env-file .env -p 8000:8000 ghcr.io/rastin-amani/ezdistro:latest
```

The image runs the web process plus a pool of `WORKERS` worker processes (default 4)
in one container; see [docs/OPERATIONS.md](docs/OPERATIONS.md#docker).

Sign in or create an account with Google or GitHub (there is no password login).
Register the app’s `/auth/oauth2/callback` URL at both providers before the first
sign-in (the app builds it from the public origin it is served on); see
[docs/CONFIGURATION.md](docs/CONFIGURATION.md#sign-in-oauth-only-google--github).

### Workflow

1. Create a **project**.
2. **Integrations** tab: add active credentials — WordPress (site URL + username +
   application password), LLM (base URL + API key), Embedding (base URL + key),
   Qdrant (URL, optional key), Reranker (optional).
3. **Settings** tab: embedding model/dimensions, chunk size/overlap, retrieval
   top-k/threshold, rerank, LLM model/temperature, section concurrency, retry policy,
   publishing mode, SEO rules, schedules.
4. **Prompts** tab: system / outline / section / SEO prompts (per-project; global
   defaults seeded by bootstrap).
5. **Topics** tab: add topics (manually or bulk-import CSV/TSV), click **Write article**
   → writer job → article appears in **Articles** (edit outline/sections there) →
   publish job → live/draft on WordPress.
6. **Research** tab (optional): connect Google Ads on **Connections** (or upload a
   Keyword Planner CSV/XLSX), run research, then accept opportunities to feed Topics.
7. **Jobs** and **Events** tabs: monitor, cancel, retry (the **Failed jobs**
   dead-letter view opens from **Jobs**).
8. **Full re-index** button (or schedule): index WordPress posts into Qdrant.

The documentation set in `docs/` is also served in-app at `/help/` (auth-gated) —
the Research tab's **Setup guide** link points there.

## Development

```bash
make test          # pytest
make lint          # ruff check + format check
make typecheck     # mypy app/
make check         # all three
```

CI (`.github/workflows/ci.yml`) runs the same four steps on every push to `main`
and on every pull request, on Python 3.12.

## Interface language

The UI is **English-only**. Dates and numbers render through the `loc_*` filters in
`app/templates.py`: Gregorian dates via Babel and thousands-grouped integers via
`loc_int`. Layouts use logical properties, so markup stays direction-agnostic.

Machine-readable enum values stay unformatted; display labels are plain English strings.

## Process model

```
┌────────────────────┐     ┌─────────────────────┐
│  Web (uvicorn)     │     │  Worker (asyncio)   │
│  HTMX UI + API     │     │  poll → claim → run │
│  creates jobs      │     │  heartbeat/retry/   │
│  (idempotent)      │     │  cancel/progress    │
└─────────┬──────────┘     └──────────┬──────────┘
          │            PocketBase     │
          └──────────── (source of    ┘
                        truth)    Qdrant / LLM / WP (providers)
```

Web and worker are fully independent; run any number of workers (atomic claiming +
leases make multi-worker safe).

## Documentation & contributing

- Full docs index: [docs/README.md](docs/README.md) — also served in-app at
  `/help/` (auth-gated).
- Architecture, schema, configuration, operations, failure modes and the SEO
  research engine: [`docs/`](docs/README.md).
- Setup, checks, architecture rules and the schema workflow: [CONTRIBUTING.md](CONTRIBUTING.md).
- Release history: [CHANGELOG.md](CHANGELOG.md).
- Security model and how to report a vulnerability: [SECURITY.md](SECURITY.md).

## License

Apache License 2.0 — see [LICENSE](LICENSE). Copyright 2026 Rastin Amani.
