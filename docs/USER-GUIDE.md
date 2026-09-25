# EzDistro Platform — User Guide

A practical, task-oriented guide to operating EzDistro through its web interface.
The UI itself is Persian (RTL); this guide gives English translations of all
labels in **bold** so you can match them on screen. No technical background is
required, although the final sections touch on monitoring tools that power
users will appreciate.

```text
Documentation status:  Verified against templates & route handlers
                       (post-v1.3.0, images pipeline)
Last verified:         2026-09-12
UI language:           Persian (RTL) · English available via the globe
                       switcher · PWA-enabled
```

---

## 1. Sign in and get oriented

1. Open the platform URL (development default: `http://localhost:8000`).
2. You are redirected to **Login to EzDistro** (the login page). Enter your email and
   password → you land on **Dashboard**.
3. The top navigation has five areas:
   - **Dashboard** (`/dashboard`) — pipeline overview,
   - **Projects** (`/projects`) — project list,
   - **Jobs** (`/jobs`) — job monitor,
   - **Failed jobs** (`/failed`) — dead-letter shortcut,
   - **Logs** (`/logs`) — event feed.

**Expected result:** the dashboard shows content/article counters, job pipeline
counts, indexing health per project, provider health, failure rate and average
duration. It refreshes itself every few seconds while open.

**Access model:** a *platform admin* sees everything. Regular members see only the
projects they belong to, and inside a project their role decides permissions:
`owner` / `admin` can do everything, `editor` may change content and settings,
`viewer` is read-only. If something refuses with a permission toast, ask an owner or
platform admin.

> First login? The bootstrap script creates one admin account from
> `SEED_ADMIN_EMAIL` / `SEED_ADMIN_PASSWORD`. Change that password immediately in a
> production deployment (PocketBase user management).

## 2. Create a project

**When:** starting work for a new website.

1. Go to **Projects** and create a new project (**+ Topic**-style creation form on the
   projects page).
2. Fill in name, unique slug, description, language (`fa` default), timezone
   (default `Asia/Tehran`).
3. Open the project. It presents twelve tabs:

| Tab (label) | Purpose |
|---|---|
| **Settings** (settings) | chunking, retrieval, generation, retry policy, publishing mode |
| **Connections** (integrations) | credentials: WordPress, LLM, embedding, Qdrant, reranker |
| **AI Models** (ai_models) | per-role model configuration + global defaults |
| **Images** (images) | image-generation providers/models, sizes, optimization, style + one-click generation test |
| **Prompts** (prompts) | prompt library, versions, tester |
| **Topics** (topics) | editorial backlog |
| **Articles** (articles) | generated articles list |
| **Retrieval** (retrieval) | retrieval diagnostics playground |
| **Jobs** (jobs) | this project's jobs |
| **Indexing** (indexing) | index runs, documents health |
| **Publishing** (publishing) | publish history |
| **Logs** (logs) | project event feed |

**Expected result:** the project appears with status `active`. Archived projects
(`archived`) are skipped by the scheduler.

## 3. Connect integrations (**Connections**)

EzDistro needs credentials before it can talk to your site and AI providers. Add one row
per category; mark it **enabled**. Secrets (API keys, application passwords)
are stored encrypted — after saving you only ever see a masked preview like
`sk-1…abcd`.

Minimum set to run the full pipeline:

| Category | Provider options | What you enter |
|---|---|---|
| LLM (language model) | `openai_compat`, `gemini`, `custom`, `ollama`* | base URL + API key (+ model id for custom) |
| Embedding (text embedding) | `cohere`, `openai_compat` | base URL + API key |
| Vector store | `qdrant` | URL (+ optional API key); localhost works only when the server runs with private networks allowed (dev setting) |
| Publisher | `wordpress` | site URL + username + **application password** |
| Reranker (optional) | `cohere_compat` | base URL + API key |
| Image (image, v1.3.0) | `gemini`, `bfl`, `openai_compat` | base URL + API key; cover defaults to Gemini, interiors to FLUX |

\* Ollama appears only when the server is started with `OLLAMA_ENABLED=1`.

For each connection press **Test**: a healthy check stores
`healthy` + latency; failures store `unhealthy` with a short message. The dashboard's
provider-health panel reflects these statuses.

**Common problems:**
- *Test fails with a network/private-address error* — the SSRF guard blocks private
  hosts; local development requires `ALLOW_PRIVATE_NETWORKS=1` on the server process.
- *WordPress test fails* — application passwords must be enabled for your account
  (Users → Profile in WP), and the URL should include the full site path.

## 4. Configure models & settings (**Settings** + **AI Models**)

