---
name: EzDistro Atlas
description: Wireframe-atlas operations UI — paper canvas, carbon ink, one rationed electric blue over DaisyUI.
colors:
  paper: "#ffffff"
  ash: "#f6f6f7"
  mist: "#e4e4e7"
  carbon: "#111113"
  graphite: "#323232"
  slate: "#717886"
  electric-blue: "#0000ff"
  wireframe-green: "#098551"
  ink-ground: "#1a1b21"
  blue-dark: "#7575ff"
  green-dark: "#3fce7d"
typography:
  display:
    fontFamily: "Inter, ui-sans-serif, system-ui, sans-serif"
    fontSize: "clamp(1.9rem, 1.35rem + 1.7vw, 3rem)"
    fontWeight: 800
    lineHeight: 1.02
    letterSpacing: "-0.035em"
  body:
    fontFamily: "Inter, ui-sans-serif, system-ui, sans-serif"
    fontSize: "1rem"
    fontWeight: 400
    lineHeight: 1.5
    letterSpacing: "-0.011em"
  mono-label:
    fontFamily: "JetBrains Mono, IBM Plex Mono, ui-monospace, Menlo, Consolas, monospace"
    fontSize: "0.7rem"
    fontWeight: 400
    lineHeight: 1.43
    letterSpacing: "0.073em"
  metric-count:
    fontSize: "2.25rem"
    fontWeight: 800
    lineHeight: 1
    letterSpacing: "-0.04em"
rounded:
  tag: "2px"
  field: "10px"
  surface: "12px"
  selector: "8px"
spacing:
  unit: "4px"
  section: "48px"
  card: "24px"
components:
  button-primary:
    backgroundColor: "{colors.electric-blue}"
    textColor: "{colors.paper}"
    rounded: "{rounded.field}"
    padding: "12px 16px"
  button-ghost:
    backgroundColor: "{colors.paper}"
    textColor: "{colors.carbon}"
    rounded: "{rounded.field}"
    padding: "12px 16px"
  logo-block:
    backgroundColor: "{colors.electric-blue}"
    textColor: "{colors.paper}"
    rounded: "{rounded.field}"
    size: "36px"
  nav-active-chip:
    backgroundColor: "primary/10"
    textColor: "{colors.electric-blue}"
---

## Overview

EzDistro is a lightweight **Operate-mode** writing tool: the operator's loop is Content plan → Draft → Review → Publish. The interface is a wireframe atlas on white paper: hairline rules form the terrain, tight-tracked display titles open top-level pages only, mono uppercase labels pin every section, and electric blue (`#0000ff`) is rationed to brand and interaction surfaces — the logo block, the primary action, live data nodes, the active nav chip, focus and selection. It is never body text, resting icons or decoration. Surfaces are flat (no card shadows, no gradients); structure comes from spacing, hairlines, and type scale. Dark theme translates the same rationing onto an ink ground with a one-step-brightened blue. The interface is English-only, laid out with logical properties so it stays direction-agnostic.

Information architecture follows the writing flow, not the data model. Project tabs are grouped **Grow your website** (Overview, Find opportunities, Content plan, Articles, Published) → **Set it up** (Website setup, Advanced); specialist surfaces (Connections, AI models, Writing rules, Article images, Search settings, Site health, Technical task history, Diagnostic logs) live one level down under Advanced. Global nav is **Workspace** (Overview, Websites) → **Operations** (Activity, Automation, Workers, Events) → **Account** (Plan & billing), with billing also reachable from the account menu. Styling is standard **DaisyUI + theme tokens** with Tailwind utilities inline in the templates — `input.css` holds the two theme plugins, `@theme` animations, the Inter `@font-face`, base accessibility, the htmx indicator, two table display-order fixes, the theme cross-fade rule, and scrollbar/mobile-overflow rules. There is no bespoke component layer: all component visuals (cards, stats, menus, docks, toasts, tables, badges, buttons, modals, tabs) come from DaisyUI directly.

## Colors

- Paper `#ffffff` page canvas; Ash `#f6f6f7` panel/secondary fill; Mist `#e4e4e7` hairlines, dividers, input outlines.
- Carbon `#111113` primary text and headlines; Graphite `#323232` strong secondary; secondary prose and mono labels use the `base-content/60–65` tokens (a literal slate hex appears only on the PWA offline page).
- Electric blue `#0000ff`: logo block, primary filled button, live pipeline node, active-nav chip, focus ring, selection. Never body text, icons at rest, or decoration.
- Wireframe green `#098551`: published/indexed/healthy nodes and success states.
- Dark (`ezdistro-dark`): graphite ground `#1a1b21` (a step above pure black, so panels, hairlines and floating layers all separate), panel `#23252d`, hairline `#383b47`, ink `#e9eaee`, blue `#7575ff`, green `#3fce7d`. The dark ramp mirrors the light paper → ash → mist steps instead of inverting them, which is what previously produced a black-on-black page. The blue keeps the brand hue (240°) and is lifted only as far as contrast requires: `#0000ff` sits at ~2.2:1 on the ground, `#7575ff` at 4.7:1, and its fill carries dark ink (`#0d0e13`, 5.3:1) so the primary button is readable too.

