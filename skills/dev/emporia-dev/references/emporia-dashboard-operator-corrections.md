# Emporia dashboard — operator corrections (session notes)

Consolidated pitfalls from UI polish sessions — authoritative detail lives in **`creative/srcl-terminal-ui`** references.

## “Square menu only, no different background”

When the user narrows a styling ask to **only** the hamburger/menu control:

1. **Revert** filled **overview tile** experiments, **`fetchDashboardHints`**, and global **`ActionButton_content { background: var(--theme-border) }`**.
2. **Keep:** `border-radius: 0` on **`.e-mobile-top`** menu `ActionButton`, **`e-section-tab`** (header + drawer), and **`.e-status-chip`** (API/events) with chip fill.
3. Section nav stays **transparent**; active state = accent **border**, not gray chip.

Do not re-ship filled nav/overview chips unless the user asks again.

## “Overview should show info from all tabs”

Means **live REST snippets**, not click-only tiles:

- **`fetchOverviewSections`** — up to **3** lines per card (`<p>` in `.e-overview-card__body`).
- **Omit** **Sessions** and **Profile** on overview (games cover play; profile is header-only).
- **Games** = recent **`emporia:*`** sessions, not `/health` `modules`.
- Layout: **`emporia-dashboard-overview-hub.md`** — **single** `.e-overview-hub` grid for all tab ids (no MARKET/OPS/NETWORK row breaks).

## “Too big when only two cards” / “max per row should increase”

- **Wrong:** Per-section rows with 2 cards + `repeat(2, 1fr)` or `data-cols={ids.length}` → **half-screen** squares.
- **Wrong:** `overviewGridCols` + flex + **`min-width: 9.5rem`** → often **wraps to 2 columns** anyway.
- **Right (shipped):** **One grid** for all overview cards; **`repeat(auto-fit, minmax(min(100%, 8.5rem), 1fr))`**; square **`aspect-ratio: 1`**; no duplicate count in header.

## “Number repeated” (e.g. `1` then `1 room`)

Show count **once**: **`.e-overview-card__topline`** = icon + **title** only; **`.e-overview-card__sub`** = `caption || metric`. Do not stack a large metric above the caption.

## “Cards should fill horizontally” vs “small and many”

Resolved with **unified auto-fit grid** (many columns on wide viewports) rather than **n = card count** `1fr` rows or fixed `max-width` tiles with empty gutter.

## Discover surface

Rooms / DMs / Agoras: **`ReadOnlyChat`** or scroll-only detail — no compose, vote, or comment bars. Agents post via MCP/REST.

## Build

Always `npm run build:embedded` and tell user to hard-refresh `/ui/` (new asset hashes).