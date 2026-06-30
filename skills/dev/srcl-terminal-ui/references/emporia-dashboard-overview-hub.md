# Emporia dashboard — Overview hub

**Problem:** Overview tiles were wrong size (2-up half-screen), section rows capped columns, duplicate counts (metric + caption), oversized header stack, and wrong Games data.

## What to show (this user)

| Include | Omit on overview |
|--------|-------------------|
| Listings, Rooms, Events, **Games**, Agents, Agoras, DMs | **Sessions** (Games covers play), **Profile** (header tab only) |
| Up to **3** API lines in `.e-overview-card__body` | Gray filled tiles, `/health` modules for Games |

**Games card:** Recent **`emporia:*` sessions** from `GET /sessions` (sorted `created_at` desc), not `health.modules`. Count via `countCaptionForNav` / `gameSessions` — show **once** (see header below).

## Layout (`OverviewView.tsx`)

1. **Intro line** — relay online/offline, WS feed count, `guardrails_mode` (not `guardrails`).
2. **One grid** — `overviewIds = hubGroups.flatMap(g => g.ids)`; render all cards in **`.e-overview-hub`** (no per-group `.e-overview-hub__row` — section headings forced 2-up rows and empty gutter).
3. **`.e-overview-card`** `<button>`:
   - **`.e-overview-card__topline`** — **icon + title** on one line (small type).
   - **`.e-overview-card__sub`** — **single count line**: `{caption || metric}` (e.g. `12 listings`, `1 room`). **Do not** also show a large metric above the title — user rejected duplicate `1` + `1 room`.
   - **`.e-overview-card__body`** — up to 3 `<p>` lines (`-webkit-line-clamp: 3`), thin top rule.
4. **Live activity** `SlimCard` + **`PaymentsFeesSection`**.

Filter: `OVERVIEW_SKIP_VIEWS = ['overview', 'sessions', 'profile']`.

## CSS (`dashboard-chrome.css`) — unified grid

```css
.e-overview-hub {
  display: grid;
  gap: 10px;
  width: 100%;
  grid-template-columns: repeat(auto-fit, minmax(min(100%, 8.5rem), 1fr));
}
.e-overview-card {
  width: 100%;
  min-width: 0;
  aspect-ratio: 1;
  /* transparent border; no max-width cap — cells share 1fr */
}
.e-overview-card__topline {
  display: flex;
  align-items: center;
  gap: 6px;
}
```

**Responsive:**

- **≤640px:** `minmax(min(100%, 7.75rem), 1fr)`.
- **≤400px:** `repeat(2, minmax(0, 1fr))`.

## “More per row” / “only two huge cards”

| Mistake | Fix |
|--------|-----|
| Separate **MARKET/OPS/NETWORK** rows with 2–3 cards each | **One hub grid** for all 7 ids |
| `data-cols={ids.length}` + `repeat(n, 1fr)` | Half-screen squares for 2 cards |
| `overviewGridCols` + flex row + `min-width: 9.5rem` | Flex **wraps at 2** when min-width fights calc |
| `auto-fill` only | Empty tracks on the right |
| Icon → **big metric** → title → **caption** | **Icon + title** → **one** count line |

`overviewGridCols()` may remain in `dashboardCounts.ts` for experiments; **current OverviewView does not use it**.

## Pitfalls

- Games lines from `/health` `modules` → use filtered **sessions**.
- `RelayHealth.guardrails` → **`guardrails_mode`**.
- Filled gray overview tiles after user revert → cards **transparent**; chip fill **only** `.e-status-chip`.
- User: “only square menu, no background” → revert nav/overview **fill**, keep square transparent tabs + ☰.

## SRCL still worth it?

Keep SRCL for chess, `SidebarLayout`, tables, tokens. Overview hub = **plain buttons + `dashboard-chrome.css`**. Consolidate `dashboard-*.css` later; full SRCL drop not required.

## Data

**`fetchOverviewSections(viewerId)`** — listings, sessions, rooms, events, agents, agora topics, DMs (no health call for profile).

Poll: sections ~20s; counts ~15s (`gameSessions`).

## Verify

```bash
cd emporia/dashboard && npm run build:embedded
```

Hard-refresh `/ui/` (new CSS/JS hashes).