# EzDistro Platform — User Guide

A practical, task-oriented guide to operating EzDistro through its web interface.
The UI is English; labels are shown in **bold** so you can match them on screen. No
technical background is required, although the monitoring sections touch on tools
power users will appreciate.

```text
Documentation status:  Verified against templates & route handlers (v1.27.3)
Last verified:         2026-10-25
UI language:           English (LTR) · PWA-enabled
```

---

## 1. Sign in and get oriented

1. Open the platform URL (development default: `http://localhost:8000`).
2. You land on **Sign in or create an account** (`/login`). Press **Continue with
   Google** or **Continue with GitHub** — there is no password form. The provider
   signs you in, or creates the account on first use.
3. After the callback you land on **Overview** (`/dashboard`).

The desktop rail has three sections (the mobile dock keeps **Overview**,
**Websites**, **Activity** and **Automation**):

| Section | Item | What it is |
|---|---|---|
| Workspace | **Overview** (`/dashboard`) | Content progress across your websites and what needs you |
| Workspace | **Websites** (`/projects`) | Your projects (one per website/market) |
| Operations | **Activity** (`/jobs`) | Every background task, its progress and failures |
| Operations | **Automation** (`/schedules`) | What runs on a schedule, what is paused, what is overdue |
| Operations | **Workers** (`/workers`) | Background processes, version match, restart |
| Operations | **Events** (`/logs`) | Platform event feed (filter by level) |
| Account | **Plan & billing** (`/billing`) | Plan, managed-AI allowance, payment history (when billing is on) |

**Overview** shows **Content progress** (ideas → planned → writing → review →
published), a **Needs your attention** list, and **Behind the scenes** links to
Activity and Automation. It refreshes its partials as you work; there is no
job/provider health dashboard here — those live under **Activity** and
**Workers**.

**Access model:** a *platform admin* sees everything and is the only role that can
edit global AI defaults, restart a worker or manage plan products. Regular members
see only the projects they belong to; inside a project `owner` / `admin` can do
everything including deleting the website, `editor` may change content and settings,
`viewer` is read-only. Permission refusals arrive as a toast.

> **First sign-in:** an admin account is provisioned from `SEED_ADMIN_EMAIL` at
> bootstrap. There is no password to change — sign in with Google/GitHub using
> exactly that email and PocketBase links the identity to the existing admin record.
> Bootstrap also promotes that account to `admin` if it signed up first.

## 2. Add a website

**When:** starting work for a new site or a new market.

1. Go to **Websites** and press **Add website**.
2. Fill in **Website name** (the slug is derived from it, with `-2`, `-3`… on
   collision), optional **Internal address label**, and optionally **Copy setup from
   another website** to clone connections, settings, prompts and routines.
3. Open the website. It has **two tab groups**:

| Group | Tab | Purpose |
|---|---|---|
| Grow your website | **Overview** | This site's progress and next action (default) |
| | **Find opportunities** | Harvest, Google Ads, file import → clusters → opportunity roadmap |
| | **Content plan** | Topics: add, import, filter, **Write** |
| | **Articles** | Generated articles and mirrored WordPress posts |
| | **Published** | Publishing history and live posts |
| Set it up | **Website setup** | Basics, language/brand, retrieval, publishing, automation, landing page |
| | **Advanced** | Hub for Connections, AI models, Writing rules, Article images, Search settings, Site health, Technical task history, Diagnostic logs |

The header carries the website status toggle (**Paused** in the list; paused websites
are skipped by automation), **Refresh site content**, **Continue writing** (resume the
newest unfinished article) and, for owners/admins, **Delete project** in the **More
actions** menu.

**Expected result:** the website appears in the list as **Active**. Nothing runs until
you add at least a language-model connection and WordPress publishing credentials.

## 3. Connect integrations (**Advanced → Connections**)

Add one connection per category and leave it enabled. Secrets (API keys,
application passwords) are Fernet-encrypted; after saving you only see a masked
preview like `sk-1…abcd`. Each row has **Test** (stores health + latency)
and can be edited or disabled without deleting history.

Minimum set for the full loop:

