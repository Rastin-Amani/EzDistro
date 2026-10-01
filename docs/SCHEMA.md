# EzDistro Platform — PocketBase Schema Reference

This is a **reference** of the live schema. The source of truth is code:
`app/scripts/bootstrap_pb.py` (idempotent bootstrap; `pb_collections_import.json` in
the repo root mirrors it for manual import). If this document and the code disagree,
the code wins — please fix the doc.

```text
Documentation status:  Verified against app/scripts/bootstrap_pb.py (post-v1.3.0)
Last verified:         2026-09-12
Requires:              PocketBase ≥ 0.23
```

## 1. Design principles

1. Domain-driven collections (topics, articles, publishing runs) — not workflow blobs.
2. Multi-project first: domain rows carry a `project` relation; access enforced at the
   app layer via `project_members`.
3. References, not copies: articles link topics; audit rows store small snapshots only.
4. No giant payloads: article HTML has explicit 100 000-char caps; vectors never enter
   PocketBase.
5. Secrets never unencrypted at rest: `integrations.secretsEnc` holds Fernet
   ciphertext.
6. Append-only audit: `job_events`, `article_revisions`, `publishing_runs` are
   insert-mostly.
7. All collection API rules are superuser-only (`null` — PocketBase's "locked"
   state; `""` would mean **public**, guests included, not "locked");
   project-level authorization lives in the app layer (`app/api/deps.py`)
   and all data access goes through the superuser client (`get_admin_pb()` /
   `get_data_pb()`). Never relax to `@request.auth.id != ''` — any
   authenticated user could then bypass tenant isolation via direct REST.

## 2. Collections overview

33 base collections (+ built-in `users`, extended with `role` / `displayName`):

> **Note (SEO research engine, 2026-10):** `app/scripts/bootstrap_pb.py` now also
> defines 13 research collections — `google_ads_connections`, `google_ads_customers`,
> `research_runs`, `research_seeds`, `keywords`, `keyword_metrics`,
> `keyword_volumes`, `clusters`, `competitor_pages`, `content_gaps`,
> `article_ideas`, `serp_queries`, `serp_results` — plus a `serp` value on
> `integrations.category` and WordPress-mirror fields on `articles`
> (`source`, `syncStatus`, `remoteStatus`, `remoteModified`, `remoteContentHash`,
> `remoteSlug`, `remoteExcerpt`, `remoteMeta`, `contentHash`). The table below
> predates them; the bootstrap script is the source of truth, and
> `pb_collections_import.json` is generated from it
> (`python -m app.scripts.bootstrap_pb --export`). See
> [SEO_RESEARCH.md](SEO_RESEARCH.md).

| Collection | Purpose | Key uniqueness |
|---|---|---|
| `projects` | Workspace root | unique `slug` |
| `project_settings` | Flat per-project configuration (no JSON blobs for hot fields) | unique `project` (1:1) |
| `integrations` | Configurable provider credentials per category | unique `(project, category, provider, displayName)` |
| `prompts` | Versioned prompt library (project rows + global defaults) | unique `(project, type, name, version)` |
| `topics` | Editorial backlog feeding the writer | — |
| `articles` | Generated article aggregate | unique `topicId` (1:1 topic) |
| `article_sections` | Ordered section rows — the outline IS these rows | unique `(article, position)` |
| `article_revisions` | Append-only full snapshots | unique `(article, revision)` |
| `article_images` | Versioned generated images + optimized files (v1.3.0) | unique `(article, role, sectionKey, version)` |
| `documents` | Indexed source documents (metadata) | unique `(project, sourceType, sourceId)` |
| `index_runs` | One row per indexing run | — |
| `jobs` | Persistent async jobs | unique `idempotencyKey` |
| `job_leases` | Atomic claim primitive | unique `job` (CAS) |
| `job_events` | Append-only job timeline | — |
| `publishing_runs` | Append-only publish attempts | — |
| `provider_metrics` | Daily per-provider aggregates | unique `(project, provider, model, operation, day)` |
| `schedules` | Interval schedules (`index` \| `write`) | — |
| `worker_heartbeats` | Worker liveness beacons (v1.1.0) | unique `workerId` |
| `app_settings` | Global defaults singleton | unique `key` (`default`) |
| `project_members` | Authorization | unique `(project, user)` |

