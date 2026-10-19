# Changelog

All notable changes to EzDistro Platform. Versions follow the repository's git
tags (`git tag`); see [docs/](docs/README.md) for the architecture that each
release builds on. Format is based on [Keep a Changelog](https://keepachangelog.com/).

> `APP_VERSION` in `app/config.py` (`0.2.0`) is an independent, env-overridable
> app label and does not track these release tags.

## [Unreleased]

## [v1.23.0] — 2026-10-19

### Added
- **Auto-publishing schedule.** A per-project `publish` schedule drains the ready
  queue one article per interval (oldest first, at or above the auto-publish
  score floor), then indexes it and builds its inbound links. Configure it beside
  Auto-indexing and Auto-writing on the project's Settings tab; it is inert
  unless **Auto-publish** is enabled.
- **Inbound internal link building.** After an article is published, the pages
  most related to it (from the retrieval store) each receive one contextual link
  back to it. The insertion is text-preserving — it only wraps an existing
  keyword or title occurrence, never nests an anchor, and does not use an LLM —
  and each edited page is re-indexed.
- A bulk **Reset to planned** action returns failed or cancelled topics to the
  write pool without queueing anything, and the Topics tab now links to the bulk
  actions page.

### Fixed
- Article assembly no longer stalls forever when a pending section's
  `generate_section` job has vanished (cancelled or lost with its worker): the
  article, its topic and the billing reservation now fail deterministically
  instead of sitting at "generating".
- A cancelled topic is no longer written: `cancelled` is out of the
  in-write-pipeline states, so a queued write stops instead of overriding the
  cancellation.
- Publishing waits while the article's image jobs are still queued, instead of
  racing ahead and shipping a post with no images.
- Self-replacing HTMX containers no longer wrap links: the refresh trigger is a
  hidden sibling, so the article back button, the dashboard's project links and
  the workers job-queue links navigate instead of blanking the page (inherited
  `hx-target`/`hx-select`).

### Changed
- Accepting a research opportunity queues a write job only when the project has
  no enabled write schedule; with one, the schedule owns the cadence.

## [v1.22.0] — 2026-10-18

### Added
- Starting, importing or resuming research now swaps the content region in place
  instead of reloading the whole page, keeping the sidebar and shell alive.
- The article detail, review and workspace pages share one header macro, so the
  title, status, counts and actions sit in the same place on all three.

### Changed
- The interface no longer carries a bespoke `.ezdistro-*` styling vocabulary:
  every surface uses DaisyUI primitives and Tailwind utilities inline, and
  `input.css` is reduced to the themes, base accessibility, the htmx indicator
  and two layer-order fixes.
- Inter is served as WOFF2 (352 KB) with the TTF as fallback.
- The dashboard drops its static flow strip and header action; the writing flow
  stays on the workspace/review progress strips.

### Performance
- All PocketBase clients share one warm, bounded connection pool instead of
  opening a fresh one per client; bulk creates and deletes go through
  `/api/batch` (50 per request) with a per-row fallback.
- Job claiming, heartbeat, release and retry paths re-read far less: the lease id
  returned by `acquire` is reused, the claim record is handed to the run, the
  cancel flag is memoized, and a retry is one write.
- The dashboard and `/workers` share one TTL-cached queue-count computation, the
  dashboard reads only the columns it renders, and the indexing tab merges its
  metric counts into the existing fanout wave.
- Templates compile once at boot, asset URLs are content-hashed from the newest
  css/js file, and `/static` responses carry `Cache-Control`.
- `qdrant_client` is imported lazily, removing its multi-second import from the
  first request that resolves any provider.

## [v1.21.0] — 2026-10-18

### Added
- **Free, keyless keyword harvest.** Paste seed keywords and expand them through
  the public autocomplete endpoints (Google, YouTube, Bing, DuckDuckGo, Amazon) —
  no account, key or quota — then cluster them and propose article
  opportunities. It reports no volume, competition or CPC by design; Keyword
  Planner is the documented way to fill them in.
- Export a run's keywords as CSV (**Keywords CSV**) for a Keyword Planner
  volume round trip, alongside the roadmap export.

### Changed
- Research clustering splits a cluster larger than 15 keywords at a tighter
  cosine, so a large file becomes many pages instead of a few.
- Opportunity generation runs two clusters per call with up to 40 keywords each,
  reads the imported monthly volume trend, and resolves sibling-cluster
  keywords instead of dropping them.
- The `meta` / `review` roles accept temperature, max tokens, timeout and
  retries like `outline` / `section`.
- The container entrypoint runs the web process plus a `WORKERS` pool, giving
  each slot a stable id (`worker-1…N`).

### Fixed
- A redeploy no longer leaves outdated workers behind: the container entrypoint
  gives every worker slot a stable id (`worker-1…N`), so the new process
  overwrites its own `worker_heartbeats` row instead of adding a second one to
  the Workers page, and it forwards `SIGTERM` to the pool so workers are asked
  to stop instead of being killed with their in-flight jobs still locked.
- A worker stopped mid-job hands that job straight back to the queue
  (`job.requeued`) instead of leaving it `running` until `LEASE_SECONDS`
  expires, so a deploy resumes work in seconds and its own version shows on
  `/workers` immediately.
- A research run's progress advances inside the long opportunity stage instead
  of freezing at 88%.
- Cross-batch merges persist: a candidate folded into an earlier batch's page
  now writes back what was merged and which keywords it carried, and stored
  keyword rows carry the id they were saved with.
- Opportunity generation no longer discards a keyword that belongs to a sibling
  cluster in the same batch; an exact collected keyword wins over a fuzzy match;
  and same-intent candidates ("gym crm", "best gym crm") merge into one page
  while a distinct format ("gym crm software pricing") stays its own.
- A pooled HTTP client is no longer closed while a job is still using it, and an
  empty length-truncated completion reports the model spent its token budget on
  reasoning instead of an unactionable "empty completion".

## [v1.20.0] — 2026-10-17

### Added
- Writing-flow guidance: a **Topics → Draft → Review → Publish** strip on the
  dashboard and a **Draft → Review → Publish** strip on the workspace and review
  pages, a **Continue writing** primary action on the project header, and a
  **Start here** label on the rail.
- Research now opens on the no-API path: upload a Keyword Planner export first,
  with Google Ads setup and the run form collapsed underneath.

### Changed
- Information architecture follows the writing loop: a project opens on its
  **Topics** tab and its tabs are grouped **Write → Research → Setup → System**;
  the global rail is **Workspace / Operations / Account**, the top-level
  **Failed jobs** shortcut moves behind **Jobs**, and the mobile dock is trimmed
  to four items.
- The component layer is DaisyUI-native: `input.css` now holds only the theme,
  base accessibility and the shell/rail/dock/subnav layout helpers — the
  hand-rolled button/card/input/table/badge/stat/alert/modal overrides and the
  dead `.ezdistro-bars`/`.ezdistro-rule`/tab-pill motifs are removed.

### Fixed
- The article workspace no longer repeats section status (the outline is the
  single source), shows Validation only when invalid, and collapses **Publish
  history** and **Version history** behind `<details>`.

### Documentation
- Updated `DESIGN.md`, `docs/USER-GUIDE.md` and the README to the new component
  layer and navigation.

## [v1.19.1] — 2026-10-16

### Fixed
- Polling is no longer destructive on tabbed pages: the polled containers lead
  with their swap root, so the tab section headers are replaced in place instead
  of duplicated on every tick; the job **Timeline** poll now swaps only the
  event fragment, keeping its panel wrapper.
- A portless `https://` Qdrant URL no longer dials the REST default port 6333:
  the client pins 443/80 by scheme, so reverse-proxied Qdrant URLs connect
  instead of timing out while the health ping reports healthy.
- The billing pages use the app's standard panel spacing.

### Changed
- `PB_URL` now defaults to `http://127.0.0.1:8090` (and the commented
  `QDRANT_URL` example to the code's local fallback) instead of hardcoding the
  maintainer's hosted hosts; deployments keep overriding them from `.env`.

### Testing
- The test suite forces billing off by default, so a local `BILLING_ENABLED=true`
  no longer changes test behaviour.

## [v1.19.0] — 2026-10-16

### Added
- **Billing & payments.** Optional per-user metering of article generation: a free
  allowance first, then purchased credits. Credit packs are paid in crypto through
  **PayRam**; billing is off by default (`BILLING_ENABLED=false`).
- Five PocketBase collections (`billing_products`, `billing_invoices`,
  `billing_payment_events`, `billing_usage`, `credit_transactions`) plus an
  append-only credit ledger, seeded with three default credit packs.
- A **Billing** page (balance, credit packs, payment history, invoice detail) and
  a public, signature-verified PayRam webhook (`POST /webhooks/payram`).
- `money_cents` / `money_usd` template filters and billing status badges.

### Changed
- The `write_article` pipeline reserves a generation when queued and releases it on
  failure/cancel; queuing a topic is blocked with a paywall message when a user has
  neither free allowance nor credits (scheduled jobs are billed to the project owner).

### Documentation
- Added [`docs/BILLING.md`](docs/BILLING.md); refreshed the schema/architecture
  collection counts to 38.

## [v1.18.0] — 2026-10-13

### Added
- Filter **Failed jobs** by project, stage and reason.
- Connections editor: choosing a category resets the provider/model/base URL to
  that category's defaults, and **Test** refreshes the row's health badge
  out-of-band.

### Changed
- Redesigned the shell and project navigation: grouped sidebar, a grouped vertical
  project rail (horizontal strip on small screens), a tighter mobile dock, compact
  tab section headers, collapsible settings groups, and a one-prompt-at-a-time
  editor.
- Projects no longer carry `language`, `timezone` or `description`: market and
  locale live in project settings (`targetLocale`/`targetCountry`, default `US` /
  `en-US`) and the writing language derives from the locale. Existing databases
  are migrated with `python -m app.scripts.drop_legacy_project_fields`.
- The **Events** feed retries a transient PocketBase read and degrades to an inline,
  retryable panel instead of a dead-end error page.
- Adopted the Apache-2.0 license and corrected package metadata.

### Fixed
- The **Jobs** monitor keeps the selected filter values when a filter is applied.
- The service worker never caches a degraded/error page; error pages send
  `Cache-Control: no-store`.
- The Events page template (`app/templates/pages/logs/feed.html`) is tracked again
  — a broad `logs/` `.gitignore` rule had excluded it from the repository.
- WordPress sync fetches pending posts in bounded parallel batches.

### Performance
- The worker's job poll loop backs off while idle (up to 5× the interval).
- Static assets are cache-busted with the app version.

## [v1.17.1] — 2026-10-05

### Fixed
- Analyse the typed landing-page URL, stop caching the status poll, and round the
  brand tile with the box radius.

## [v1.17.0] — 2026-10-04

### Added
- Analyse the landing page into a stored product profile, and register the
  corresponding job type.
- Emit Gutenberg blocks when publishing to WordPress; resume a failed article from
  its last failed step.
- Pick integration providers from the Connections tab.
- Adopt the EzDistro brand asset kit.

### Changed
- Make the content pipeline English-only.
- Overlap read-only PocketBase reads; open the worker's job loop to the new
  landing-page job.

### Fixed
- Preserve system field patterns when bootstrapping the `users` collection.
- Match duplicate topic titles case-insensitively and filter log levels by their
  event types.
- Scope tab polling, serve `docs/`, and harden actions.

### Documentation
- Brought the whole `docs/` set up to date for the SEO research engine: 33
  collections, 16 job types, 25 prompt types, the research/WordPress-sync
  pipelines, Google Ads + SERP providers, and the corrected research-cap defaults.
- Added `CONTRIBUTING.md`, `CHANGELOG.md` and `SECURITY.md`.

## [v1.16.0] — 2026-10-03

### Added
- Bulk actions on many clusters/opportunities; filters on every research tab.
- Plain-language surfacing of PocketBase validation errors.

### Changed
- Research pipeline scales, reports progress and tolerates partial failure;
  repository reads scale and bulk writes are idempotent.

## [v1.15.1] — 2026-10-03

### Fixed
- Deterministic opportunity ideas when no meta LLM is configured.
- Chunk OR-filters by character budget; read-only probes are non-fatal.

## [v1.15.0] — 2026-10-02

### Added
- Import a Google Keyword Planner CSV/XLSX file from the Research tab and run
  the pipeline from it (stdlib-only parser, no Google Ads API access required).

## [v1.14.6] — 2026-10-02

### Fixed
- Actionable Google Ads auth failures; keep customers whose metadata probe is
  denied; normalise the connection to a list for the tab; read the OAuth state
  with its real keys; land the OAuth outcome on a valid URL; serve the OAuth
  callback on the `/auth` path.

### Changed
- Connections: fetch model options from a saved connection; default the theme to
  the system preference; align `.form-control` with daisyUI 5.
- Bootstrap unions new select values into live PocketBase collections.

## [v1.13.0] — 2026-10-01

### Added
- Per-project Google Ads OAuth client editor and setup flow.

### Changed
- UI polish: 8px radius sweep, grouped project forms, formatted raw counts,
  removal of the last Persian/RTL remnants.

## [v1.12.0] — 2026-10-01

### Fixed
- PocketBase import mirror is generated from the collections; full-list query
  params pass as keyword args.

## [v1.11.0] — 2026-10-01

### Added
- SEO research engine: research collections + WordPress-mirror fields, Google Ads
  and SERP provider adapters, keyword/competitor/cluster/SERP collection,
  opportunity engine with handoff to the writer, WordPress sync, the research
  workspace, and background `research_run`/`wordpress_sync` jobs.

## [v1.10.1] — 2026-09-29

### Fixed
- Anchor the mobile header menus to the bar and clamp overflow.

## [v1.10.0] — 2026-09-29

### Added
- EzDistro brand mark; aligned dark palette.

### Fixed
- Allow-list the events level filter and guard the logs page.

## [v1.9.0] — 2026-09-29

### Changed
- **Breaking:** removed the gettext layer; the interface is English-only.
- Rewrote `.env.example` around the production deployment.
- Locked PocketBase collections with `null` rules (not empty strings).

## [v1.8.0] — 2026-09-29

### Added
- Multilingual engine: research / metadata / QA / repair writing stages, seeded
  multilingual prompt defaults, per-project locale/brand settings, and a
  one-container web+worker deployment.

## [v1.7.0] — 2026-09-29

### Added
- The Atlas theme and streamlined navigation; mobile content workflows.

### Fixed
- Bound PocketBase client calls with an explicit timeout; prevent duplicate
  send-back submissions.

## [v1.5.0] — 2026-09-27

### Added
- Signal design system; English as default interface language.

### Changed
- Rebranded Seoz → EzDistro; default PocketBase URL now `ezdistro.space`.

## [v1.4.0] — 2026-09-25

### Added
- Team-features PRD (draft, unimplemented — see `docs/TEAM-FEATURES-PRD.md`).

### Security
- Split session validation from superuser data access; locked PocketBase API
  rules to superuser-only.

## [v1.3.0] — 2026-08-31

### Added
- Image pipeline: image-generation providers (`gemini` / `bfl` FLUX /
  `openai_compat`), image plan domain + schema, image job handlers with
  optimization and publish integration, workspace images pane, project Images
  settings tab with a one-click generation test, and automatic planning after
  article generation.

## [v1.2.0] — 2026-08-30

### Added
- i18n locale flags and popover switchers (later superseded by the English-only
  decision in v1.9.0).

### Fixed
- Service worker serves static assets network-first.

## [v1.1.1] — 2026-08-30

### Changed
- Performance: lazy-import Qdrant, cache `ProjectConfig`, carry the active prompt
  version through `resolve_all`, reuse the bleach Cleaner.

### Fixed
- Persist `autoPublish` settings; make bootstrap idempotent.

## [v1.1.0] — 2026-08-29

### Added
- Worker liveness (`worker_heartbeats`) + operational workers dashboard.
- Scheduler heartbeat and isolated schedule firing; systemd units + install
  targets; deterministic SEO enforcement and auto-publish.

## [v1.0.1] — 2026-08-28

### Added
- Bulk/CSV topic import with editorial week/url fields; instant reindex of live
  WordPress posts after publishing.

### Changed
- Migrated collection definitions to the PocketBase 0.23 field shape; docs became
  tracked (un-ignored).

## [v1.0.0] — 2026-08-22

### Added
- First tagged release: project workspace with interactive tabs, editable
  connections, job engine, writer/indexer pipelines, publishing with orphan
  recovery, and Garden light/dark themes (Persian toasts, later removed).

[v1.16.0]: https://github.com/Rastin-Amani/EzDistro/releases/tag/v1.16.0
[v1.15.1]: https://github.com/Rastin-Amani/EzDistro/releases/tag/v1.15.1
[v1.15.0]: https://github.com/Rastin-Amani/EzDistro/releases/tag/v1.15.0
[v1.14.6]: https://github.com/Rastin-Amani/EzDistro/releases/tag/v1.14.6
[v1.13.0]: https://github.com/Rastin-Amani/EzDistro/releases/tag/v1.13.0
[v1.12.0]: https://github.com/Rastin-Amani/EzDistro/releases/tag/v1.12.0
[v1.11.0]: https://github.com/Rastin-Amani/EzDistro/releases/tag/v1.11.0
[v1.10.1]: https://github.com/Rastin-Amani/EzDistro/releases/tag/v1.10.1
[v1.10.0]: https://github.com/Rastin-Amani/EzDistro/releases/tag/v1.10.0
[v1.9.0]: https://github.com/Rastin-Amani/EzDistro/releases/tag/v1.9.0
[v1.8.0]: https://github.com/Rastin-Amani/EzDistro/releases/tag/v1.8.0
[v1.7.0]: https://github.com/Rastin-Amani/EzDistro/releases/tag/v1.7.0
[v1.5.0]: https://github.com/Rastin-Amani/EzDistro/releases/tag/v1.5.0
[v1.4.0]: https://github.com/Rastin-Amani/EzDistro/releases/tag/v1.4.0
[v1.3.0]: https://github.com/Rastin-Amani/EzDistro/releases/tag/v1.3.0
[v1.2.0]: https://github.com/Rastin-Amani/EzDistro/releases/tag/v1.2.0
[v1.1.1]: https://github.com/Rastin-Amani/EzDistro/releases/tag/v1.1.1
[v1.1.0]: https://github.com/Rastin-Amani/EzDistro/releases/tag/v1.1.0
[v1.0.1]: https://github.com/Rastin-Amani/EzDistro/releases/tag/v1.0.1
[v1.0.0]: https://github.com/Rastin-Amani/EzDistro/releases/tag/v1.0.0
