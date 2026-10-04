---
version: 1
slug: "app-templates-layouts-platform-html"
primary_target: "app/templates/layouts/platform.html"
related_targets: []
---

# Surface brief — App shell + Dashboard (EzDistro redesign)

Scope: full-app visual world replacement (shell, tokens, dashboard, shared components). Mode: Operate.

Audience / job / action: indie hacker / small SEO team (manager + operators), Persian RTL first. Daily job: see pipeline state (topics → writing → images → publishing → indexed) + provider/worker health, then act (write article, retry failed, open workspace). Success in seconds: what needs attention, what is live, what to do next.

Proof / content: real PocketBase data only — pipeline counts, rail stages, job tables, event feed, integration health. No synthetic claims.

Constraints: preserve all routes/HTMX/Alpine/i18n/PWA behavior; DaisyUI only, no new deps; light + dark themes; mobile spectacular (thumb-zone dock); flat, no shadows/gradients; Persian strings stay Persian; 44px fields / 32px buttons; tabular numerals; Jalali dates.

## Direction contract

THESIS: The SEO pipeline as a wireframe atlas on white paper — hairline bars form the terrain, one electric blue marks every live action. Refuses the lavender-soft SaaS card grid and the hero-metric template; the rail itself is the hero.

OWN-WORLD: Paper canvas (#fff, dark: #0a0a0f ink), carbon ink text, slate prose, mist hairlines, electric blue #0000ff rationed to logo block + primary action + live nodes, wireframe green #098551 for growth/indexed nodes. Doto/Space-Grotesk-scale display for page titles (clamped, never overflowing), mono uppercase labels at 0.073em tracking above every block, 4px grid, 8px radii everywhere, 2px tag radius. Sidebar 220px fixed rail with blue logo block + bottom Start panel; mobile: floating pill dock + bar-strip header.

STORY: Operator lands, reads the atlas rail (4 stages with threaded hairline + breathing live node), sees attention queue (failed/awaiting), acts via one blue primary per viewport. Deep tabs inherit the same grammar: mono label, display subhead, hairline tables, edge-status pills.

FIRST VIEWPORT: Dashboard — mono label "PIPELINE ATLAS" over clamped display title (e.g. "Pipeline Atlas") + slate subhead right-aligned (RTL mirror of two-column header); below, full-width rail track (4 cells split by hairlines, node row threaded by rule, counts at 32px black, micro bars); then attention queue + health panels. Primary action (＋ Write / New project) is the single blue filled button. Sidebar rail left (RTL: right), dock pill on mobile.

FORM: Grounded candidate #3 of 7 (Base wireframe atlas — the brief-pinned world; ordered list: 1 Search-console workbench, 2 WP publishing desk, 3 Base atlas, 4 terminal log console, 5 Persian newsroom desk, 6 vector embedding atlas, 7 assembly-line rail). Seed key cdc68a01. Raises from challengers: transit-line discipline (pipeline as stations with interchange dots); ghost-cell discipline (unlit track + tabular numerals for idle/empty states); card-as-addressable-object (every row deep-links); hairline band-edge status (2px edge ticks, never left-border blocks).

FINISH: unreviewed and undocumented is unfinished; this build ends with the finish review, the verdict, DESIGN.md, and every shipping raster carrying its provenance