### Entity relationships

```
users ──< project_members >── projects
                                │ 1:1  project_settings
                                │ 1:N  integrations · prompts (nullable=global) · topics
                                │      documents · index_runs · jobs · job_events
                                │      publishing_runs · provider_metrics · schedules
topics ──1:1── articles ──1:N── article_sections
                    │─────── 1:N article_revisions · publishing_runs · article_images
jobs ──1:1(unique)── job_leases        jobs ──1:N── job_events
jobs.parent ──▶ jobs (chained jobs, e.g. publish chained from write)
```

Cascade deletes (relation `cascadeDelete=true`): deleting a project removes its
settings, integrations, project prompts, topics → articles → sections/revisions/
publishing runs, documents, index runs, jobs → leases/events, metrics, schedules and
memberships. **Global prompts (empty `project`) survive.** Deleting a user cascades
their memberships. Audit pointers that intentionally do NOT cascade: `documents.lastRun`,
`index_runs.job`, `publishing_runs.job`, `articles.lastJob`, `jobs.parent`,
`topics.articleId` (cleared in app code).

Field naming: all names are camelCase because PB clients run with
`auto_snake_case=False`; do not "fix" them to snake_case.

---

## 3. Field reference

Legend: `*` required · `U` unique index · `R` relation · `sel` select values listed.

### 3.1 projects

| field | type | notes |
|---|---|---|
| name* | text | |
| slug* | text | U; used in Qdrant namespace keys |
| description | text | |
| status | sel | `active` \| `archived` |
| language | sel | `fa` \| `en` \| `ar` \| `tr` |
| timezone | text | IANA name |
| createdBy | text | audit |

Indexes: UNIQUE slug; status.

### 3.2 project_settings (1:1 flat)

| field | type | group |
|---|---|---|
| project* R | projects (cascade) | U unique |
| defaultLlmProvider / defaultLlmModel | text | legacy single-LLM fallback |
| outlineProvider/Model, outlineTemperature, outlineMaxTokens, outlineTimeout, outlineRetry(json) | text/num/json | OUTLINE role overrides |
| sectionProvider/Model, sectionTemperature, sectionMaxTokens, sectionTimeout, sectionRetry(json) | text/num/json | SECTION role overrides |
| metaProvider/Model, reviewProvider/Model | text | optional META / REVIEW roles |
| embeddingProvider / embeddingModel / embeddingDimensions | text/text/num | vector index identity |
| chunkSize / chunkOverlap | num | word-budget chunking (defaults applied at runtime: 500/100) |
| separatorStrategy | text | `auto` \| `paragraph` \| `sentence` \| `word` |
| maxChunkCount | num | 0 = unlimited |
| retrievalTopK / similarityThreshold | num | candidate pool / min similarity (runtime fallback 0 = off) |
| rerankingEnabled | bool | |
| rerankerProvider / rerankerModel / rerankerTopN | text/text/num | runtime model fallback `rerank-v4.0`, top_n 8 |
| contextMaxLinks / contextMaxPassages / contextMaxChars | num | prompt-context budget (runtime fallbacks 5 / 5 / 4000) |
| generationConcurrency | num | stored & editable in UI; ⚠ not read by current engine code (see ARCHITECTURE §11) |
| minArticleWords | num | assembly validation gate (runtime default 300) |
| retryPolicy | json | `{max_attempts, backoff_base, backoff_max}` (defaults 3 / 30 s / 3600 s) |
| publishingMode | sel | `draft` \| `publish` |
| targetLocale / targetCountry / targetAudience | text | market + locale + reader for every prompt |
| brandName | text | empty = project name |
| preferredTerminology / forbiddenTerminology | text | comma-separated wording rules |
| urlPolicy | text | slug convention for non-Latin scripts |
| productContext | text | brand offering in 1–2 sentences |
| autosave | json | `{enabled, interval_minutes}` |
| indexing | json | `{schedule_enabled, schedule_interval_minutes, wp_status}` |
| imageCoverProvider / imageCoverModel | text | defaults `gemini` / `gemini-3-pro-image` |
| imageInteriorProvider / imageInteriorModel | text | defaults `bfl` / `flux-2-klein-9b` |
| imageFallbackProvider / imageFallbackModel | text | empty = no fallback |
| imageCoverAspectRatio / imageInteriorAspectRatio | text | default `16:9` |
| imageCoverMinWidth | num | default 1200 (SEO/Discover floor) |
| imageMaxInteriorImages | num | default 4 (hard cap; density further limits) |
| imageMaxRetries | num | default 3 |
| imageOptimizationFormat | sel | `webp` \| `avif` \| `jpeg` (default `webp`) |
| imageAiQaEnabled | bool | optional vision QA (extra cost) |
| imagePromptLanguage | text | visual-prompt language, default `en` |
| imageStyle | json | style profile passed to the plan prompt |

