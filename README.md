# EzDistro Platform — SEO Automation (Indexer + Writer)

Production-grade, reusable SEO automation platform: fetches WordPress posts, chunks and
embeds them into Qdrant (Indexer), and generates fully-formed articles from topics that
are written section-by-section by an LLM and published back to WordPress (Writer).

Built as a **modular monolith**: FastAPI + HTMX + DaisyUI (PWA) front end,
PocketBase as the source of truth, Qdrant for vectors, and pluggable cloud providers for
LLM / embeddings / reranking / publishing. No Redis, no Celery, no n8n.

Architecture, data model, job-state design and provider interfaces: see [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

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
workflows): **33 collections** covering projects, flat project settings
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
`article_ideas`, `serp_queries`, `serp_results`).
Relationships, cascades, uniqueness and the indexing strategy:
[docs/SCHEMA.md](docs/SCHEMA.md).

## Quick start

```bash
# 1. Python + JS deps
pip install -r requirements.txt
npm install && npm run css:build

# 2. Configure
cp .env.example .env            # PB_URL, PB_ADMIN_EMAIL/PASSWORD, SECRETS_KEY (prod)

# 3. Bootstrap PocketBase collections (idempotent)
make bootstrap

# 4. Run the two processes (independently)
make web                        # uvicorn app.main:app --port 8000
make worker                     # python -m app.workers.worker
```

Login with the seeded admin user (`SEED_ADMIN_EMAIL` / `SEED_ADMIN_PASSWORD`).

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
7. **Jobs / failed jobs / events** tabs: monitor, cancel, retry.
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
