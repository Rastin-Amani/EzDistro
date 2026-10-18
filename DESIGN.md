---
name: EzDistro Atlas
description: Wireframe-atlas operations UI — paper canvas, carbon ink, one rationed electric blue over DaisyUI.
colors:
  paper: "#ffffff"
  ash: "#f2f2f2"
  mist: "#b1b7c3"
  carbon: "#000000"
  graphite: "#323232"
  slate: "#717886"
  electric-blue: "#0000ff"
  wireframe-green: "#098551"
  ink-ground: "#0b0b10"
  blue-dark: "#3333ff"
  green-dark: "#3fce7d"
typography:
  display:
    fontFamily: "Inter, ui-sans-serif, system-ui, sans-serif"
    fontSize: "clamp(2.5rem, 4vw + 1.5rem, 5rem)"
    fontWeight: 800
    lineHeight: 0.95
    letterSpacing: "-0.04em"
  body:
    fontFamily: "Inter, ui-sans-serif, system-ui, sans-serif"
    fontSize: "1rem"
    fontWeight: 400
    lineHeight: 1.5
    letterSpacing: "-0.014em"
  mono-label:
    fontFamily: "JetBrains Mono, IBM Plex Mono, ui-monospace, Menlo, Consolas, monospace"
    fontSize: "0.72rem"
    fontWeight: 400
    lineHeight: 1.43
    letterSpacing: "0.073em"
  rail-chrome:
    fontSize: "0.65rem"
    fontWeight: 400
    letterSpacing: "0.073em"
  dock-label:
    fontSize: "0.6rem"
    fontWeight: 400
    letterSpacing: "0.02em"
  metric-count:
    fontSize: "2rem"
    fontWeight: 900
    lineHeight: 1
    letterSpacing: "-0.04em"
rounded:
  tag: "2px"
  surface: "8px"
  dock: "16px"
  dock-item: "12px"
  scrollbar-thumb: "9999px"
spacing:
  unit: "4px"
  section: "48px"
  card: "24px"
components:
  button-primary:
    backgroundColor: "{colors.electric-blue}"
    textColor: "{colors.paper}"
    rounded: "{rounded.surface}"
    padding: "12px 16px"
  button-ghost:
    backgroundColor: "{colors.paper}"
    textColor: "{colors.carbon}"
    rounded: "{rounded.surface}"
    padding: "12px 16px"
  logo-block:
    backgroundColor: "{colors.electric-blue}"
    textColor: "{colors.paper}"
    rounded: "0px"
    size: "40px"
  nav-active-bar:
    backgroundColor: "{colors.electric-blue}"
    width: "2px"
---

## Overview

EzDistro is a lightweight **Operate-mode** writing tool: the operator's loop is Topics → Draft → Review → Publish. The interface is a wireframe atlas on white paper: hairline rules form the terrain, oversized tight-tracked display titles open top-level pages only, mono uppercase labels pin every section, and electric blue (`#0000ff`) is rationed to exactly three contexts — the logo block, the primary action, and live data nodes. Surfaces are flat (no shadows, no gradients); structure comes from spacing, hairlines, and type scale. Dark theme translates the same rationing onto an ink ground with a one-step-brightened blue. The interface is English-only, laid out with logical properties so it stays direction-agnostic.

Information architecture follows the writing flow, not the data model. Project tabs are grouped **Write** (Topics, Articles, Images, Publish) → **Research** → **Setup** (Settings, Connections, AI models, Prompts) → **System** (Retrieval, Indexing, Jobs, Events). The project landing tab is **Topics**; the project header's primary action is **Continue writing**. Global nav is Workspace (Dashboard, Projects) → Operations (Jobs, Workers, Events) → Account (Billing); Failed jobs live behind Jobs, not as a top-level scare. Styling is standard **DaisyUI + theme tokens** with Tailwind utilities inline in the templates — `input.css` holds only the two theme plugins, base accessibility, the htmx indicator and two Tailwind layer-order fixes. There is no bespoke component layer: all component visuals (cards, stats, menus, docks, toasts, tables, badges, buttons, modals) come from DaisyUI directly.

## Colors

- Paper `#ffffff` page canvas; Ash `#f2f2f2` panel/secondary fill; Mist `#b1b7c3` hairlines, dividers, input outlines.
- Carbon `#000000` primary text and headlines; Graphite `#323232` strong secondary; Slate `#717886` body prose and mono labels.
- Electric blue `#0000ff`: logo block, primary filled button, live pipeline node, active-nav bar/icon, focus ring, selection. Never body text, icons at rest, or decoration.
- Wireframe green `#098551`: published/indexed/healthy nodes and success states.
- Dark (`ezdistro-dark`): ground `#0b0b10`, panel `#14151c`, hairline `#2c2e3a`, ink `#f5f5f5`, blue `#3333ff`, green `#3fce7d`. Same rationing.

## Typography

- Display: Inter, 800, `clamp(2.5rem, 4vw + 1.5rem, 5rem)`, `-0.04em`, `0.95` — page titles only (the `forms.page_header` macro). Clamp keeps display type inside mobile viewports.
- Body: 16px/1.5, `-0.014em`, Slate-tinted secondary text (the `page_header` subtitle).
- Mono labels: uppercase, `+0.073em` tracking, Slate — section tags, panel titles, metric labels, rail labels, nav groups, with a short leading rule on page eyebrows. Content labels are `0.72rem`; chrome labels step down to `0.65rem` and DaisyUI dock labels to `0.6rem`.
- Data numerals use `tabular-nums` (`stat-value`, `.font-mono`, tables, metrics). Hero counts step to `2rem` black, `-0.04em`. Dates render Gregorian through the `loc_*` filters.
- Brand face: Inter ships as a committed local variable font — `app/static/assets/InterVariable.woff2` (352 KB) preferred, with the `InterVariable.ttf` fallback, `@font-face` in `input.css`; no webfont deps.

