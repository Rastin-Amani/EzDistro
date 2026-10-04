# SEO Research Intelligence

The research engine turns Google Ads demand data, your WordPress content and
competitor pages into an evidence-backed article roadmap. It is **facts first,
AI second**: the model interprets real data, it never invents metrics.

```text
Documentation status:  Verified against app/services/research*.py, providers, schema
Last verified:         2026-10-04
Companion documents:   ARCHITECTURE.md §6.7–6.8 · SCHEMA.md §3.22–3.34 ·
                       CONFIGURATION.md · FAILURES.md
Also served in-app at  /help/SEO_RESEARCH.md (auth-gated)
```

## What it does

1. Connects a Google Ads account (OAuth) and picks a customer/account.
2. Collects keyword ideas and metrics from Google Ads Keyword Planning.
3. Mirrors your existing WordPress posts into the normal **Articles** section.
4. Crawls competitor sites (sitemap-first, robots-respecting, cached).
5. Clusters keywords, finds content gaps and matches them against your content.
6. Reads a natural-language goal and generates `GENERATE` / `UPDATE` / `EXPAND` /
   `SUPPORT` / `REJECT` opportunities with a transparent score.
7. Hands accepted opportunities to the existing article generation and
   publishing pipeline — the research engine never writes or publishes itself.

The whole feature works with **no paid SERP provider** and **no LLM** (clustering
has a deterministic mode). Every optional capability degrades gracefully.

## Google Ads setup

### Access model (current)

Developer tokens were **sunset on 2026-09-09**. API access levels now belong to
the **Google Cloud project** that owns the OAuth client; there is no
end-user developer token to paste. Do not build a developer-token onboarding
workflow. The optional `developer_token` field on the Google Ads connection
(and the `GOOGLE_ADS_DEVELOPER_TOKEN` env fallback) still exists only for
backwards compatibility and is sent as a header *only if set*; Google ignores it.

### Where the OAuth client lives (Connections tab)

The OAuth **client** (client id, client secret, redirect URI) is configured
**per project** on the project's **Connections** tab (`?tab=integrations`),
exactly like every other provider in the app — because it may differ from
project to project.

Add a connection with **category = `Google Ads (keyword research)`** and fill
in:

| Field | Purpose |
| --- | --- |
| `client_id` | Google Cloud OAuth client id |
| `client_secret` | Google Cloud OAuth client secret (stored encrypted) |
| `redirect_uri` | Exact callback URL (see below) |
| `api_version` | Default `v25`; versions sunset annually, keep configurable |
| `login_customer_id` | Optional manager (MCC) id used as `login-customer-id` |
| `developer_token` | Optional legacy header, ignored by Google today |

The non-secret fields are stored in the integration's `configuration`; the
client secret and developer token are Fernet-encrypted into `secretsEnc` and
never leave the server. The project's research tab reads
`_credentials(pb, project_id)` ([`app/services/google_ads.py`](../app/services/google_ads.py)),
which resolves the enabled `google_ads` integration for that project.

> **Projects are self-contained by default.** Env `GOOGLE_ADS_*` is an optional
> **fallback** used only when a project has no enabled `google_ads` connection —
> useful for a single shared OAuth client across all projects. Configure it on
> the Connections tab unless you intentionally share one client everywhere.

If you do use the env fallback, set:

| Variable | Purpose |
| --- | --- |
| `GOOGLE_ADS_CLIENT_ID` | Google Cloud OAuth client id |
| `GOOGLE_ADS_CLIENT_SECRET` | Google Cloud OAuth client secret |
| `GOOGLE_ADS_REDIRECT_URI` | Exact callback URL (see below) |
| `GOOGLE_ADS_API_VERSION` | Default `v25`; versions sunset annually, keep configurable |
| `GOOGLE_ADS_LOGIN_CUSTOMER_ID` | Optional manager (MCC) id used as `login-customer-id` |
| `GOOGLE_ADS_DEVELOPER_TOKEN` | Optional legacy header, ignored by Google today |

In the Google Cloud Console add the OAuth scope
`https://www.googleapis.com/auth/adwords` and register the redirect URI exactly
as configured. **Two callback paths are served and are interchangeable:**

- `https://your-app.example.com/auth/google-ads/callback` — the path the app
  derives by default when no redirect URI is configured.
