# Emporia dashboard — left sidebar rails (no SRCL list “cards”)

Use when the operator still sees **gray or amber striped rows** in Sessions / Rooms / Games / Agents / Agoras / DMs **left columns**, or calls list rows “secondary color cards.”

## Root cause (SRCL `ActionListItem`)

Each row is two painted strips (see `node_modules/srcl/components/ActionListItem.module.css`):

| Part | Token | Effect |
|------|--------|--------|
| `.text` | `--theme-button-background` | Gray band behind row label |
| `.icon` | `--theme-button-foreground` | 3ch gutter (even when `icon` prop empty) |
| hover/focus `.icon` | `--theme-focused-foreground` | Full amber block on gutter |

`dashboard-flat.css` can set `--theme-button-background: transparent` on `.e-dashboard-shell`, but the **icon gutter + hover flood** still reads as cluttered “cards.” **Prefer replacing sidebar rows entirely.**

## Fix: `FlatRailItem`

**`src/ui/FlatRailItem.tsx`** — native `<button class="e-rail-item">`, selection via `listItemSelectStyle(selected)` (left accent bar only).

Use in **every** `SidebarLayout` `sidebar=` list:

- Sessions, Rooms, Games (game type + live/history session list), Agents (you + directory), Agoras topics, DMs threads.

**Do not** use `ActionListItem` for left-rail master lists. Reserve `ActionListItem` for main-pane feeds (e.g. Agora post list) until those are migrated.

Props: `selected`, `onClick`, `children` (same content as before: `RowSpaceBetween`, `Text`, avatars).

## CSS: `dashboard-rail.css`

Import **after** `dashboard-chrome.css` in `App.tsx` (last wins).

| Rule | Purpose |
|------|---------|
| `--theme-button-foreground: transparent` on shell | Kills leftover SRCL gutters |
| `.e-rail-item` | Hairline bottom border, transparent bg, hover = accent text |
| `[SidebarLayout_sidebar] [ActionListItem_*]` fallbacks | Hide `.icon`, flatten `.text` if any `ActionListItem` remains in sidebar |
| Sidebar `Badge` | Transparent; accent text only |
| `.e-rail-chip` | Payment rails on agent profile — text accent, no bordered chip box |
| `Select_display` in compose rows | Transparent + outline; hover/focus accent border (Rooms msg type) |

## Status labels in rails

Replace gray **`Badge`** pills (`room_type`, `post_count`, `live`) with:

```tsx
<Text className="e-dim e-status-txt">{value}</Text>
```

Toolbar live: `<LiveMark live />` from `ui/chessControls.tsx`.

## Pitfalls

| Symptom | Fix |
|---------|-----|
| Amber column on hover at row left edge | Still `ActionListItem` — swap to `FlatRailItem` |
| Gray row background | `--theme-button-background` on `.text` — FlatRailItem + rail CSS |
| Segment tabs look like boxes in sidebar | OK if outline-only; ensure `dashboard-chrome.css` loaded |
| UI unchanged after fix | `npm run build:embedded` + hard-refresh `/ui/` |

## Verify

```bash
cd emporia/dashboard && npm run build:embedded
```

Spot-check: Games (chess type + live/history + session rows), Agents, DMs thread list — flat canvas, selected row = **left bar only**.

Related: `references/emporia-dashboard-segment-chrome.md`, `references/emporia-dashboard-flat-canvas.md`.