Note: there is no seeding of default values for new settings rows; runtime fallbacks
live in `ProjectConfig` (`app/services/settings.py`) and the API form casts. Embedding
fallback differs from the global seed — see ARCHITECTURE §11.4.

### 3.3 integrations

| field | type | notes |
|---|---|---|
| project* R | projects | cascade |
| category* | sel | `llm` \| `embedding` \| `reranker` \| `vector_store` \| `publisher` \| `image` (v1.3.0) |
| provider* | text | e.g. `openai_compat`, `gemini`, `cohere`, `cohere_compat`, `qdrant`, `wordpress`, `bfl` |
| displayName* | text | part of uniqueness |
| configuration | json | non-secret config only (base_url, username, model, …) |
| secretsEnc | text | Fernet ciphertext of the secret JSON — never rendered or logged |
| enabled | bool | the active integration per category feeds the registry |
| healthStatus | sel | `unknown` \| `healthy` \| `degraded` \| `unhealthy` |
| lastTestedAt | date | set by "test connection" |
| createdBy | text | audit |

Indexes: (project, category); UNIQUE (project, category, provider, displayName).

### 3.4 prompts

| field | type | notes |
|---|---|---|
| project R | projects | empty ⇒ **global default**; project row wins resolution |
| type* | sel | `brand_voice` \| `seo_content_contract` \| `research_system` \| `research_user` \| `outline_system` \| `outline_user` \| `section_system` \| `section_user` \| `internal_linking` \| `metadata_system` \| `metadata_user` \| `article_qa_system` \| `article_qa_user` \| `article_repair_system` \| `article_repair_user` \| `image_plan_system` \| `image_plan_user` \| `output_validation` \| `content_refresh_system` \| legacy `seo_rules` \| `validation` |
| name* | text | typically `default` |
| content* | text (≤20000) | may contain `{{ variable }}` tokens (validated registry) |
| version* | num | 1-based; each save inserts a NEW row |
| active | bool | one active per (project, type, name) |
| variables | json | `{used: [...], duplicatedFrom}` |
| updatedBy | text | audit |

Indexes: (project, type, active); UNIQUE (project, type, name, version).

Bootstrap seeds twenty-one global prompts (19 from
`multilingual-seo-content-engine-prompts.md` plus legacy `seo_rules` /
`validation` aliases carrying the new contract / repair text) — see
`DEFAULT_PROMPTS` in `bootstrap_pb.py`. The single source of the type list is
`PROMPT_TYPES` in `app/repositories/prompts.py`.

### 3.5 topics

| field | type | notes |
|---|---|---|
| project* R | projects | cascade |
| title* | text | |
| keyword | text | SEO focus keyword |
| pillar / cluster | text | topical grouping labels |
| type | sel | `article` \| `pillar_page` \| `guide` \| `news` |
| status* | sel | `planned` \| `queued` \| `planning` \| `outline_ready` \| `writing` \| `review` \| `completed` \| `publishing` \| `published` \| `failed` \| `cancelled` |
| priority | num | higher first when picking topics/jobs |
| week | num | editorial-calendar week (bulk/CSV imports) |
| url | text | published URL (bulk/CSV imports) |
| articleId R | articles | no cascade; cleared by app code |

Indexes: (project, status, priority); (project, status); (project, week).

### 3.6 articles