- `https://your-app.example.com/projects/google-ads/callback` — the original
  path, kept so an already-registered URI keeps working.

Register whichever you prefer and use the same exact string in the connection.
The redirect must match character-for-character (`redirect_uri_mismatch`
otherwise).

### OAuth flow

```
Connect Google Ads -> Google consent -> /auth/google-ads/callback
                                     (or /projects/google-ads/callback)
  -> exchange code -> store the refresh token ENCRYPTED -> list customers
  -> user picks an account -> connection verified
```

- The callback is a **browser navigation**, so it redirects back to the project
  page with a flag (`?ga=connected|unconfigured|denied|error`) and the page
  renders an inline alert — it does not use `HX-Trigger`.
- `state` is HMAC-signed and expires (see `app/services/google_ads.py`); it
  binds the flow to the user and project so one user can never attach another
  user's account.
- The refresh token is encrypted with the project's existing `SecretsService`
  (Fernet) into `google_ads_connections.refreshTokenEnc`. It is never returned
  in a response, rendered, logged or sent to analytics.
- A single connection can expose several customers. Adding/switching customers
  does not require re-consent while the credential grants access.

## Targeting

A research run records its targeting explicitly — `country`, `language`,
`locale`, `network` (`GOOGLE_SEARCH` is the default for organic research) and
`includeAdultKeywords`. US/UK/Canada are never silently mixed: language and
geo-target constants are resolved from real Google Ads ids via GAQL, and an
unresolvable language/country raises a human-readable error instead of
mis-targeting.

The same keyword can legitimately carry different metrics per location, so
metrics are stored per run/targeting (see `keyword_metrics`, `keywords`).

## Seeds and research scope

Supported seed types (`research_seeds.seedType`): `keyword`, `url`, `site`,
`competitor`. Keyword seeds are batched (~20 per `GenerateKeywordIdeas` call);
URL/site/competitor seeds each become their own request. The per-run budget is
shared across batches (`RESEARCH_MAX_KEYWORDS_PER_RUN`, default 5000).

### File import (no Google Ads API access)

If a user cannot get Google Ads API access, they can export keyword ideas from
**Google Keyword Planner** (CSV or XLSX) and upload the file in the Research tab.
This creates a research run with `researchType = "import"` and the file stored
base64-encoded in `config.importPayload`; the run needs **no** Google Ads
connection or customer.

- `app/services/research_import.py::parse_keyword_file(data, filename, mapping=None)`
  parses the file with the standard library only (`csv`; XLSX via `zipfile` +
  `xml.etree`, so **no new dependency**). It detects columns by alias (Keyword /
  Avg. monthly searches / Competition / Competition (indexed value) / Avg. CPC /
  bid ranges / Currency), tolerates preamble rows, handles `1,234` and `1.2K`
  style numbers, and de-duplicates case-insensitively keeping the larger volume.
- The parsed rows are persisted by the **existing**
  `research_keywords._persist(...)` with `source="keyword_planner_import"`, so the
  API path and the import path share clustering, gaps, opportunities and the
  topic handoff. Google metrics are never invented or estimated — only
  classified/summarised by AI later.
- `POST /projects/{id}/research/import/preview` returns the detected columns and
  keyword count (and a small preview table) so the user can confirm before
  starting. `POST /projects/{id}/research/import` starts the run.
- If no keyword column can be detected, the preview says so and the route accepts
  a manual `mapping_column` (column index) rather than failing.

## Caching, idempotency and resumability

- **Run-level cache.** `ResearchRunRepo.fingerprint(targeting, seeds)` hashes
  the targeting plus the sorted, normalised seed set (project deliberately
  excluded). Starting research with an identical fingerprint reuses the previous
  run unless the user ticks **Refresh**. This is cheap and correct but coarse:
  provenance is run-level, not per-seed (there is no keyword↔seed join table).
- **Stage-level resume.** Each of the nine stages writes a summary into
  `research_runs.stageState`. A re-run (or a worker restart) skips any stage
  whose summary already exists, so an interrupted run never re-pays Google Ads.
- **Idempotent writes.** Keywords are keyed by
  `(project, normalizedKeyword, language, locationId)`; metrics by
  `(run, keyword)`; volumes by `(run, keyword, year, month)`; clusters by
  run+slug. Re-running any stage does not duplicate rows.
