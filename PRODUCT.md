# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Users

Primary: indie hacker / solo developer running SEO + AIO + GEO for their own sites with AI automation; secondary: small SEO team (manager + human operators) running many sites/projects in parallel. Persian-speaking first (fa RTL source language), English LTR second. Operators live in the pipeline daily: topics → outline → sections → images → publish → index health.

## Product Purpose

EzDistro automates SEO content operations end-to-end: Indexer (fetch WordPress posts → chunk → embed → Qdrant) plus Writer (topics → retrieved context → validated LLM outline → per-section generation → sanitized HTML → publish back to WordPress, draft or live, idempotent). Success = a solo operator ships publish-ready, indexed articles across many projects with minimal clicks, and sees pipeline + provider health at a glance.

## Positioning

Modular monolith with no Redis/Celery/n8n: FastAPI + HTMX + DaisyUI front, PocketBase as source of truth (20 collections), Qdrant vectors, pluggable LLM/embedding/rerank/image providers, standalone asyncio worker with atomic lease claiming. Everything (WP config, prompts, SEO rules, schedules, concurrency) is per-project and UI-editable.

## Operating Context

Daily loop: create project → Integrations tab (WP, LLM, embedding, Qdrant, reranker credentials, Fernet-encrypted) → Settings/Prompts → Topics tab (bulk manage, Write article) → Articles workspace (3-pane: outline nav / section editor / metadata+SEO+links+status) → Jobs/failed/events monitoring → schedules + full re-index. Long-running work lives in job handlers; web only creates/monitors jobs. PWA, HTMX mutations gated by require_hx, Jalali dates for fa.

## Capabilities and Constraints

Must preserve: all routes/HTMX/Alpine behavior, i18n (fa RTL + en LTR), light + dark DaisyUI themes, PWA shell, sidebar + mobile dock navigation (Dashboard/Projects/Jobs/Workers/Failed/Events), project tabs (Articles/Topics/Indexing/Prompts/Integrations/Settings/Jobs/Logs/Images/Publishing/Retrieval/AI models), article workspace 3-pane, toasts/confirm-dialog, only-DaisyUI constraint (no new deps). Attached design.md is binding visual direction (Base blueprint: white paper, ink, rationed electric blue) translated to both themes. Mobile must be spectacular; layouts scalable.

## Brand Commitments

Name: EzDistro Platform. Incumbent logo mark: single-letter block. Attached design.md pins: light paper canvas, carbon ink, electric blue #0000ff rationed to logo block + primary action + data points, wireframe green #098551 for growth nodes, mono uppercase labels with wide tracking, 4px grid, 8px radii, flat (no shadows), hairline dividers. Persian UI strings stay Persian.

## Evidence on Hand

Live codebase (FastAPI/Jinja2/HTMX/DaisyUI5/Tailwind4), docs/ARCHITECTURE.md + docs/SCHEMA.md, attached design.md tokens in request, real pipeline data via PocketBase (no synthetic marketing claims allowed in UI).

## Product Principles

1. Pipeline legibility beats decoration: state, progress, and next action always visible in seconds.
2. One operator, many projects: every surface scales from 1 to N projects without new chrome.
3. Trust through honesty: encrypted secrets masked, idempotent publishes, cancellable jobs, real timestamps.
4. RTL-first, mobile-first: thumb-zone actions, safe-area aware, tabular numerals, Jalali dates.
5. Flat paper craft: hierarchy from type scale + hairlines + spacing, never shadows or gradients.

## Accessibility & Inclusion

Keyboard focus visible, Persian + English locales, reduced-motion respected (single orchestrated entrance), touch targets ≥32px buttons / 44px fields, contrast ≥4.5:1 body.
