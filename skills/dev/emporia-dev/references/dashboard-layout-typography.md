# Dashboard layout & typography (session notes)

## Build & deploy

- Relay static mount: `dashboard/dist` → `/ui/`
- Command: `npm run build:embedded` (not only `npm run build` if embedded base path matters)

## Typography preference (operator)

- Keep SRCL at **100%** (16px root on `html, body`).
- **Current preference:** no global ~1.1× “comfort bump” across all `dashboard.css` rules — tune individual sections (KPI strips, nav labels) if needed.
- **Never** use `html { font-size: 110% }` as the primary fix (competes with SRCL tokens and fixed `px` literals).
- Shell: `.e-dashboard-shell { --font-size: 16px; }` and **both** `e-app-shell` + `e-dashboard-shell` on the root div in `App.tsx`.

### One-off revert of an old 1.1× pass

Multiply each `font-size: Nrem` / `Npx` in `dashboard.css` by **1/1.1**; round px to int, rem to 3–4 decimals. **Exclude** `--font-size: 16px` on `.e-dashboard-shell` (restore manually if a script touched it). Re-run `build:embedded`.

### Two-tier fix (KPI OK, rest too small)

After a global 1/1.1 revert, operator may want **smaller KPI numbers** (Payments, Trust & Safety on Overview, Fees tab) but **larger body/nav** elsewhere.

1. **Pin KPI** — leave selectors containing `.e-kpi` unchanged (e.g. `__val` ~1rem, `__lbl` ~0.62rem, row `minmax(72px, 1fr)`).
2. **Bump UI** — multiply every other `font-size:` line by **1.1** (skip lines with `.e-kpi` in the selector and skip `--font-size: 16px`).
3. Helper: `scripts/selective-ui-font-bump.py` in this skill (point `DASHBOARD_CSS` at `dashboard/src/dashboard.css`).

Do **not** multiply KPI rules again — that recreates “fees/payments/trust too big.”

## Hash routing (nav “directory”)

- File: `dashboard/src/dashboardRoute.ts` — `viewFromHash()`, `syncHash(view)`, routes like `#/fees`.
- `App.tsx`: initial state from hash; `applyView` updates hash; `hashchange` handler.
- Without this, hard refresh always lands on Overview.

## Nav active-state pitfall

- Do not map `fees` → `overview` for tab highlighting in `AppNav.tsx` (Fees tab looked like Overview was selected).
- Use `tabView = view` with only **games → sessions** alias if needed.

## Chess session transport (Sessions / Games)

- **Wrap:** `<div className="e-chess-stage"><Chessboard … /><ChessTransport status={…} /><ChessSpeedSlider … /></div>` in `SessionsView`, `GamesView`, and `ChessReplayPanel`.
- **Components:** `ui/chessControls.tsx`; icons in `ui/chessIcons.tsx`.
- **Do not add a second live strip:** omit `ViewBody` `toolbar` (`live · N`, `LiveMark`). Operator: live count is **already** on Games **SegmentTabs** (`live · {length}`) — only remove the extra toolbar/header to stop vertical shift.
- **No header above board:** drop session-id / live / move `RowSpaceBetween` above the chessboard; use `ChessReplayStatus` (and optional `AuditBadge`) in `ChessTransport` `status` on the right of `.e-chess-ctrl-row`.
- **Toolbar:** four icon buttons in `.e-chess-ctrl-icons` — prev/next chevrons, play/stop toggle (replay), refresh-to-latest. Words only in `aria-label`/`title`.
- **Chrome:** `.e-chess-icon-btn__glyph` — **padding ~8px** + inset `box-shadow` 1px border (SRCL ActionButton-like), not borderless oversized SVGs alone. `.e-chess-svg` ~**1.25rem** inside glyph.
- **Relay header:** `RelayStrip` events live indicator — pulsing `●` with fixed width, **not** `BlockLoader` (prevents nav tab horizontal jump).

## Horizontal scroll fix checklist

1. `.e-app-shell` / `.e-app-main` / `.e-view`: `max-width: 100%`, `overflow-x: clip`, `min-width: 0` on flex children
2. `.e-pipeline-strip`, `.e-overview-status`: `max-width: 100%`, `overflow-wrap: anywhere`
3. `.e-overview-hub`: `minmax(0, 1fr)` columns; card sublabels ellipsis
4. `.e-app-top__row`: `flex-wrap: wrap`
5. `.e-view-body`: `max-width: min(1440px, 100%)`

## KPI strips (Overview Payments / Trust, Fees view)

- `.e-kpi-row`: `minmax(72px, 1fr)`, gap ~8px
- `.e-kpi__val`: ~1rem if hero numbers feel oversized after typography changes