- The job is queued as `research_run` with `idempotencyKey=research_run:{runId}`,
  so double submission cannot create two jobs.

## WordPress synchronisation

`app/services/wordpress_sync.py` mirrors WordPress posts into the existing
`articles` collection (with an auto-created topic per post, because
`articles.topicId` is required). This means existing WordPress content appears
in the **same Articles section** as EZDistro-generated drafts, badged by source.

- **Remote identity** is `(project, wordpressPostId)` — never title, slug or URL,
  all of which change. A slug change updates the existing row.
- A cheap paged inventory (`id, status, modified`) runs first; full content is
  fetched only for new posts or posts whose remote `modified` differs from the
  stored `remoteMeta.modified` (or when `force`).
- **Local edits are never overwritten.** If a mirrored article is in a local
  work state and the remote hash differs, it is flagged
  `syncStatus='update_available'`.
- Remote deletions are flagged `remoteStatus='deleted'` /
  `syncStatus='remote_deleted'`; local research history is preserved.
- Changed published posts are handed to the **existing** indexing pipeline
  (`index_project` for a big batch, `index_document` per post otherwise).
  Content hashes prevent re-embedding unchanged posts.

WordPress Application Passwords are sent over HTTPS only and stored server-side;
they are never exposed to the browser after saving.

## Optional SERP provider

`SERP_PROVIDER=none` is a **first-class state**, not an error. Without a SERP
provider the engine still produces keyword discovery, search demand, historical
trends, paid competition, CPC ranges, competitor analysis, content gaps,
clusters and opportunities. Ranking positions, People Also Ask, featured
snippets and SERP features simply show as *unavailable* — they are never
estimated.

To enable SERP data add a `serp` integration in the project's **Connections**
tab (provider `serper`, with an API key). Observations are cached
(`serp_queries`/`serp_results`) with a TTL; a single keyword can be refreshed on
demand from the keyword drawer. `RESEARCH_MAX_SERP_QUERIES` (default 0) caps how
many live queries one run may spend.

## AI connections

The research engine reuses the project's AI connections and adds **dynamic model
discovery**: when a Base URL and API key are configured, the available models
are fetched from the provider's model-list endpoint (OpenAI-compatible `/models`,
OpenRouter's models API). A manually entered model id is always accepted as a
fallback, so a provider without a listing endpoint still works. Capabilities
(chat / structured output / decision / embedding) are recorded where
discoverable and left `unknown` otherwise.

Decision-style models (e.g. Jev) are treated as a separate **decision**
capability rather than ordinary chat. If a requested decision model is not
exposed by the connection, the UI says so instead of silently sending a
decision prompt through a chat-only endpoint. Jev is **optional**: research runs
with deterministic clustering, existing embeddings, or any configured LLM.

Cost is split by task: deterministic code for normalisation/dedupe/hashing,
a cheap model for intent/action classification, a strong model for final
opportunity synthesis.

## Clustering

Three modes, selectable per run (`config.clustering`):

| Mode | Behaviour |
| --- | --- |
| `auto` | embedding when the project has an embedding provider, else lexical |
| `deterministic` | pure lexical grouping (`app/domain/keywords.py`), zero AI |
| `embedding` | reuses the project's embedding provider |
| `llm` | batched AI clustering with the `cluster_system`/`cluster_user` prompts |

Deterministic clustering buckets by head term with an inverted token index and
an intent "gate" (superlatives are neutral, so *gym software* and *best gym
software* stay together while a pricing/cost query does not), giving O(n·k)
behaviour rather than an n×n similarity matrix. LLM clustering degrades to
deterministic per failing batch, so one bad response cannot break a run.

## Opportunity engine

The user describes a goal in natural language (Step 12). `parse_goal` builds a
structured representation (objectives, priority intents, audience, min/max
volume, excluded topics, max opportunities) with a deterministic heuristic, then
the engine applies it: a lead-gen goal raises the intent bar toward
commercial/comparison, an authority goal favours pillar/supporting content, an
update goal favours gaps/freshness.

Generation is **batched** (a handful of clusters per LLM call, never one giant
"give me 10,000 articles" prompt) and iterative. Every candidate is validated:

- the primary keyword must resolve to a keyword **that was actually collected**;
- the title must not be excluded by the goal;
- near-duplicate concepts are merged, not persisted twice;
- a candidate that matches an existing article is forced to `UPDATE` (a
  different title does not justify a new URL).