| Category (UI label) | Providers | What you enter |
|---|---|---|
| **Language model (LLM)** | `openai_compat`, `gemini`, `custom`, `ollama`* | base URL + API key + model id |
| **Text embedding** | `openai_compat` | base URL + API key + embedding model + dimensions |
| **Vector store (Qdrant)** | `qdrant` | URL (+ optional key) |
| **Publishing (WordPress)** | `wordpress` | site URL + username + **application password** |
| **Image generation (AI Image)** *(optional)* | `gemini`, `bfl`, `openai_compat` | base URL + API key; cover defaults to Gemini, interiors to FLUX |
| **Reranking** *(optional)* | `cohere_compat` | base URL + API key |
| **SERP data** *(optional)* | `serper` | API key — enables live SERP observations in research |
| **Google Ads (keyword research)** *(optional)* | `google_ads` | Google Cloud OAuth client id, redirect URI, client secret (+ optional login customer id / developer token) |

\* **Ollama** appears only when the server runs with `OLLAMA_ENABLED=1`.

**Common problems:**
- *Test fails with a private-address error* — the SSRF guard blocks private hosts;
  local development needs `ALLOW_PRIVATE_NETWORKS=1` on the server.
- *WordPress test fails* — application passwords must be enabled for that account
  (WP → Users → Profile), and the URL should be the site root.
- *No model is used* — the role has no model and the connection has none; set one on
  the connection or in **AI models**.

## 4. Configure the website (**Website setup**)

Sections are collapsible. Defaults are sane; the ones worth checking before the
first article:

- **Basics** — name, slug, publishing mode (**publish** by default; choose **draft**
  if you want drafts in WordPress), default WordPress category.
- **Language & brand** — target locale / market / audience (default `en-US` / `US`;
  the writing language derives from the locale), brand name, preferred/forbidden
  terminology, URL policy, product context.
- **Landing page** — **Landing page URL** + **Analyse page**. A durable job fetches
  the page, extracts a factual product profile and stores it so every research,
  outline and section prompt knows what you sell. Nothing is invented: facts come
  from the page.
- **Retrieval & chunking** — chunk size 500 words / overlap 100 / `auto`; top-k 20;
  similarity threshold 0.35 (`0` disables the filter); context budget 5 links /
  5 passages / 4000 chars.
- **Publishing & automation** — publishing mode, **Autosave** (off by default,
  interval 5 min), **Auto-publish** (off by default; see §7).
- **Scheduling & retry** — per-website routines (index / write / publish) and the
  retry policy (3 attempts, base 30 s, cap 3600 s).
- **Minimum article words** (300) — articles below the floor fail assembly.

> The **section concurrency** field is stored but does not add parallelism; the real
> limits are the worker settings `MAX_CONCURRENT_JOBS` and `LLM_CONCURRENCY`.
> Configure embeddings explicitly — there is no seeded embedding model.

## 5. AI models (**Advanced → AI models**)

Per role (`outline`, `section`, and optional `meta`, `review`) choose provider +
model, plus temperature / max tokens / timeout. **Fetch** lists the models the
provider offers (cached ~10 minutes); typing a model id manually always works.
The global-defaults panel is titled **Global defaults (admin only)** — it only
affects projects that do not override the role.

**Embedding** model + dimensions are set on the *embedding connection* / in
**Website setup**; changing the model starts a new Qdrant namespace, changing only
the dimensions requires a re-index into the same namespace.

## 6. Writing rules (**Advanced → Writing rules**)

Prompts control writing behaviour without code changes. The tab exposes 21
project-editable types (brand voice, SEO content contract, research, outline,
section, internal linking, metadata, article QA/repair, image planning) — each with
system/user where applicable. Editorial and taxonomy prompts (`cluster_*`,
`opportunity_*`, `category_*`) are internal/global and are not editable here.

Every save creates a **new version**; you can activate an older version to roll
back, duplicate a version as inactive, compare versions, and test a prompt against a
chosen topic/model from the tester panel (testing never changes stored versions).
Global English defaults apply whenever a project has no own version of a type.
Variables such as `{{ topic.title }}`, `{{ seo_rules }}`, `{{ retrieved_context }}`,
`{{ product_profile }}` can be inserted; unknown names are rejected at save time.