**AI Models tab:** choose provider + model + temperature/token/timeout per role —
outline and section are used by every article; meta/review roles are optional.
Model discovery lists available models from the provider (cached ~10 minutes);
typing a model id manually always works. Changing a model never rewrites history:
each section keeps the provider/model/prompt version that produced it.

**Settings tab:** sensible defaults exist; typical adjustments:
- Chunking (chunk size 500 words / overlap 100 / strategy `auto`),
- Retrieval pool (top-k 20) and similarity threshold (0 = off),
- Context budget (links 5 / passages 5 / chars 4000),
- Minimum article words (300) — articles below the floor fail assembly validation,
- Publishing mode: **draft** (default) or **publish** (live),
- Retry policy (attempts 3, backoff base 30 s, cap 3600 s).

> Note (documented limitation): the "section concurrency" field is stored but not yet
> enforced by the engine; real parallelism is bounded by worker settings
> (`MAX_CONCURRENT_JOBS`, `LLM_CONCURRENCY`). Also configure the **embedding**
> provider/model/dimensions explicitly — empty fields fall back to generic
> OpenAI-compatible values rather than your seeded defaults.

## 5. Prompts

Prompts control writing behaviour without code changes. Eight types exist per project
(outline system/user, section system/user, SEO rules, internal-linking rules, brand
voice, validation). Every save creates a **new version**; you can activate any older
version to roll back, duplicate a version as inactive, compare two versions
side-by-side, and test a prompt against a chosen topic/model from the tester panel
(testing never touches stored versions).

Variables such as `{{ topic.title }}`, `{{ seo_rules }}`, `{{ retrieved_context }}`
can be inserted; unknown variable names are rejected at save time. Global defaults
(seed data, Persian) apply whenever a project has no own version of a type.

## 6. Index your WordPress content (**Indexing**)

**Before you begin:** an enabled WordPress integration, and embedding settings +
Qdrant integration in place.

1. Open the **Indexing** tab.
2. Press the incremental index action to fetch published WordPress posts, or
   **Full re-index** for a full reindex (also deletes vectors of removed posts).
3. Watch the run appear under Indexing runs with counters: **discovered**,
   **unchanged**, **indexed**, **failed**, elapsed seconds.

**What happens:** each post's text is hashed; unchanged posts are skipped without
re-embedding; changed posts are chunked (per project chunking settings), embedded in
batches and stored in Qdrant. Runs checkpoint every 10 posts — if a run fails or a
worker restarts, retrying resumes from the checkpoint instead of starting over.
Changed embedding model/dimensions? Re-index into the new namespace (the UI guides
this; old vectors stay isolated per model).

Single post changed? Use the per-document reindex action instead of a full run.

## 7. Topics: queue article writing

1. In **Topics**, add topics with **New topic**: title, optional keyword, pillar /
   cluster labels, type (article/pillar/guide/news), priority, editorial week and
   published URL.
2. Filter chips show counts per status; search matches title/keyword; sorting by
   priority/created/title; bulk actions allow generating/retrying/cancelling several
   topics at once.
3. **Bulk add (CSV / bulk)** imports many topics at once:
   - Paste text (one topic per line, or full CSV) or upload a `.csv`/`.tsv` file.
   - Columns are auto-detected from headers (`Title`,
     `Keyword`, `Related Pillar`, `Cluster`, `Type`,
     `Priority`, `Week`, `URL`, …) and can be re-mapped before
     importing.
   - Rows whose title or keyword already exists (in the project or the same file)
     are skipped automatically; unknown type labels fall back to *article* and are
     reported. The result screen shows added / skipped / error counts per row.
4. Press **Write article** on a topic to enqueue writing. Topic moves through:
   `planned → planning → outline_ready → writing → review`.
5. When the assembler finishes, the topic sits at **review** and its article waits in
   **Articles**.

**Expected result:** a fully structured article with outline, sections, word count and
SEO score — never an automatic publish.

## 8. Work on the article workspace (**Articles**)

Open an article → the three-pane workspace:

- **Left:** outline navigation — move/add/delete sections, edit a section's brief.
  Every structural edit bumps the outline version; unchanged sections keep their
  content, changed briefs reset to pending for regeneration.
- **Center:** section editor. While generating (~every 2.5 s refresh) you see live
  status, model, elapsed time; failed sections show a human-readable error plus a
  technical-details toggle. You can edit any section's HTML manually — every edit is
  saved.
- **Right:** metadata — status, SEO score, words, keyword, per-section provenance
  (provider/model/prompt version), internal links, validation report, publish
  history, and **Version history** (revision history) with rollback.