## Typography

- Display: Inter, 800, `clamp(1.9rem, 1.35rem + 1.7vw, 3rem)`, `-0.035em`, `1.02` — top-level page titles only (the `forms.page_header` macro). The login page uses its own smaller scale (`clamp(2rem, 2.4vw + 1rem, 3.25rem)` hero, `1.75rem` form title). The restrained ceiling keeps content above the fold on laptops and phones while still reading as an atlas title.
- Body: 16px/1.5, `-0.011em`, secondary text at `text-base-content/60–65` for subtitles and prose. **Text floor:** every text token targets at least `text-base-content/60` (4.9:1 on paper, 5.9:1 on the dark ground); a few billing body lines sit at `/55`, and `/30` survives only on `aria-hidden` separators, never on words.
- Mono labels: uppercase, `+0.073em` tracking, `0.7rem`, `text-base-content/60` — section tags, panel titles, metric labels, rail group labels, table headers, with a short leading rule on page eyebrows. One size for every chrome label; no second scale.
- Data numerals use `tabular-nums` (`stat-value`, `.font-mono`, tables, metrics). Hero counts step to `2.25rem` extrabold, `-0.04em`. Dates render Gregorian through the `loc_*` filters.
- Brand face: Inter ships as a committed local variable font — `app/static/assets/InterVariable.woff2` (352 KB) preferred, with the `InterVariable.ttf` fallback, `@font-face` in `input.css`; no webfont deps. `font-optical-sizing: auto` and `text-rendering: optimizeLegibility` are set globally.

## Layout

- Desktop: sticky rail 240px → 256px (`md:w-60 lg:w-64`) — blue 36px logo block (`rounded-field`), flat nav items with 18px icons and no chevrons, a bottom **Start here → New website** panel (only on a first run with no projects), then a single foot row with the theme switcher and the account menu. Content column fills remaining width, max `75rem`, `px-5 → px-10`, `pt-6 → pt-8`.
- Mobile: sticky blurred top bar (logo + theme switcher + account); floating thumb-zone dock pill (**4 items**: Overview, Websites, Activity, Automation — active = solid blue block); `pb-32` content clearance + safe-area insets. Project tabs become a sticky scrollable underline strip (`tabs tabs-border`).
- Top-level pages open with the atlas header: mono eyebrow + display title + slate subhead (+ actions right). **Inside the project, tabs use the compact `section_head`** (20px bold + hint) — the display title never repeats per tab. The workspace and review pages carry the shared article header; review leads with a single verdict card.
- Dashboard hero is the pipeline rail: 4 cells split by hairlines, mono labels, counts at 36px, a micro share bar per stage; only the "Writing" node breathes. The old activity sparkline strip stays removed (it duplicated the rail); per-run bars live only on the Indexing tab where they describe runs.

## Elevation & Depth

No card shadows and no gradients. Elevation is paper → ash fill → blue action surface. Two deliberate exceptions, both from DaisyUI itself:

1. `--depth: 1` gives buttons, inputs, toggles and tabs a one-pixel inset highlight — the tactile "solid" quality of a real control without lifting the surface.
2. Floating layers (modal, dropdown, dock, toast) keep DaisyUI's own soft shadow so they read as materials above the page rather than holes in it.

Motion: one orchestrated entrance per navigation (`animate-rise` on the page header, 420 ms, `cubic-bezier(.22,1,.36,1)`, gated by `motion-safe`); tab fragments fade in 240 ms; the top progress bar responds on request start; theme switch cross-fades in 0.25 s and defaults to **Auto** — the OS colour scheme sets the first paint, while an explicit Light or Dark pick is stored and wins. `prefers-reduced-motion` removes the entrance, press scale and theme cross-fade.

## Shapes