## 7. Feed a website into the knowledge base (**Advanced → Site health**)

**Before you begin:** an enabled WordPress connection, plus embedding settings and a
Qdrant connection.

1. Open **Site health**.
2. Press **Incremental indexing** to fetch published posts, or **Full reindex** to
   also delete vectors of removed posts.
3. Watch the run appear with counters: **Discovered**, **Unchanged**, **Changed**,
   **Indexed**, **Skipped**, **Error**, **Duration (s)**.

Unchanged posts are skipped by content hash without re-embedding; changed posts are
chunked, embedded in batches and upserted into Qdrant with stable point ids. Runs
checkpoint every 10 posts — a retried run resumes instead of starting over. Single
post changed? Re-index that document from its row instead of a full run.

## 8. Queue writing (**Content plan**)

1. Press **Add topic** (or **+ Topic** on the full content-plan page) and fill in
   title, optional keyword, pillar/cluster, type, priority, editorial week and URL.
2. The tab offers a status filter, search and bulk **Generate** / **Retry** /
   **Cancel**. The full page (`…/topics`) adds status chips with counts and
   priority/created/title sorting.
3. **Import CSV** (or **Bulk add topics**) pastes plain lines, CSV or TSV, or uploads
   a `.csv`/`.tsv`. Columns are auto-detected (`Title`, `Keyword`, `Related Pillar`,
   `Cluster`, `Type`, `Priority`, `Week`, `URL`, …) and re-mappable; duplicate
   titles/keywords are skipped and the result reports added / skipped / errors.
4. Press **Write** on a topic. It moves through `queued → planned → outline_ready →
   writing → review`; the article then waits in **Articles**.

**Expected result:** a structured article with outline, sections, word count and SEO
score. By default nothing publishes without your approval; a website with
auto-publish on can publish schedule-generated articles that clear the score gate
(§9).

## 9. Work on an article (**Articles → workspace**)

Open an article to get the three-pane workspace:

- **Left — Outline:** reorder, add or delete sections and edit a section's brief.
  Structural edits bump the outline version; changed briefs reset that section to
  pending.
- **Center — section editor:** live status, model and elapsed time while generating
  (the page polls every 4 s), human-readable errors with a technical-details toggle,
  and a manual HTML editor (every edit saved).
- **Right — Title and meta · Checks · Article · Internal links · Publish history.**

Header actions depend on status: **Review and approve** (review), **Publish**
(approved), **Update** (published), **Resume <stage>** (failed/sent back with a
recoverable step), **Review**; the **More actions** menu holds **Rebuild from
sections**, **Regenerate the article** (snapshots the current version first) and
**Unpublish** (sets the WordPress post to private). Per-section **Regenerate** only
replaces that section.

**Article images pane (bottom of the workspace):** shows the plan version (`v1`,
`v2`…), per-slot **Article cover** / **In-text image** rows with status, aspect
ratio, dimensions, prompt, **Alt text**, **Caption**, and the **Versions** list.
Actions: **Image plan** (build the plan; it also runs automatically after assembly),
**Regenerate** (creates a new version, keeps the old), **Re-optimize**, **Upload to
WordPress** (reuses existing media on retry), **Use this image**, **Delete**. If no
plan exists the pane says **The image plan has not been created**.

Configure provider/model, aspect ratios, **Minimum cover width (px)** and **Max
in-text images**, optimization format and style in **Advanced → Article images**;
the same page has a one-click generation test.

> **Known issue:** the unpublish action writes values the current
> `publishing_runs` schema does not allow and may fail validation (see
> TROUBLESHOOTING.md). Verify on your deployment before relying on it.

## 10. Review, approve, publish

The review screen has **Checks** (structural validation: empty sections, broken
links, duplicate headings, length, keyword placement) and **Editorial QA** (an LLM
audit that reports critical/high findings; an automatic repair pass may have run).
Both decide whether **Approve** is enabled.

1. Fix anything under **Checks** or **Editorial QA**.
2. Press **Approve**. Approval is required before publishing and is enforced in the
   publish job as well as the UI.