| field | type | notes |
|---|---|---|
| project* R | projects | cascade |
| topicId* R U | topics | cascade from topic; 1:1 |
| title* | text | |
| slug | text | |
| status* | sel | `draft` \| `outline_ready` \| `generating` \| `review` \| `ready_to_publish` \| `approved` \| `sent_back` \| `publishing` \| `published` \| `failed` |
| outlineVersion | num | bumped on every outline (re)build |
| outline | json | immutable validated snapshot `{title, slug, meta_description, search_intent, audience, sections[{heading, content_brief, internal_links[], key, purpose, reader_question, required_points[], entities[], evidence[]}], research{…}}` |
| validation | json | assembler's report `{ok, issues[], stats, qa{ran, ready, summary, issues[], strengths[], repaired}}` |
| finalHtml | text (≤100k) | current displayed/stored content |
| generatedContent | text (≤100k) | last pipeline output (distinct from manual edits) |
| lastGeneratedRevision | num | revision id of last generation |
| reviewNote | text | reviewer feedback on send-back |
| metaDescription | text | |
| seoScore | num | 0–100 deterministic heuristic |
| wordCount | num | |
| generatedAt / publishedAt | date | |
| wordpressPostId | num | set on first successful publish |
| wordpressUrl | text | |
| imagePlan | json | latest `ArticleImagePlan` snapshot (v1.3.0) |
| imagePlanVersion | num | |
| lastJob R | jobs | audit pointer, no cascade |

Indexes: (project, status); UNIQUE topicId.

### 3.7 article_sections

The outline IS the ordered section rows; there is no separate outline storage beyond
the immutable snapshot on `articles.outline`.

| field | type | notes |
|---|---|---|
| article* R | articles | cascade |
| position | num | NOT marked required: PocketBase treats `0` as blank and would reject the first section |
| heading* | text | rendered as H2 |
| contentBrief | text | what the LLM must cover |
| internalLinks | json | plan links for this section |
| content | text (≤100k) | generated/sanitized HTML |
| status* | sel | `pending` \| `generating` \| `done` \| `failed` |
| generationAttempts | num | |
| promptVersion | num | active section_user version used (0 = fallback) |
| generationStartedAt | date | drives workspace elapsed display |
| provider / model | text | provenance |
| tokenUsage | json | `{prompt_tokens, completion_tokens}` |
| generationLatency | num | ms |
| error | json | structured failure |

Index: UNIQUE (article, position).

### 3.8 article_revisions (append-only)

| field | type | notes |
|---|---|---|
| article* R | articles | cascade |
| revision* | num | 1-based per article |
| kind* | sel | `generated` \| `manual` \| `checkpoint` \| `rollback` |
| snapshot | json | full article+sections state |
| note / createdBy | text | audit |

Indexes: UNIQUE (article, revision); (article, created).

### 3.9 article_images (v1.3.0)

One row per generation; `active` marks the selected version. Successful rows are
never overwritten — regenerate creates version+1, rollback flips `active`.

| field | type | notes |
|---|---|---|
| project* R | projects | cascade |
| article* R | articles | cascade |
| role* | sel | `cover` \| `interior` |
| sectionKey | text | `section-N` for interiors, empty for cover |
| version* | num | 1-based per (article, role, sectionKey) |
| active | bool | selected version |
| status* | sel | `planned` \| `generating` \| `optimizing` \| `ready` \| `failed` |
| provider / model | text | provenance |
| prompt | text (≤8000) | visual prompt |
| promptHash / styleHash / fingerprint | text | dedup / style identity |
| negativePrompt | text (≤5000) | |
| width / height | num | decoded actuals (quality gate fills them) |
| aspectRatio / format | text | e.g. `16:9`, `image/webp` |
| fileSize | num | optimized bytes |
| sourceFile | file | original generation (png/jpeg/webp, ≤10 MB) |
| optimizedFile | file | re-encoded target format |
| smallFile | file | `-640` small variant |
| wordpressMediaId | num | idempotent WP upload pointer (reuse forever) |
| wordpressUrl | text | |
| altText / caption | text (≤500) | article-language SEO text |
| filename | text | |
| generationLatency | num | ms |
| estimatedCost | num | |
| attempts / seed | num | |
| error | json | structured failure |
| createdBy | text | audit |

Indexes: UNIQUE (article, role, sectionKey, version); (article, active);
(fingerprint).

### 3.10 documents

| field | type | notes |
|---|---|---|
| project* R | projects | cascade |
| sourceType* | sel | `wordpress` \| `url` \| `manual` |
| sourceId* | text | WP post id (as text) |
| sourceUrl / title | text | |
| contentHash | text | sha256 of stripped plain text — freshness basis |
| embeddingProvider / embeddingModel / embeddingDimensions | text/text/num | recorded per document |
| chunkCount | num | chunk-config guard |
| indexStatus* | sel | `pending` \| `indexed` \| `failed` \| `deleted` |
| indexedAt | date | |
| lastRun R | index_runs | no cascade; audit |

