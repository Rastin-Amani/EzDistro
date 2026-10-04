# EzDistro — logo kit

**The mark — “e-Loop.”** A lowercase *e* drawn as one continuous stroke: a loop that never
stops. One electric blue, no second colour. The bar through the loop’s core is the feed line.
The wordmark is **Inter** (weight 800), with *Ez* in electric blue and *Distro* in carbon ink.

- **Idea:** a loop that returns enriched — one source, always distributing.
- **Feel:** calm, precise, quietly engineered.
- **Reads:** at 16 px and on signage; works in a single colour.

## Colours

| Role | Name | HEX | RGB | Notes |
|---|---|---|---|---|
| Primary | Electric Blue | `#0000FF` | 0, 0, 255 | The whole mark + `Ez`. Rationed, like the app UI. |
| Wordmark | Carbon Ink | `#0B0B10` | 11, 11, 16 | `Distro` on light backgrounds. |
| Base | White | `#FFFFFF` | 255, 255, 255 | Default background. |

Print: Blue ≈ CMYK 100/100/0/0 · Ink ≈ rich black. Confirm with your printer.

## Files

**Symbol**
- `ezdistro-symbol.svg` — primary (one colour, electric blue)
- `ezdistro-symbol-black.svg` · `ezdistro-symbol-white.svg` — one-colour
- `ezdistro-symbol-square.svg` — centred on a square (avatars / social)

**Lockups** (wordmark is outlined paths — no font needed)
- `ezdistro-logo-horizontal.svg` — symbol + wordmark (primary)
- `ezdistro-logo-horizontal-white.svg` — reversed, for dark surfaces
- `ezdistro-logo-stacked.svg` — symbol above wordmark (narrow spaces)
- `ezdistro-wordmark.svg` — wordmark only

**App / web**
- `ezdistro-app-icon.svg` · `icon-512.png` · `icon-192.png` · `maskable-512.png`
- `favicon.svg` · `favicon.ico` · `favicon-16.png` · `favicon-32.png` · `favicon-48.png`
- `apple-touch-icon.png` · `site.webmanifest` · `head-snippet.html`

## Clear space

Keep at least **half the symbol’s height** of empty space on every side of the lockup (use the
symbol’s ring stroke as a quick gauge). Nothing crosses it — no type, no rules, no edges.

## Minimum sizes

- Symbol: **16 px**
- Horizontal lockup: **24 px** tall (≈ 100 px wide)
- Stacked lockup: **64 px** wide

Below these, use the symbol alone.

## Backgrounds

- On white / light: the primary lockup.
- On ink / photos / dark UI: the reversed (`-white`) lockup.
- On busy imagery: the symbol on the blue app-icon tile, or keep 50 %+ clear space.

## Misuse

Don’t recolour or split the mark into a second colour; don’t stretch, outline, or add shadows to
the mark; don’t re-set the wordmark in another weight or font; don’t place the blue mark on a
low-contrast colour.

## Wiring into the app

The kit lives in `app/static/brand/ezdistro/`. To make it live, replace the icon links in
`app/templates/base.html` with the snippet below (paths are served from `/static`):

```html
<link rel="icon" href="/static/brand/ezdistro/favicon.ico" sizes="any">
<link rel="icon" href="/static/brand/ezdistro/favicon.svg" type="image/svg+xml">
<link rel="apple-touch-icon" href="/static/brand/ezdistro/apple-touch-icon.png">
<link rel="manifest" href="/static/brand/ezdistro/site.webmanifest">
<meta name="theme-color" content="#0000FF">
```

The in-app sidebar logo (`app/templates/components/brand_mark.html`) is a separate UI change —
swap its `#0000ff` square for `ezdistro-symbol-white` on the blue block when you’re ready.

## Notes

- The symbol’s ring is a stroke; it scales cleanly, but run *Outline Stroke + Unite* in a vector
  editor before vinyl cutting or embroidery.
- The wordmark is already outlined; no font install is required.
- A logo is not a trademark clearance — run a professional search before launch.
