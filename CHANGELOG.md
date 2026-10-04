# Changelog

All notable changes to EzDistro Platform. Versions follow the repository's git
tags (`git tag`); see [docs/](docs/README.md) for the architecture that each
release builds on. Format is based on [Keep a Changelog](https://keepachangelog.com/).

> `APP_VERSION` in `app/config.py` (`0.2.0`) is an independent, env-overridable
> app label and does not track these release tags.

## [Unreleased]

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