Actions available: assemble/regenerate buttons (**Regenerate article** regenerates the
whole article after snapshotting the current version; **Full regenerate** is the fuller
variant), status transitions, and publishing controls (see next section).
Regeneration never destroys the previous text — it becomes a rollback point.

**Images pane (v1.3.0):** below the editor, the images pane shows the article's
image plan (version badge, e.g. `1 cover + 2 inline images`) with per-slot
status, the visual prompt (image prompt collapsible), generated versions, and
alt-text/caption fields. Actions per slot: **Generate image** (first generation),
**Regenerate** (new version — the current one is kept), **Re-optimize**
(re-optimize), **Upload to WordPress** (upload, reuses existing media on
retry), and per-version **Use this image** (activate) / **Delete**
(deactivate). If no plan exists yet (`No image plan yet`), press
**Image plan** to build one — planning also runs automatically after
assembly. Configure providers, models, aspect ratios,
`minimum cover width`, `maximum inline images`, optimization format and style in
the project **Images** tab, and use its test button to verify generation before
running the pipeline.

## 9. Review, approve, publish (review screen → **Publishing**)

The review screen shows title/slug/meta description, the sanitized body, internal
links, word count, SEO score, per-section provenance, and a live **validation panel**.

Workflow and buttons:

1. Fix issues until the validation panel passes (empty sections, broken links,
   duplicate headings, length, keyword placement are checked).
2. Press **Approve** to approve. Approval is required before publishing — enforced in
   the UI **and** in the publish job itself.
3. Press **Publish** to send to WordPress. Mode follows the project setting
   (`draft` or live). Ready article images are uploaded to the WP media library
   (idempotent — retries reuse the stored media id), placeholders in the body
   are resolved, and the cover becomes the post's featured image. A missing
   cover blocks publishing unless overridden. The article enters `publishing`, then `published`; the WordPress
   URL appears in the metadata pane and the **Publishing** tab records every attempt
   (attempt number, mode, response, timestamps).
4. Re-publishing later performs a safe **update** of the same WP post — duplicates are
   prevented even across crashes (orphan lookup by slug + stored post id + meta tag).
5. **Send back** sends the article back to the writer with your note
   (`sent_back`); regenerate from there.
6. Unpublish sets the WordPress post to private.
   > ⚠️ Known issue: against the current database schema the unpublish action writes
   > values the schema does not allow and may fail validation (see
   > TROUBLESHOOTING.md §Unpublish). Verify on your deployment before relying on it.

## 10. Retrieval diagnostics (**Retrieval**)

Enter a query to see exactly what the writer would see: retrieved documents with
vector scores, rerank scores (if enabled), final internal-link candidates and the
exact trimmed prompt context. Use it to tune top-k/threshold/reranking or to explain
"why does the article ignore X?".

## 11. Monitor jobs (**Jobs** / **Failed jobs** / **Logs**)

- **Jobs** (`/jobs`): filter by project, type, status, date range, worker and error
  code (**Apply filter** applies). Rows link to detail pages. Auto-refreshes ~8 s.
- **Job detail:** progress bar + stage + message, attempts, timings, worker + lease
  info, error summary, and the full event timeline (stage changes, retries, provider
  calls with latency/tokens when enabled). Technical error details are visible to
  admins only.
- **Cancel:** **Cancel** sets a cancel request; the handler stops at the next safe
  checkpoint (current step finishes first).
- **Retry:** failed jobs offer **Retry**; transient failures retry automatically
  with exponential backoff + jitter (honoring provider rate-limit headers).
- **Failed jobs** is the filtered shortcut to everything dead-lettered.
- **Logs** shows the raw event feed for the whole platform.

## 12. Quick reference: what do I do when…?

| Goal | Where |
|---|---|
| New website | **Projects** → new project |
| Connect WordPress/AI keys | **Connections** → add + **Test** |
| Choose models per role | **AI Models** |
| Tune writing style | **Prompts** (+ brand voice / SEO rules) |
| Feed the knowledge base | **Indexing** → index / **Full re-index** |
| Queue an article | **Topics** → **Write article** |
| Edit/polish an article | **Articles** → workspace |
| Plan/regenerate images | workspace images pane → **Image plan** / **Regenerate** |
| Configure image pipeline | project **Images** tab (providers, models, sizes, style, test) |
| Approve & publish | review screen → **Approve** → **Publish** |
| Something failed | **Failed jobs** → inspect → **Retry** |
| Explain retrieval quality | **Retrieval** diagnostics |

Deeper background (how jobs recover from crashes, how retries/backoff work, security
model): see ARCHITECTURE.md and FAILURES.md. Symptom-driven fixes:
TROUBLESHOOTING.md.
