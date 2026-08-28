# Seoz Platform — User Guide

A practical, task-oriented guide to operating Seoz through its web interface.
The UI is **Persian (RTL)**; this guide quotes the exact on-screen labels so you can
match them. No technical background is required, although the final sections touch on
monitoring tools that power users will appreciate.

```text
Documentation status:  Verified against templates & route handlers
Last verified:         2026-08-22
UI language:           فارسی (Persian, RTL) · PWA-enabled
```

---

## 1. Sign in and get oriented

1. Open the platform URL (development default: `http://localhost:8000`).
2. You are redirected to **ورود به سئوز** (the login page). Enter your email and
   password → you land on **داشبورد** (Dashboard).
3. The top navigation has five areas:
   - **داشبورد** (`/dashboard`) — pipeline overview,
   - **پروژه‌ها** (`/projects`) — project list,
   - **وظایف** (`/jobs`) — job monitor,
   - **وظایف ناموفق** (`/failed`) — dead-letter shortcut,
   - **رویدادها** (`/logs`) — event feed.

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

1. Go to **پروژه‌ها** and create a new project (**+ موضوع**-style creation form on the
   projects page).
2. Fill in name, unique slug, description, language (`fa` default), timezone
   (default `Asia/Tehran`).
3. Open the project. It presents eleven tabs:

| Tab (label) | Purpose |
|---|---|
| **تنظیمات** (settings) | chunking, retrieval, generation, retry policy, publishing mode |
| **اتصالات** (integrations) | credentials: WordPress, LLM, embedding, Qdrant, reranker |
| **مدل‌های AI** (ai_models) | per-role model configuration + global defaults |
| **پرامپت‌ها** (prompts) | prompt library, versions, tester |
| **موضوع‌ها** (topics) | editorial backlog |
| **مقاله‌ها** (articles) | generated articles list |
| **بازیابی** (retrieval) | retrieval diagnostics playground |
| **وظایف** (jobs) | this project's jobs |
| **نمایه‌سازی** (indexing) | index runs, documents health |
| **انتشار** (publishing) | publish history |
| **رویدادها** (logs) | project event feed |

**Expected result:** the project appears with status `active`. Archived projects
(`archived`) are skipped by the scheduler.

## 3. Connect integrations (اتصالات)

Seoz needs credentials before it can talk to your site and AI providers. Add one row
per category; mark it **enabled** (فعال). Secrets (API keys, application passwords)
are stored encrypted — after saving you only ever see a masked preview like
`sk-1…abcd`.

Minimum set to run the full pipeline:

| Category | Provider options | What you enter |
|---|---|---|
| LLM (مدل زبانی) | `openai_compat`, `gemini`, `custom`, `ollama`* | base URL + API key (+ model id for custom) |
| Embedding (جاسازی متن) | `cohere`, `openai_compat` | base URL + API key |
| Vector store | `qdrant` | URL (+ optional API key); localhost works only when the server runs with private networks allowed (dev setting) |
| Publisher | `wordpress` | site URL + username + **application password** |
| Reranker (optional) | `cohere_compat` | base URL + API key |

\* Ollama appears only when the server is started with `OLLAMA_ENABLED=1`.

For each connection press **تست** (test): a healthy check stores
`healthy` + latency; failures store `unhealthy` with a short message. The dashboard's
provider-health panel reflects these statuses.

**Common problems:**
- *Test fails with a network/private-address error* — the SSRF guard blocks private
  hosts; local development requires `ALLOW_PRIVATE_NETWORKS=1` on the server process.
- *WordPress test fails* — application passwords must be enabled for your account
  (Users → Profile in WP), and the URL should include the full site path.

## 4. Configure models & settings (تنظیمات + مدل‌های AI)

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

## 5. Prompts (پرامپت‌ها)

Prompts control writing behaviour without code changes. Eight types exist per project
(outline system/user, section system/user, SEO rules, internal-linking rules, brand
voice, validation). Every save creates a **new version**; you can activate any older
version to roll back, duplicate a version as inactive, compare two versions
side-by-side, and test a prompt against a chosen topic/model from the tester panel
(testing never touches stored versions).

Variables such as `{{ topic.title }}`, `{{ seo_rules }}`, `{{ retrieved_context }}`
can be inserted; unknown variable names are rejected at save time. Global defaults
(seed data, Persian) apply whenever a project has no own version of a type.

## 6. Index your WordPress content (نمایه‌سازی)

**Before you begin:** an enabled WordPress integration, and embedding settings +
Qdrant integration in place.

1. Open the **نمایه‌سازی** tab.
2. Press the incremental index action to fetch published WordPress posts, or
   **بازنمایه کامل** for a full reindex (also deletes vectors of removed posts).
3. Watch the run appear under اجراهای نمایه‌سازی with counters: کشف (discovered),
   بدون تغییر (unchanged), نمایه‌شده (indexed), خطا (failed), elapsed seconds.

**What happens:** each post's text is hashed; unchanged posts are skipped without
re-embedding; changed posts are chunked (per project chunking settings), embedded in
batches and stored in Qdrant. Runs checkpoint every 10 posts — if a run fails or a
worker restarts, retrying resumes from the checkpoint instead of starting over.
Changed embedding model/dimensions? Re-index into the new namespace (the UI guides
this; old vectors stay isolated per model).