3. Press **Publish to WordPress**. Mode follows the website setting (`publish` or
   `draft`). Images are uploaded to the WP media library idempotently, body
   placeholders are resolved, and the cover becomes the featured image (a missing
   cover blocks publishing unless overridden). The article moves through
   `publishing` to `published`; the WordPress URL appears in **Article details** and
   **Published** / **Publish history** records every attempt.
4. Re-publishing later safely **updates** the same post (stored post id + slug
   recovery + meta tag prevent duplicates).
5. **Return for changes** sends the article back with a note (`sent_back`);
   regenerate from there.
6. **Section provenance** shows which provider/model/prompt version wrote each
   section. **Version history** lists revisions with **Restore** — restoring never
   destroys the current version.

After a successful live publish the worker also queues **inbound internal link
building**: up to three already-published, closely related pages get one contextual
link to the new article (existing wording only, never a rewrite).

## 11. Browse published work (**Published**)

**Published** lists the publish attempts: article, attempt, mode, status, WordPress
post id, live URL and time. Use it to confirm what is live and to jump back into the
workspace for an update. Mirrored WordPress posts also appear in **Articles**, marked
as WordPress-sourced on the record; a remote change over local edits sets
`update_available`.

## 12. Tune retrieval (**Advanced → Search settings**)

**Search settings** holds chunking, top-k, similarity threshold, reranking and the
context budget, plus a diagnostics playground: enter a query to see exactly what the
writer would see — retrieved documents with vector scores, rerank scores, final
internal-link candidates and the trimmed prompt context. Use it to explain "why does
the article ignore X?".

## 13. Find opportunities (**Find opportunities**)

The tab turns real search demand, your site and competitor pages into an
evidence-backed roadmap. **Facts first, AI second** — it never invents search
volume, CPC, competition or rankings. Everything is optional and degrades: harvest
and file import need no Google Ads, and research runs with no SERP provider or LLM.

### Option A — free autocomplete harvest (recommended)

The default form expands seeds through public autocomplete (Google, YouTube, Bing,
DuckDuckGo, Amazon), then clusters and builds the roadmap. Fill in **Run name**,
**Seed keywords** (one per line), **Research goal**, **Country**, **Language**,
**Clustering** and optional **Maximum keywords**, then press **Harvest and build the
roadmap**. A keyword only carries a search volume if this project already collected
one; everything else stays unmeasured rather than estimated.

### Option B — Google Ads

1. **Connections → Google Ads** with client id, redirect URI and client secret (the
   URI must match the registered one character-for-character). API access rides on
   the Google Cloud project; there is no developer token to obtain.
2. On **Find opportunities** press **Connect Google Ads** → consent → you return with
   an inline status (`connected` / `denied` / `failed`). Pick the account with **Use
   for research**; **Refresh accounts** re-lists customers.
3. Under **Tell us about your goals**, fill in the plain-language fields (**What do
   people search for?**, **Your website address**, **Important pages**, **Similar
   websites**, **What would success look like?**). **More options** holds country,
   language, network, clustering, caps, **Include adult keywords** and **Refresh
   cached data**.
4. Press **Find opportunities**. The run is durable and resumable; identical
   targeting + seeds reuses the previous run unless you tick **Refresh cached data**.

### Option C — Keyword Planner file (no Google Ads API)

1. Upload a Google Keyword Planner export (CSV or XLSX, up to 20 MB) under **Already
   have a keyword list?**
2. Press **Analyse imported keywords**. Columns are detected automatically; the run
   page reports what was read.

### What you get

The run page has **Keywords**, **SERP**, **Competitors**, **Content gaps**,
**Clusters** and **Opportunities**. Keywords carry *paid* competition (never organic
difficulty), CPC and volume where measured. Bulk accept/reject/mark-roadmap works for
clusters and opportunities; **Export CSV** downloads opportunities and **Keywords
CSV** downloads the keyword set (useful for a Keyword Planner volume round trip). A
run can be cancelled or resumed.

**Handoff:** accepting an opportunity creates a planned topic (keyword + cluster) in
**Content plan**; for `update`/`expand` it points at the existing page and refreshes
its keyword/cluster. The brief stays on the idea. The research engine never writes or
publishes by itself.