## Layout

- Desktop: fixed 220px rail (`md:w-55 lg:w-60`) — blue 40px logo block, flat nav items with chevrons, bottom-anchored Start panel (hairline above, mono `START HERE` label, one blue primary), then account/theme foot. Content column fills remaining width, max `75rem`.
- Mobile: sticky blurred top bar (logo + theme switcher + account); floating thumb-zone dock pill (**4 items**: Dashboard, Projects, Jobs, Events — active = solid blue block); `pb-32` content clearance + safe-area insets. Project tabs become a sticky scrollable strip.
- Top-level pages open with the atlas header: mono eyebrow + display title + slate subhead (+ actions right). **Inside the project, tabs use the compact `section_head`** (18px extrabold + hint) — the giant display title never repeats per tab. The workspace and review pages carry a 3-step progress strip (Draft → Review → Publish) driven by article status.
- Dashboard hero is the pipeline rail: 4 cells split by hairlines, node row threaded by one rule (2×2 mobile → 1 row ≥sm), counts at 32px black with micro bars; only the "being written" node breathes. The old activity sparkline strip is removed (it duplicated the rail); per-run bars live only on the Indexing tab where they describe runs.

## Elevation & Depth

No shadows anywhere (`--depth: 0`, `--noise: 0`). Elevation = paper → ash fill → blue action surface. One orchestrated entrance per navigation (the page header rises once, `prefers-reduced-motion` respected); one breathing live node on the rail; theme switch cross-fades in 0.25s and defaults to **Auto** — the OS colour scheme sets the first paint, while an explicit Light or Dark pick is stored and wins.

## Shapes

- 8px (`rounded-lg`) for cards, panels, buttons, inputs, modals, dropdowns. Tags/badges 2px (`rounded-[2px]`). Logo block and rail nodes: square. The mobile bar is DaisyUI's `dock`, whose own radii are the one sanctioned rounded shape.
- Active nav: a tinted primary chip (`bg-primary/10`) with primary text/icon, driven by `aria-current="page"` utilities — no side bar. Focus: 2px blue outline. Selection: blue fill, white ink; caret blue.

## Components

- DaisyUI is the component library — use it directly, no overrides: `card` (+ `card-body`) for panels, `stat` (+ `stat-title`/`stat-value`/`stat-desc`) for metrics, `steps`/`step-primary` for flow progress, `table`/`table-sm` for data, `badge` + `badge-soft` in 2px tags for status, `btn`/`btn-primary`/`btn-ghost` for actions, `menu` for the sidebar nav, `dock` (**4 items**) for the mobile bar, `toast` + `alert alert-soft` for notifications, `modal`/`dropdown`/`join`/`progress` as shipped. Touch targets ≥32px, fields ≥44px.
- There is **no bespoke component layer**. `input.css` keeps only the two theme plugins, base accessibility (focus ring, selection, scrollbars, cursors), the `.htmx-indicator` contract and two Tailwind layer-order fixes (`.table.hidden`, `.theme-switching`). The shell, desktop rail, mobile dock, project subnav and page frame are Tailwind utility classes written inline in `base.html`, `layouts/platform.html` and the shared macros — no `.ezdistro-*` classes exist.
- Shared structure lives in macros: `macros/forms.html` (`page_header`, `panel`, `stat`, `empty`, form controls, disclosures) and `macros/article.html` (one header shared by the article detail, review and workspace pages).
- Status: `badge-soft` in 2px tags; edge color lives in the tag fill + icon chip, never side borders. Rail node colors: planned = mist, writing = blue (live), review = amber, published = green.
- Workspace discipline: the editor is one column of section cards (Title/meta first, then sections, then images). The right rail holds **Status → Validation (only when invalid) → Internal links → Publish history (`<details>` collapsed)**. The duplicate "Generating sections" list is deleted — the outline pane is the single source of section status. Review page: Validation + Editorial QA + Research snapshot + Generation info stay; **Version history is a collapsed `<details>`**.

## Do's and Don'ts

- Do ration blue to logo / primary action / live nodes; everything else is ink, slate, mist.
- Do pin a mono uppercase label above every block; never add kickers above headings or section numbers.
- Do keep backgrounds paper (light) / ink-ground (dark); ash only for secondary fills.
- Do reach for DaisyUI first (`card`, `stat`, `steps`, `table`, `badge-soft`, `btn`, `menu`, `dock`, `toast`); write layout as inline Tailwind utilities.
- Do keep the writing flow one tap away: Topics default tab, Continue-writing primary, 4-item mobile dock, 3-step progress strips on workspace/review.
- Don't add shadows, gradients, glass, or radii other than 8px / 2px / square.
- Don't use color for hierarchy — use display scale, weight, and Carbon/Slate contrast.
- Don't break the rail + fluid-content shell; use logical properties for direction-agnostic layout, never offset hacks.
- Don't reintroduce a bespoke component layer into `input.css` (buttons, inputs, tables, badges, cards, or `.ezdistro-*` class families) or dead motifs — put the utilities on the element in the template instead.
- Detector-accepted exceptions: Inter flagged as an overused face — kept as the committed brand asset under the DaisyUI-only, no-new-deps constraint (PRODUCT.md); the themed scrollbar thumb (`border-radius: 9999px` on the UA scrollbar pseudo-element in `input.css` — a browser surface, not a component); the mobile dock's larger radii, pinned by the floating dock-pill brief.
