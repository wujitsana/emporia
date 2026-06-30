# Emporia dashboard — segment chips & chrome (session notes)

## User preference (repeat complaints)

- **One flat dark background** everywhere except **table `thead`**.
- **Chosen nav / filters / options** = **main secondary accent** (`--theme-focused-foreground`): border + text, **not** gray panel fill and **not** solid SRCL ActionButton `.selected` background.
- Master–detail views: **auto-select first row**; no “Pick a session/room/agent” empty marketing copy.

## CSS import order (`App.tsx`)

```
dashboard.css → dashboard-v2.css → dashboard-polish.css → dashboard-minimal.css
→ dashboard-flat.css → dashboard-chrome.css → dashboard-rail.css
```

| File | Role |
|------|------|
| `dashboard-flat.css` | SRCL token flatten on `.e-dashboard-shell`; Window/modal/input; feed/tab hover; listings table |
| `dashboard-chrome.css` | `SegmentTabs`, `e-status-chip`, `e-ctrl-btn`, `e-sub-header`; **square** controls (`border-radius: 0`); `ActionButton_content` + chips use `--theme-border` fill; Agora/Window/Card transparent overrides |
| `dashboard-rail.css` | `FlatRailItem`, sidebar Select/Badge flatten, `--theme-button-foreground` |
| `dashboard-polish.css` | `.e-section-tab.is-active` must stay **transparent** + accent (patched — no `emporia-nav-active`) |

## Components

| Path | Use |
|------|-----|
| `ui/SegmentTabs.tsx` | Visibility, sort, live/history, agent profile tabs, post type |
| `ui/chessControls.tsx` | `ChessTransport`, `LiveMark`, `MoveIndex`, `ChessSpeedSlider` |
| `ui/FlatRailItem.tsx` | **Left sidebar** master lists (Sessions, Rooms, Games, Agents, Agoras, DMs) — not ActionListItem |
| `listingNav.ts` | `viewForListing` → `navigate(view, NavOpts)` — `listingPeek`, `gameModuleType` (chess only), `roomId`, `agentId` |

## Anti-patterns

- `ActionButton` + `isSelected` for **mutually exclusive options** (games live/history, agora ALL/public, agent tabs).
- `ActionListItem` in **sidebar** master lists — use **`FlatRailItem`** (`references/emporia-dashboard-left-rail.md`).
- `Badge` for `active` / `live` / counts in sidebars — use `.e-status-txt` or dim `Text`.
- `EmptyPane` with “Pick a …” in spectator master–detail main pane.

## Verify

```bash
cd emporia/dashboard && npm run build:embedded
```

Hard-refresh `http://127.0.0.1:8088/ui/` (new `dist/assets/index-*.css` hash).