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
  blue-dark: "#5b5bff"
  green-dark: "#3fce7d"
typography:
  display:
    fontFamily: "Arad, Inter, ui-sans-serif, system-ui, sans-serif"
    fontSize: "clamp(2.5rem, 4vw + 1.5rem, 5rem)"
    fontWeight: 800
    lineHeight: 0.95
    letterSpacing: "-0.04em"
  body:
    fontFamily: "Arad, Inter, ui-sans-serif, system-ui, sans-serif"
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
rounded:
  tag: "2px"
  surface: "8px"
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

EzDistro's interface is a wireframe atlas on white paper: hairline rules form the terrain, oversized tight-tracked display titles carry hierarchy, mono uppercase labels pin every section, and electric blue (`#0000ff`) is rationed to exactly three contexts — the logo block, the primary action, and live data nodes. Surfaces are flat (no shadows, no gradients); structure comes from spacing, hairlines, and type scale. Dark theme translates the same rationing onto an ink ground with a one-step-brightened blue. Persian (RTL) is the source language; English (LTR) mirrors via logical properties.

## Colors

- Paper `#ffffff` page canvas; Ash `#f2f2f2` panel/secondary fill; Mist `#b1b7c3` hairlines, dividers, input outlines.
- Carbon `#000000` primary text and headlines; Graphite `#323232` strong secondary; Slate `#717886` body prose and mono labels.
- Electric blue `#0000ff`: logo block, primary filled button, live pipeline node, active-nav bar/icon, focus ring, selection. Never body text, icons at rest, or decoration.
- Wireframe green `#098551`: published/indexed/healthy nodes and success states.
- Dark (`ezdistro-dark`): ground `#0b0b10`, panel `#14151c`, hairline `#2c2e3a`, ink `#f5f5f5`, blue `#5b5bff`, green `#3fce7d`. Same rationing.

## Typography

- Display: Arad (Persian) / Inter (Latin), 800, `clamp(2.5rem, 4vw + 1.5rem, 5rem)`, `-0.04em`, `0.95` — page titles only (`.ezdistro-title`). Clamp keeps Persian display type inside mobile viewports.
- Body: 16px/1.5, `-0.014em`, Slate-tinted secondary text (`.ezdistro-subtitle`).
- Mono labels: uppercase, `+0.073em` tracking, 12px, Slate — section tags, panel titles (`.ezdistro-panel-title`), metric labels, rail labels, nav groups, always with a short leading rule (`.ezdistro-eyebrow::before`).
- Data numerals are tabular everywhere (`font-variant-numeric: tabular-nums` on `.stat-value`, `.font-mono`, tables, metrics). Dates render Jalali for `fa`, Gregorian otherwise.
- Brand faces are committed local variable fonts (Arad + Inter); no webfont deps.

## Layout

- Desktop: fixed 220px rail (`md:w-55 lg:w-60`) — blue 40px logo block, flat nav items with chevrons, bottom-anchored Start panel (hairline above, blue dot + mono `START HERE`, one blue primary + ghost actions), then account/theme/locale foot. Content column fills remaining width, max `75rem`.
- Mobile: sticky blurred top bar (logo + theme/locale/account); floating thumb-zone dock pill (6 items, active = solid blue block); `pb-32` content clearance + safe-area insets. Project tabs become a sticky scrollable strip.
- Pages open with the atlas header: mono label + display title + slate subhead (+ actions right). Dashboard hero is the pipeline rail: 4 cells split by hairlines, node row threaded by one rule (2×2 mobile → 1 row ≥sm), counts at 32px black with micro bars; only the "being written" node breathes.
- Signature motif `.ezdistro-bars`: CSS hairline vertical bars (`2px`, mist) with occasional `.is-blue` / `.is-green` nodes for decorative-data strips.

## Elevation & Depth

No shadows anywhere (`--depth: 0`, `--noise: 0`). Elevation = paper → ash fill → blue action surface. One orchestrated entrance per navigation (`.ezdistro-pagehead` rises once, `prefers-reduced-motion` respected); one breathing live node on the rail; theme switch cross-fades in 0.25s.

## Shapes

- 8px (`rounded-lg`) for cards, panels, buttons, inputs, modals, dropdowns, dock. Tags/badges 2px (`rounded-[2px]`). Logo block and bar nodes: square.
- Active nav: 2px blue bar at inline-start + blue icon, no background chip. Focus: 2px blue outline. Selection: blue fill, white ink; caret blue.

## Components

- Buttons: `.btn` 8px + mist border; `.btn-primary` solid blue, white ink, darkens on hover; `.btn-ghost` borderless paper. Touch targets ≥32px, fields ≥44px.
- Tables: sticky mist headers, calm row hover, hairline row dividers; mobile collapses to divided row lists.
- Status: DaisyUI `badge-soft` in 2px tags; edge color lives in the tag fill + icon chip, never side borders. Rail node colors: planned = mist, writing = blue (live), review = amber, published = green.
- Toasts: flat paper cards, semantics in the icon chip only. Confirm dialog, dropdowns, modals: paper + hairline, no shadow.
- Forms: 8px fields, blue focus ring (`border-primary` + soft ring), 40%-opacity placeholders; controls fill their grid cell.

## Do's and Don'ts

- Do ration blue to logo / primary action / live nodes; everything else is ink, slate, mist.
- Do pin a mono uppercase label above every block; never add kickers above headings or section numbers.
- Do keep backgrounds paper (light) / ink-ground (dark); ash only for secondary fills.
- Don't add shadows, gradients, glass, or radii other than 8px / 2px / square.
- Don't use color for hierarchy — use display scale, weight, and Carbon/Slate contrast.
- Don't break the rail + fluid-content shell; RTL via logical properties only, never offset hacks.
- Detector-accepted exceptions: Inter/Arad flagged as overused faces — kept as committed brand assets under the DaisyUI-only, no-new-deps constraint (PRODUCT.md).
