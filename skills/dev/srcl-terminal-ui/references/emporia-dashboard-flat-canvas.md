# Emporia dashboard — flat canvas (one background)

Use when the operator says **gray backgrounds** remain, panels look stacked, or they want **minimalist retro-modern** with **one dark color** except allowed contrast (e.g. table headers).

## Layer order

Import in `App.tsx` (last file wins):

```
dashboard.css → dashboard-v2.css → dashboard-polish.css → dashboard-minimal.css → dashboard-flat.css → dashboard-chrome.css → dashboard-rail.css
```

`dashboard-v2.css` still adds structure (feed rows, stat strip layout) but **flat** overrides fills/gradients on `.e-dashboard-shell`.

## What `dashboard-flat.css` does

Scoped to **`.e-dashboard-shell`**:

1. **CSS variables** — align modal/window/input/button surfaces with page background:
   - `--theme-background-modal`, `--theme-window-background`, `--theme-background-input`, `--theme-button-background` → `var(--theme-background)`
   - `--theme-window-shadow: transparent`
   - `--emporia-surface`, `--emporia-surface-raised`, `--emporia-nav-active` → `transparent`

2. **Chrome** — `e-app-main`, `e-app-top`, drawer: `background: var(--theme-background) !important` (no gradients).

3. **Emporia panels** — `e-slim-card`, `e-panel-card`, KPI/stat items, empty panes: transparent + border only.

4. **SRCL Window** — `[class*="Window_window"]`: flat background, no drop shadow (Sessions/Games sidebars).

5. **Chat** — `[class*="Message_bubble"]` / `MessageViewer_bubble`: transparent fill, 1px border, no gray box-shadow.

6. **Interaction** — tab/feed hover: **border/accent only**, no gray wash (`background: transparent !important` on hover/active where polish used surfaces).

7. **Exception — table headers** — keep SRCL gray bar:
   ```css
   .e-dashboard-shell table thead td {
     background: var(--ansi-240-gray-35, #585858) !important;
   }
   ```
   Body rows stay transparent.

8. **Mobile overlay** — `.e-mobile-drawer__backdrop` may stay semi-transparent black; drawer panel uses page background.

## Pitfalls

| Symptom | Cause | Fix |
|---------|--------|-----|
| Gray sidebar **list rows** (striped) | SRCL `ActionListItem` button-bg + icon gutter | **`FlatRailItem`** + `dashboard-rail.css` — see `references/emporia-dashboard-left-rail.md` |
| Gray chat bubbles | `--theme-border` fill on `.bubble` | Flat outline-only override |
| Header still tinted | `e-app-top` `emporia-surface` in polish | flat `!important` on top/header |
| “Fixed” but UI unchanged | Stale `/ui/` assets | `npm run build:embedded` + hard-refresh |

## Verify

```bash
cd emporia/dashboard && npm run build:embedded
```

Spot-check: Overview, Sessions (sidebar + detail), Rooms messages, Listings table **header row** still gray, body flat.

Related: `references/emporia-dashboard-cards-minimal.md`, `references/emporia-dashboard-ui-refresh.md`.