Single post changed? Use the per-document reindex action instead of a full run.

## 7. Topics: queue article writing (موضوع‌ها)

1. In **موضوع‌ها**, add topics with **موضوع جدید**: title, optional keyword, pillar /
   cluster labels, type (article/pillar/guide/news), priority, editorial week and
   published URL.
2. Filter chips show counts per status; search matches title/keyword; sorting by
   priority/created/title; bulk actions allow generating/retrying/cancelling several
   topics at once.
3. **افزودن گروهی (CSV / گروهی)** imports many topics at once:
   - Paste text (one topic per line, or full CSV) or upload a `.csv`/`.tsv` file.
   - Columns are auto-detected from headers (English or Persian: `Title/عنوان`,
     `Keyword/کلمه کلیدی`, `Related Pillar/ستون`, `Cluster/خوشه`, `Type/نوع`,
     `Priority/اولویت`, `Week/هفته`, `URL/لینک`, …) and can be re-mapped before
     importing.
   - Rows whose title or keyword already exists (in the project or the same file)
     are skipped automatically; unknown type labels fall back to *مقاله* and are
     reported. The result screen shows added / skipped / error counts per row.
4. Press **نوشتن مقاله** on a topic to enqueue writing. Topic moves through:
   `planned → planning → outline_ready → writing → review`.
5. When the assembler finishes, the topic sits at **review** and its article waits in
   **مقاله‌ها**.

**Expected result:** a fully structured article with outline, sections, word count and
SEO score — never an automatic publish.

## 8. Work on the article workspace (مقاله‌ها)

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
  history, and **تاریخچه نسخه‌ها** (revision history) with rollback.

Actions available: assemble/regenerate buttons (**بازتولید مقاله** regenerates the
whole article after snapshotting the current version; **بازتولید کامل** is the fuller
variant), status transitions, and publishing controls (see next section).
Regeneration never destroys the previous text — it becomes a rollback point.

## 9. Review, approve, publish (review screen → انتشار)

The review screen shows title/slug/meta description, the sanitized body, internal
links, word count, SEO score, per-section provenance, and a live **پنل اعتبارسنجی**
(validation panel).

Workflow and buttons:

1. Fix issues until the validation panel passes (empty sections, broken links,
   duplicate headings, length, keyword placement are checked).
2. Press **تأیید** to approve. Approval is required before publishing — enforced in
   the UI **and** in the publish job itself.
3. Press **انتشار** to send to WordPress. Mode follows the project setting
   (`draft` or live). The article enters `publishing`, then `published`; the WordPress
   URL appears in the metadata pane and the **انتشار** tab records every attempt
   (attempt number, mode, response, timestamps).
4. Re-publishing later performs a safe **update** of the same WP post — duplicates are
   prevented even across crashes (orphan lookup by slug + stored post id + meta tag).
5. **بازگرداندن** sends the article back to the writer with your note
   (`sent_back`); regenerate from there.
6. Unpublish sets the WordPress post to private.
   > ⚠️ Known issue: against the current database schema the unpublish action writes
   > values the schema does not allow and may fail validation (see
   > TROUBLESHOOTING.md §Unpublish). Verify on your deployment before relying on it.

## 10. Retrieval diagnostics (بازیابی)

Enter a query to see exactly what the writer would see: retrieved documents with
vector scores, rerank scores (if enabled), final internal-link candidates and the
exact trimmed prompt context. Use it to tune top-k/threshold/reranking or to explain
"why does the article ignore X?".

## 11. Monitor jobs (وظایف / وظایف ناموفق / رویدادها)

- **وظایف** (`/jobs`): filter by project, type, status, date range, worker and error
  code (**اعمال فیلتر** applies). Rows link to detail pages. Auto-refreshes ~8 s.
- **Job detail:** progress bar + stage + message, attempts, timings, worker + lease
  info, error summary, and the full event timeline (stage changes, retries, provider
  calls with latency/tokens when enabled). Technical error details are visible to
  admins only.
- **Cancel:** **لغو** sets a cancel request; the handler stops at the next safe
  checkpoint (current step finishes first).
- **Retry:** failed jobs offer **تلاش مجدد**; transient failures retry automatically
  with exponential backoff + jitter (honoring provider rate-limit headers).
- **وظایف ناموفق** is the filtered shortcut to everything dead-lettered.
- **رویدادها** shows the raw event feed for the whole platform.

## 12. Quick reference: what do I do when…?

| Goal | Where |
|---|---|
| New website | پروژه‌ها → new project |
| Connect WordPress/AI keys | اتصالات → add + تست |
| Choose models per role | مدل‌های AI |
| Tune writing style | پرامپت‌ها (+ brand voice / SEO rules) |
| Feed the knowledge base | نمایه‌سازی → index / بازنمایه کامل |
| Queue an article | موضوع‌ها → نوشتن مقاله |
| Edit/polish an article | مقاله‌ها → workspace |
| Approve & publish | review screen → تأیید → انتشار |
| Something failed | وظایف ناموفق → inspect → تلاش مجدد |
| Explain retrieval quality | بازیابی diagnostics |

Deeper background (how jobs recover from crashes, how retries/backoff work, security
model): see ARCHITECTURE.md and FAILURES.md. Symptom-driven fixes:
TROUBLESHOOTING.md.
