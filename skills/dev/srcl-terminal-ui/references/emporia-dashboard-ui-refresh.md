# Emporia dashboard — visible UI refresh

Use when the operator says the console **looks the same** after backend-of-frontend work, or UI/UX is the **priority**. Keep SRCL; change what humans see.

## v5 flat canvas

1. Import **`dashboard-flat.css`** **last** in `App.tsx` (after minimal/polish/v2).
2. Confirm **no gray fills** on main, header, slim cards, SRCL Window sidebars, chat bubbles — **table thead** rows stay gray.
3. Details: `references/emporia-dashboard-flat-canvas.md`.

## v4 cards + theme (minimal retro)

1. Import **`dashboard-minimal.css`** before flat.
2. Use **`src/ui/cards.tsx`** — `SlimCard`, `MetaGrid`, `EmptyPane`, `RailChips`.
3. Theme: **`D` | `L`** + color dots; **outer ring** on selected dot (`dashboard-minimal.css`). Profile: no theme card.
4. Extend to **Games / DMs / Agoras** if cards still feel crowded or empty.

Details: `references/emporia-dashboard-cards-minimal.md`.

## v3 minimum (header-nav — preferred)

1. **Import** full CSS stack through **`dashboard-flat.css`** from `App.tsx`.
2. **One nav system** — `AppNav.tsx` `SectionTabs` in header; mobile drawer = same list. **Remove** persistent sidebar + overview section list + per-route title headers.
3. **Top row** — `✶ Emporia` only (no “Operator console” tagline); `ThemeCompact` (dot only) + `RelayStrip` (one line, WS URL tooltip-only).
4. **Overview** — `.e-stat-strip` + single live-activity feed + payments KPIs — no fat nav cards.
5. **Views** — `ViewBody` / `ViewBody flush`; no inner `Window` + `Navigation`.
6. **`npm run build:embedded`** + tell user to hard-refresh `/ui/` (cite new asset hash).

## v2 layer (still useful)

- `dashboard-v2.css`: feed row styling, layout — **gradients/surfaces overridden by flat**
- `ui/layout.tsx` `PanelCard`, `FeedEventType`

## `ViewBody` contract

```tsx
<ViewBody>…</ViewBody>
<ViewBody flush toolbar={…}>…</ViewBody>  // master–detail
```

## What does *not* count as UX improvement alone

- Splitting `App.tsx` without layout/nav/CSS change
- `ViewStatus` only
- Fixing props without removing redundant nav surfaces

## Pitfalls (from operator feedback)

| Problem | Fix |
|---------|-----|
| Sidebar + tabs + overview shortcuts | Pick header + drawer only |
| WSS/relay twice in header | One `RelayStrip` line; events URL in badge `title` |
| Theme “Appearance” + big swatch grid with names | Dot trigger; `D`/`L` + dots; outer ring on selected |
| Empty master–detail | `EmptyPane` + facts (counts, viewer id) |
| Crowded agent profile | `MetaGrid` + `RailChips` not two tables |
| Cluttered overview cards | Stat strip + feed |
| Section title row under brand | Tabs are enough |
| Gray panels everywhere | `dashboard-flat.css` on `.e-dashboard-shell`; table headers only |

## Embedded stale assets

Always `build:embedded` after dashboard edits; user must hard-refresh `8088/ui/`.