> **Without a SERP provider** the ranking/PAA/snippet fields show *unavailable* — they
> are never estimated.

## 14. Automate it (**Automation**)

**Automation** lists every website's routines — **Refresh website content** (index),
**Write articles**, **Publish articles** — with their next run, state (**Enabled**,
**Paused**, **Due now**, **Overdue**, **Website paused**) and a per-website
**Edit routine** link into **Website setup**.

- Enable an indexing routine to keep the vector corpus current.
- Enable a writing routine to draft on a cadence (one topic per window).
- Enable a publishing routine only with auto-publish configured if you want live
  output without review.
- A banner tells you when no automation check has been recorded or tasks are overdue —
  that usually means the worker process is down (see **Workers** and
  TROUBLESHOOTING.md).

## 15. Monitor work (**Activity · Workers · Events**)

- **Activity** (`/jobs`): filter by **Website** and **Status** and press **Show
  activity**; rows link to job detail. The table refreshes while open.
- **Job detail:** progress + stage + message, attempts, timings, worker/lease info,
  error summary and the event timeline (stages, retries, provider calls when
  enabled). Technical error details are visible to admins.
- **Cancel** requests a stop at the next safe checkpoint (a running step finishes
  first). Cancelling a queued job records the request but the job still runs when
  claimed.
- **Retry** revives a failed job and resets its attempt budget.
- The dead-letter view at `/failed` groups failed jobs by reason; it is reachable from
  the Workers page's failed-jobs **All** link (and by URL).
- **Workers** shows liveness (refreshes every 5 s), a **Needs restart** badge when a
  worker's version differs from the web process, **Restart the worker** guidance for
  admins, and per-worker counters. A restart badge means a redeploy left an old
  process.
- **Events** is the platform event feed with level filters (**All / info / warning /
  error**).

## 16. Plan & billing (optional)

Billing is **off by default**. With `BILLING_ENABLED=false` the **Plan & billing**
page says **Billing is turned off** and every feature is free. When an operator
enables it, the page shows the current plan, managed-AI articles left, plans
(**Pay for period**) and top-ups (**Buy top-up**), and payment history. Checkout is a
one-time crypto payment via PayRam; plans are prepaid periods, not auto-renewing.

Projects with their own LLM connection are unmetered. Managed-AI credits apply to
projects without one — but note that generation still requires an LLM connection, so
a project with no LLM key cannot generate even with credits. Add provider keys on
**Connections** unless your operator has wired a platform model.

## 17. Quick reference: what do I do when…?

| Goal | Where |
|---|---|
| New website | **Websites** → **Add website** |
| Connect WordPress/AI keys | **Advanced → Connections** → add + **Test** |
| Choose models per role | **Advanced → AI models** |
| Tune writing style | **Advanced → Writing rules** |
| Teach it what you sell | **Website setup → Landing page → Analyse page** |
| Feed the knowledge base | **Advanced → Site health** → **Incremental indexing** / **Full reindex** |
| Queue an article | **Content plan** → **Write** |
| Edit/polish an article | **Articles** → workspace |
| Plan/regenerate images | workspace images pane → **Image plan** / **Regenerate** |
| Configure image pipeline | **Advanced → Article images** |
| Approve & publish | review screen → **Approve** → **Publish to WordPress** |
| Research for free | **Find opportunities** → seeds → **Harvest and build the roadmap** |
| Research with Google Ads | **Connections → Google Ads** → **Connect Google Ads** → **Find opportunities** |
| Research from a file | **Find opportunities** → Keyword Planner upload → **Analyse imported keywords** |
| Turn demand into topics | run page → **Opportunities** → accept |
| Schedule work | **Automation** (or **Website setup → Scheduling**) |
| Something failed | **Activity** / **Needs your attention** → inspect → **Retry** |
| Explain retrieval quality | **Advanced → Search settings** |

Deeper background (how jobs recover from crashes, how retries/backoff work, the
security model): see ARCHITECTURE.md and FAILURES.md. Symptom-driven fixes:
TROUBLESHOOTING.md.
