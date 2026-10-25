# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Users

Primary: indie hacker / solo developer running SEO + AIO + GEO for their own sites with AI automation; secondary: small SEO team (manager + human operators) running many sites/projects in parallel. English-only interface. Operators live in the pipeline daily: topics → outline → sections → images → publish → index health.

## Product Purpose

EzDistro automates SEO content operations end-to-end: Research (free autocomplete harvest, Google Ads Keyword Planner data or keyword-file import → WordPress mirror → competitor crawl → clusters → gaps → scored opportunities), Indexer (fetch WordPress posts → chunk → embed → Qdrant), Writer (topics → retrieved context → validated LLM outline → per-section generation → sanitized HTML → publish back to WordPress, draft or live, idempotent) and Link building (older published pages get one contextual inbound link to each new article). Landing-page analysis turns the brand's own page into the product context every prompt reads. Facts first, AI second: the research engine never invents volume, CPC, competition or rankings. Success = a solo operator ships publish-ready, indexed articles across many projects with minimal clicks, and sees pipeline + provider health at a glance.

## Positioning

Modular monolith with no Redis/Celery/n8n: FastAPI + HTMX + DaisyUI front, PocketBase as source of truth (39 collections: 20 core + 13 research + 6 billing), Qdrant vectors, pluggable LLM/embedding/rerank/image/SERP/keyword-source providers, standalone asyncio worker with atomic lease claiming. Everything (WP config, prompts, SEO rules, schedules, concurrency, research targeting) is per-project and UI-editable. Optional Google Ads OAuth client is configured per project on the Connections tab (env is a fallback). Billing is optional and off by default; self-hosted deployments with their own provider keys are unmetered.

## Operating Context

Daily loop: add a website → **Website setup** / **Connections** (WP, LLM, embedding, Qdrant, reranker credentials, Fernet-encrypted) / **Writing rules** → **Content plan** (bulk manage, **Write**) → article workspace (3-pane: outline nav / section editor / meta+checks+links+publish history) → review gate (**Checks** + **Editorial QA** → **Publish to WordPress**) → **Activity** monitoring → **Automation** routines + **Site health** re-index. Long-running work lives in job handlers; web only creates/monitors jobs. PWA, HTMX mutations gated by require_hx, Gregorian dates through the `loc_*` filters.

## Capabilities and Constraints

Must preserve: all routes/HTMX/Alpine behavior, the English-only UI, light + dark DaisyUI themes, PWA shell, sidebar + mobile dock navigation (Overview, Websites, Activity, Automation, Workers, Events, Plan & billing), project tabs (Overview, Find opportunities, Content plan, Articles, Published / Website setup, Advanced → Connections, AI models, Writing rules, Article images, Search settings, Site health, Technical task history, Updates), article workspace 3-pane, toasts/confirm-dialog, only-DaisyUI constraint (no new deps). Attached design.md is binding visual direction (Base blueprint: white paper, ink, rationed electric blue) translated to both themes. Mobile must be spectacular; layouts scalable.

## Brand Commitments

Name: EzDistro Platform. Incumbent logo mark: single-letter block. Attached design.md pins: light paper canvas, carbon ink, electric blue #0000ff rationed to logo block + primary action + data points, wireframe green #098551 for growth nodes, mono uppercase labels with wide tracking, 4px grid, 8px radii, flat surfaces (the mobile dock is the single elevated element), hairline dividers.

## Evidence on Hand

Live codebase (FastAPI/Jinja2/HTMX/DaisyUI5/Tailwind4), docs/ARCHITECTURE.md + docs/SCHEMA.md, attached design.md tokens in request, real pipeline data via PocketBase (no synthetic marketing claims allowed in UI).

## Product Principles

1. Pipeline legibility beats decoration: state, progress, and next action always visible in seconds.
2. One operator, many projects: every surface scales from 1 to N projects without new chrome.
3. Trust through honesty: encrypted secrets masked, idempotent publishes, cancellable jobs, real timestamps.
4. Mobile-first: thumb-zone actions, safe-area aware, tabular numerals, Gregorian dates.
5. Flat paper craft: hierarchy from type scale + hairlines + spacing; one elevation (the mobile dock), no gradients.

## Accessibility & Inclusion

Keyboard focus visible, English-only interface (LTR; content language is per project), reduced-motion respected (single orchestrated entrance), touch targets ≥32px buttons / 44px fields, contrast ≥4.5:1 body.