- 12px (`rounded-box`) for cards, panels, modals, dropdowns, stat tiles. 10px (`rounded-field`) for buttons, inputs, selects, icon chips, nav items and the logo block. 8px (`--radius-selector`) for checkboxes, radios, toggles and status badges, so every tag reads as a soft chip rather than a hard rectangle. Pipeline nodes are `rounded-full` (live dots); the mobile dock is the one sanctioned large radius (`rounded-2xl`).
- Active nav: a tinted primary chip (`bg-primary/10`) with primary text/icon, driven by `aria-current="page"` utilities — no side bar. Project tabs: underline (`tabs-border`). Accordions: the divider only appears when a disclosure is open (`group-open:border-b`), so a closed card has no stray rule. Focus: 2px blue outline, 2px offset. Selection: blue fill, white ink; caret blue. Buttons compress to 0.98 on press (120 ms) for immediate feedback.

## Components

- DaisyUI is the component library — use it directly, no overrides: `card` (+ `card-body`) for panels, `stat` (+ `stat-title`/`stat-value`/`stat-desc`) for metrics, `tabs tabs-border` for project sections, `table`/`table-sm` for data with mono uppercase headers, `badge` + `badge-soft` for status (2px only on compact type tags), `btn`/`btn-primary`/`btn-ghost` for actions, `dock` (**4 items**) for the mobile bar, `toast` + `alert alert-soft` for notifications, `modal`/`dropdown`/`join`/`progress` as shipped, and `menu` inside the account popovers (the rail itself is a plain list of links). Touch targets ≥32px, fields 44px (`--size-field: 0.275rem`).
- There is **no bespoke component layer**. `input.css` keeps only the two theme plugins, base accessibility (focus ring, selection, scrollbars, cursors, press scale), the `.htmx-indicator` contract, two table display-order fixes (`.table.hidden`, `.sm\:table.hidden`) and the `.theme-switching` cross-fade rule. The shell, desktop rail, mobile dock, project subnav and page frame are Tailwind utility classes written inline in `base.html`, `layouts/platform.html` and the shared macros — no `.ezdistro-*` classes exist.
- Menus (row actions, account, event data) are **popover** menus, not absolute dropdowns: they live in the top layer, so a sticky bar, a card or an `overflow-x-auto` table can never cover or clip them. DaisyUI's `dropdown[popover]` relies on CSS anchor positioning (unsupported in Firefox) and the UA default centres a popover, so `base.html` positions every open popover against its invoker — flip up near the bottom, clamp to the viewport, scroll when taller than the screen. One helper, every menu.
- Shared structure lives in macros: `macros/forms.html` (`page_header`, `section_head`, `panel`, `stat`, `empty`, form controls, disclosures) and `macros/article.html` (one header shared by the article detail, review and workspace pages).
- Status: `badge-soft` tags with 8px corners; edge color lives in the tag fill + icon chip, never side borders. Rail node colors: planned = mist, writing = blue (live), review = amber, published = green. Job rows use a status medallion (check / warning / spinner / clock) instead of glyphs.
- Workspace discipline: the editor is one column of section cards (Title/meta first, then sections, then images). The right rail holds **Title and meta → Checks (always) → Article (score/words/keyword/dates) → Internal links → Publish history (`<details>` collapsed)**. The duplicate "Generating sections" list is deleted — the outline pane is the single source of section status. Review page: **Article · Outline · Checks · Editorial QA · Article details · Internal links · Section provenance**; **Version history is a collapsed `<details>`** with restore.

## Do's and Don'ts

- Do ration blue to logo / primary action / live nodes / active nav; everything else is ink, slate, mist.
- Do pin a mono uppercase label above every block; never add kickers above headings or section numbers.
- Do keep backgrounds paper (light) / ink-ground (dark); ash only for secondary fills.
- Do reach for DaisyUI first (`card`, `stat`, `tabs`, `table`, `badge-soft`, `btn`, `menu`, `dock`, `toast`); write layout as inline Tailwind utilities.
- Do keep the writing flow one tap away: Overview and Websites first in the rail, Continue-writing primary on the project, 4-item mobile dock, underline project tabs.
- Don't add card shadows, gradients, glass or radii other than 12px / 10px / 8px / 2px / square.
- Don't use color for hierarchy — use display scale, weight, and Carbon/Slate contrast.
- Don't break the rail + fluid-content shell; use logical properties for direction-agnostic layout, never offset hacks.
- Don't reintroduce a bespoke component layer into `input.css` (buttons, inputs, tables, badges, cards, or `.ezdistro-*` class families) or dead motifs — put the utilities on the element in the template instead.
- Detector-accepted exceptions: Inter flagged as an overused face — kept as the committed brand asset under the DaisyUI-only, no-new-deps constraint (PRODUCT.md); the themed scrollbar thumb (`border-radius: 9999px` on the UA scrollbar pseudo-element in `input.css` — a browser surface, not a component); the mobile dock's larger radius and soft shadow, pinned by the floating dock-pill brief; DaisyUI's own modal/dropdown elevation.