Indexes: UNIQUE (project, sourceType, sourceId); (project, indexStatus).

### 3.11 index_runs

| field | type | notes |
|---|---|---|
| project* R | projects | cascade |
| job R | jobs | nullable, no cascade |
| trigger | sel | `manual` \| `schedule` |
| status* | sel | `running` \| `succeeded` \| `failed` \| `cancelled` |
| totalDocuments / processedDocuments | num | discovered / processed |
| unchangedDocuments / changedDocuments / indexedDocuments / skippedDocuments / failedDocuments | num | counters |
| elapsedSeconds | num | |
| lastSourceId | text | resume checkpoint (WP post id) |
| startedAt / finishedAt | date | |
| error | json | structured |

Index: (project, created).

### 3.12 jobs

| field | type | notes |
|---|---|---|
| project* R | projects | cascade |
| type* | text | one of the 14 registered types (see ARCHITECTURE §5.8) |
| entityType / entityId | text | polymorphic ref: project \| topic \| article \| section \| document |
| status* | sel | `pending` \| `running` \| `completed` \| `failed` \| `cancelled` \| `retrying` — **there is no `claimed` value** |
| priority | num | poll order desc |
| payload | json | small descriptor only (topicId / sectionId / articleId / action / force / full / trigger …) |
| progress | num | 0–100 |
| stage / progressMessage | text | UI progress line |
| currentItem / totalItems | num | granular progress |
| attempts / maxAttempts | num | |
| availableAt | date | gate for pending/retrying claims (backoff, scheduling) |
| lockedAt / lockedBy | date/text | claim stamp |
| leaseExpiresAt / heartbeatAt | date | crash-recovery window |
| startedAt / finishedAt | date | |
| errorCode / errorMessage | text | stable codes, human message |
| errorDetails | json | `{retryable, attempts, details{...}}` incl. traceback tail |
| result | json | handler result summary |
| idempotencyKey* U | text | duplicate creation returns existing job |
| cancelRequested | bool | cooperative cancellation flag |
| parent R | jobs | chain pointer (e.g. publish after write), no cascade |

Indexes: UNIQUE idempotencyKey; (status, availableAt, priority); (project, status);
(entityType, entityId); (type, status).

Worker poll query: fresh `(pending || retrying) && availableAt <= now`
(order priority DESC, created ASC) plus stale recovery
`running && leaseExpiresAt < now`.

### 3.13 job_leases

| field | type | notes |
|---|---|---|
| job* R U | jobs | cascade — the unique constraint IS the atomic claim |
| workerId* | text | |
| expiresAt* | date | extended by heartbeats |

Index: UNIQUE job; (expiresAt).

### 3.14 job_events (append-only)

| field | type | notes |
|---|---|---|
| job R | jobs | nullable (system events), cascade |
| project* R | projects | denormalized for fast filters |
| eventType* | text | see event taxonomy in ARCHITECTURE §5.7 |
| message | text | Persian-safe human summaries |
| metadata | json | structured detail (stage, provider stats, ids) |

Indexes: (job, created); (project, created).

### 3.15 publishing_runs (append-only)

| field | type | notes |
|---|---|---|
| project* R | projects | cascade |
| article* R | articles | cascade |
| job R | jobs | nullable, no cascade |
| status* | sel | `pending` \| `published` \| `failed` \| `skipped_duplicate` — ⚠ the repository also writes `"unpublished"` (unpublish flow) which this enum does not allow (ARCHITECTURE §11.1) |
| attempt | num | 1-based per article |
| mode | sel | `draft` \| `publish` — ⚠ unpublish writes `"unpublish"` (not allowed) |
| wordpressPostId | num | |
| wordpressUrl | text | |
| responseMetadata | json | includes `requestId`, `responseStatus`, `mode`, `link` on success |
| error | json | structured failure |
| startedAt / completedAt | date | |

Indexes: (project, created); (article, created).

> Note: the repository writes a `requestId` key on create, but the collection defines
> no such field — PocketBase drops it; the request id persists via
> `responseMetadata.requestId`. Documented as-is to match reality.