The opportunity score is transparent and versioned (`scoreVersion='opp_v1'`,
weights in `SCORE_WEIGHTS`): demand, business relevance, content gap, existing
coverage and uniqueness. When SERP data is absent the SERP component is simply
excluded — `scoreComponents.serp` is `null` and the UI says so.

## Handoff to articles

`accept_opportunity` (in `app/services/research_handoff.py`):

- `update` / `expand` attach a brief to the existing article — no new URL;
- `generate` / `support` create a topic (left at status `planned` so the
  existing writer runs) + an article, then queue the **existing** `write_article`
  job with `idempotencyKey=write_article:{articleId}` (re-accepting is a no-op).

Publishing, auto-publishing rules and scheduling are untouched: the research
engine feeds the current pipeline, it does not replace it.

## Cost, retention and caps

| Setting | Code default | Meaning |
| --- | --- | --- |
| `RESEARCH_MAX_KEYWORDS_PER_RUN` | `0` (uncapped) | Google Ads budget per run (`.env.example` recommends `5000`) |
| `RESEARCH_MAX_COMPETITOR_PAGES` | `0` (uncapped) | crawl budget per run (`.env.example` recommends `200`) |
| `RESEARCH_MAX_SERP_QUERIES` | `0` | live SERP budget (0 = none) |
| `RESEARCH_CRAWL_CONCURRENCY` | `4` | crawl parallelism |
| `RESEARCH_GOOGLE_ADS_CONCURRENCY` | `2` | Keyword Planning parallelism (rate-limited) |

> The keyword/crawl caps default to **no cap** in code; the `5000`/`200` values are the
> recommended production caps shown commented in `.env.example`.

Google Ads keyword-planning responses are stable within a month, so the
fingerprint cache is the primary saving. Keep normalised strategic data
(metrics, clusters, gaps, ideas); raw temporary payloads (SERP raw, crawler
caches) can be pruned on a schedule.

## Terminology you must not get wrong

- **Google Ads competition / competition index is PAID competition, never
  organic difficulty.** It is labelled "paid comp." in the UI. Organic
  difficulty only exists when a SERP provider supplies ranking evidence.
- A metric is shown with its source: *measured* (Google Ads), *observed*
  (SERP, with a timestamp), *derived*, or *AI-classified*.
- The engine says *opportunity*, *potential*, *observed*, *estimated* — never
  "Google will rank this".

## Analytics

There is deliberately **no Umami/analytics layer** in EZDistro today, so the
research engine emits none. If analytics is added later, the intended events are
`seo_research_started/completed`, `google_ads_connected`, `wordpress_sync_*`,
`serp_provider_connected`, `clustering_completed`,
`article_opportunity_generated/accepted/rejected`. Never send API keys, tokens,
private prompts or article content to analytics.

## Troubleshooting

| Symptom | Cause / fix |
| --- | --- |
| "Google Ads is not configured for this project" | Add a Google Ads connection on the project's **Connections** tab (category `Google Ads (keyword research)`) with the Cloud OAuth client id, client secret and redirect URI. Env `GOOGLE_ADS_*` only acts as a fallback. |
| `redirect_uri_mismatch` | The redirect URI must match the Google Cloud registration exactly (scheme, host, path). |
| "Google Ads does not recognise the language '…'" | The language code is not in Google Ads' language constants; use an ISO code like `en`, `fa`, `es`. |
| Keyword research produces nothing | Check the run's targeting, the chosen customer, and that the account allows Keyword Planning for the Cloud project. Errors are humanised in the run page; the technical error is in the job events. |
| SERP tab shows "not configured" | Expected: add a `serp` integration, or keep working in Google Ads + site intelligence mode. |
| WordPress posts missing from Articles | Run **Refresh accounts**/re-run the research; sync is paged and incremental. Check the WordPress connection health in Connections. |
| A mirrored post shows `update_available` | The remote content changed while you had local work in progress — the engine refuses to overwrite local edits. |
| Research run stuck in `running` | The worker must be running (`make worker`); runs are resumable, so a restart continues from the last completed stage. |
| Provider rate limits | Keyword Planning has tight limits; `RESEARCH_GOOGLE_ADS_CONCURRENCY` defaults to 2. Retries use exponential backoff with jitter and respect `retry_after`. |