### 3.16 provider_metrics

| field | type | notes |
|---|---|---|
| project* R | projects | cascade |
| provider* / model / operation* / day* | text | aggregation key; day = `YYYY-MM-DD` |
| requestCount / successCount / failureCount / retryCount | num | |
| promptTokens / completionTokens | num | token usage |
| latencySum | num | ms |
| latencyBuckets | json | histogram → P95 computed at read time |

Indexes: UNIQUE (project, provider, model, operation, day); (project, day).
Accumulated in memory per worker; flushed every 30 s and at shutdown.

### 3.17 schedules

| field | type | notes |
|---|---|---|
| project* R | projects | cascade |
| name* | text | |
| kind* | sel | `index` \| `write` |
| enabled | bool | |
| intervalMinutes* | num | ≥1 |
| nextRunAt / lastRunAt | date | due-polling on (enabled, nextRunAt) |
| payload | json | extra descriptor |

Index: (enabled, nextRunAt).

### 3.18 worker_heartbeats (v1.1.0)

Worker liveness registry: each worker process upserts ONE row (unique
`workerId`) every heartbeat interval; the web reads the rows for the workers
dashboard (active/stale/offline + per-session job counts).

| field | type | notes |
|---|---|---|
| workerId* U | text | unique per worker process |
| hostname / version | text | |
| pid / maxConcurrentJobs | num | |
| startedAt / lastHeartbeatAt | date | beacon timestamp |
| runningJobs / completedJobs / failedJobs | num | per-session engine counters |
| scheduleLastPollAt | date | scheduler loop beacon |
| scheduleDue / scheduleCreated / scheduleFailed | num | scheduler counters |

Index: UNIQUE workerId.

### 3.19 app_settings (singleton)

| field | type | notes |
|---|---|---|
| key* U | text | `default` |
| value | json | seeded global defaults: `default_llm_provider=openai_compat`, embedding seed `cohere` / `embed-v4.0` / 1024, `heartbeat_interval=15`, `llm.{outline|section|meta|review}` role configs (`gpt-4o-mini`, temp 0.7, max_tokens 4096, timeout 120; meta/review empty) |

### 3.20 project_members

| field | type | notes |
|---|---|---|
| project* R | projects | cascade |
| user* R | users | cascade |
| role* | sel | `owner` \| `admin` \| `editor` \| `viewer` |

Indexes: UNIQUE (project, user); (user).
Role semantics: owner/admin/editor may mutate; owner/admin for destructive ops;
platform admins bypass membership checks entirely.

### 3.21 users (built-in, extended)

Added by bootstrap if missing: `role` (sel: `admin` \| `member`) and `displayName`
(text). Seeded admin account comes from `SEED_ADMIN_EMAIL` / `SEED_ADMIN_PASSWORD`.

---

## 4. Vector payload contract (Qdrant)

Collection naming: `ezdistro-{project_slug}-{model_slug}`, sanitized and truncated to
63 chars — namespaced per project AND embedding model so switching models never mixes
vectors. Point ids are stable strings `{slug}:{wp_post_id}:{chunk_index}` → idempotent
upserts.

Every point payload carries:

```json
{
  "project":      "<namespace key>",
  "project_id":   "<PB id — EVERY query filters by it>",
  "document_id":  "<PB documents record>",
  "source_id":    "<WP post id>",
  "source_url":   "https://…",
  "title":        "…",
  "chunk_index":  3,
  "content_hash": "sha256…",
  "language":     "fa",
  "created_at":   "YYYY-MM-DD HH:MM:SS.SSSZ",
  "chunk_text":   "…(truncated at 4000 chars)"
}
```

Freshness = `content_hash` equality + matching embedding model/dimensions + unchanged
chunk count. Metadata-only changes update payloads without re-embedding; stale-vector
deletion happens only on explicit full reindexes.

---

## 5. Known drift vs older revisions of this document

- The old document described `publishing_runs.request_id` / `response_status` columns
  and a `claimed` job status; none exist (see §3.15 note and ARCHITECTURE §11).
- Section numbering duplication (two “3.7b”/“3.15” blocks) fixed here.
- Collection count corrected to 20 (was stated as 17/18 in older revisions;
  `article_images` added in v1.3.0, `worker_heartbeats` in v1.1.0).
