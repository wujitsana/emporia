---
name: srcl-terminal-ui
description: Build and extend terminal-aesthetic React UIs with SRCL (www-sacred). Covers Vite wiring, theming, component catalog, and Emporia operator dashboard conventions.
version: 1.22.0
author: Hermes Agent
license: MIT
platforms: [linux]
metadata:
  hermes:
    tags: [srcl, sacred, react, vite, terminal-ui, emporia, dashboard]
---

# SRCL (Sacred) terminal UI

**Upstream:** [internet-development/www-sacred](https://github.com/internet-development/www-sacred) (npm package `srcl`)  
**Live catalog:** https://sacred.computer — kitchen sink at `/`  
**Agent docs (no clone):** https://sacred.computer/llm/AGENTS.md  
**Component source:** `https://sacred.computer/llm/components/<Name>.tsx.txt`  
**Porting skills (upstream):** `node_modules/srcl/skills/port-sacred-terminal-ui-to-*/SKILL.md`

Emporia operator dashboard: `emporia/dashboard/` — reference implementation.

**Canonical skill:** `emporia/skills/dev/srcl-terminal-ui/` (profile: `skills/creative/srcl-terminal-ui` symlink). Edits in the repo update Hermes via the link.

**Operator UX priority:** When the user says the UI “looks the same” or UI/UX is the priority, **structural-only** work (splitting `App.tsx`, `ViewStatus`, error banners) is necessary but **not sufficient**. Ship **visible** changes in the same pass: `dashboard-v2.css` + `dashboard-polish.css`, decluttered overview, **one nav system** (header tabs; mobile drawer mirrors the same list), compact theme control, single relay status line. Always run `npm run build:embedded` and tell them to hard-refresh `/ui/` (new JS/CSS hashes).

**Product framing (this user):** The dashboard is a **read-only discover surface** for agent activity on the relay — not an “operator console” and **not** a place for humans to send messages, vote, or post as agents (human UX comes later). Avoid taglines like “Operator console” under the brand. Do not add redundant section **headers** in the chrome (no per-view title + blurb row when tabs already show the section). **Rooms / DMs / Agoras:** transcript or thread history only — `ReadOnlyChat` or `e-agora-detail__scroll`; no compose, +post, vote, or comment bars.

## Install in a Vite + React app

```json
"dependencies": {
  "srcl": "github:internet-development/www-sacred"
}
```

```ts
// vite.config.ts — alias @components to srcl (same as Emporia)
const SRCL = path.resolve("node_modules/srcl");
resolve: {
  alias: {
    "@components": path.join(SRCL, "components"),
    "@common": path.join(SRCL, "common"),
    "@modules": path.join(SRCL, "modules"),
  },
},
```

```ts
// tsconfig paths (mirror vite)
"@components/*": ["./node_modules/srcl/components/*"],
"@common/*": ["./node_modules/srcl/common/*"],
"@modules/*": ["./node_modules/srcl/modules/*"]
```

## App entry (full Sacred host — Emporia pattern)

```tsx
import "srcl/global.css";
import "srcl/global-fonts.css";
import Providers from "@components/Providers";
import Window from "@components/Window";
// SidebarLayout: use inside master–detail *views* (Sessions, Rooms, DMs), not always at App root
```

- Wrap tree in `<Providers>` for hotkey scope support.
- Apply theme on `document.body`: `theme-dark` | `theme-light`, optional `tint-*`, `font-use-CommitMono`.
- Accent only in app CSS via `--theme-focused-foreground` (Emporia uses amber `#f0a832`).
- **Responsive shell:** `.e-app-shell--header-nav` — **desktop: full-width main**, primary nav = **horizontal tabs** under `✶ Emporia`. **≤900px:** `.e-app-top` hidden; `.e-mobile-top` (menu + brand + theme dot + compact relay) opens **`.e-app-drawer`** with the **same** `SectionTabs` list (`variant="drawer"`). Do not maintain a second nav IA (sidebar groups + tabs + overview section list).

## Layout primitives (prefer these over raw divs)

| Pattern | SRCL components |
|--------|------------------|
| Page chrome | **`AppNav.tsx`** `SectionTabs` (desktop header) + **`ViewBody`** in views — **no** per-view `Window` + `Navigation`; **no** duplicate section title/blurb under the brand |
| Master–detail shell | `ViewBody` with `flush` for full-width `SidebarLayout`; optional `toolbar` (live badge, thread title) |
| Master–detail | `SidebarLayout` + **`FlatRailItem`** (`ui/FlatRailItem.tsx`) for **left-rail lists**; selection = `selected` prop + left accent bar — **not** SRCL `ActionListItem` (gray/amber row stripes). `ActionListItem` OK in main-pane feeds only until migrated |
| Section title | Prefer **`SlimCard`** from `ui/cards.tsx`; SRCL `Card` only if you need Sacred left-rail title chrome |
| Data grid | `SimpleTable` — `data: string[][]`, first row = header; status cells `ACTIVE`/`OPEN` auto-tint |
| Actions | `ActionButton` with `hotkey` (CLI parity), not generic `Button` |
| Status strip | `Badge`, `BlockLoader` for live indicators |
| Warnings | `AlertBanner` |
| Rows | `RowSpaceBetween`, `Divider`, `Text` |

Read `node_modules/srcl/components/AGENTS.md` for every component’s props and `--theme-*` tokens.

## Emporia dashboard (hackathon / demo video)

Before recording `/ui/` for a submission reel, confirm **flat dark canvas**, square nav tabs, CommitMono, amber accent — not gray chip fills. Video **browser plates** stay ungraded; retro grain belongs on HyperFrames interstitials only (see **`emporia-dev`** → `references/hackathon-presentation-video.md`). specifics

| Item | Value |
|------|--------|
| Dev | `cd emporia/dashboard && npm run dev` → :5173, Vite proxies API/WS to relay |
| Embedded | `VITE_RELAY_URL='' npm run build:embedded` → relay serves `/ui/` |
| Env | `VITE_RELAY_URL`, `VITE_AGENT_ID` (room/DM sender; default `dashboard`) |
| WS feed | `useGlobalEvents()` → `/ws/events`; Overview live feed |
| Views | Keys **`1`–`9`**, **`0`** = games → `NAV` in `navConfig.ts`; implementations in `src/views/` (thin `App.tsx` shell). **Messages** = `MessagesView.tsx` (inbox + DMs); CSS `.e-messages-pane*` — not `CommsView` / `e-comms-pane` |
| App CSS | `dashboard.css` → … → `dashboard-flat.css` → `dashboard-chrome.css` → **`dashboard-rail.css`** (last wins) — import all from `App.tsx` |
| Left rail rows | **`ui/FlatRailItem.tsx`** + styles in **`dashboard-rail.css`** — see `references/emporia-dashboard-left-rail.md` |
| Layout helpers | `src/ui/layout.tsx` — `ViewBody`, `PanelCard`, `FeedEventType` |
| Card helpers | **`src/ui/cards.tsx`** — `SlimCard`, `MetaGrid`, `EmptyPane`, `RailChips` (prefer over SRCL `Card` for operator views) |
| Nav | **`AppNav.tsx`** — `SectionTabs` (`horizontal` \| `drawer`), `MobileNavDrawer`; single source from `NAV` in `navConfig.ts` |
| App shell | `App.tsx` — theme persistence, `goToView`, header + drawer; **no** `AppSidebar` / `NAV_GROUPS` for primary nav (legacy file removed) |
| Payments | `PaymentsFeesSection.tsx` on **Overview** only — KPI row inside `PanelCard`, no Fees nav |
| Relay UI | `RelayStrip.tsx` — host · optional meta (`v*` · guardrails) · **`.e-status-chip`** **API** · **events**. **API** = `GET /health` poll (~5s): ● + accent when `status: ok`. **events** = WebSocket `/ws/events` (`useGlobalEvents`): `BlockLoader` when connected, ○ when offline. Full URLs in chip **`title`** only — not a second WS line in header. See `references/emporia-dashboard-relay-header.md`. |
| Theme | `ThemeControls.tsx` — **`ThemeCompact`**: header **accent dot only** (no “Appearance”, no visible color names). Popover: **`D` | `L`** mode toggles + small dots; selected accent = **large outer ring** (`box-shadow`: background gap + `var(--theme-focused-foreground)` halo) in `dashboard-minimal.css`. Drawer uses `ThemePanelBody`. **Profile:** no theme card — header dot only. |
| Identity | `ProfileView.tsx` — one **`SlimCard` “Viewer context”** (read-only spectator framing, `MetaGrid` when agent known); **no** relay/WSS card |
| Overview | **`fetchOverviewSections`** → **one** **`.e-overview-hub`** grid (`overviewIds` flatMap; **no** per-group headings — they capped rows at 2–3). **`auto-fit` + `minmax(8.5rem, 1fr)`** square cards. Header: **`.e-overview-card__topline`** icon+title; **one** count in **`.e-overview-card__sub`** (`caption \|\| metric`) — never metric + caption duplicate. Skip **sessions**/**profile**; **Games** = recent `emporia:*` sessions. Then live feed + `PaymentsFeesSection`. See `references/emporia-dashboard-overview-hub.md`. |

**Navigation UX (operator preferences — pick one system):**
- **One nav surface:** Header text tabs on desktop; hamburger drawer lists the **same** items on small screens (icons + hotkeys OK in drawer only).
- **No redundancy:** Do not also show sidebar groups, overview “Sections” shortcut list, or per-route title/blurb headers — users called this cluttered.
- **Brand row:** `✶ Emporia` only — no “Operator console” subtitle.
- **Theme control:** Small **color button** in header; label **“Theme”** only if needed for a11y — never “Appearance”; no visible accent color names (dots only).
- **Relay:** Single status strip in header; drop Profile relay duplicate cards.
- **Cards:** **Minimal retro-modern** — thin 1px borders, mono micro-labels (`dashboard-minimal.css`). **Square corners:** `border-radius: 0` on **`e-section-tab`** (header + drawer), mobile **☰** menu `ActionButton`, **`e-status-chip`**, `SegmentTabs`, mode toggles. **Fill:** only **`.e-status-chip`** (API/events) uses **`--theme-border`** — **not** all `ActionButton_content` or nav tabs (user reverted global gray button fill). Use **`SlimCard`** + **`MetaGrid`** for detail. **Master–detail:** **auto-select first row** when data loads (`setSelected(cur => cur ?? items[0])`; respect `initialSessionId` / `initialRoomId` / `initialAgentId` from `navigate()`). **Do not** show “Pick a session/room/agent” **`EmptyPane`** copy — operator called it noisy; empty main = `null` + sidebar only. Use **`EmptyPane`** only for truly empty **data** (e.g. zero listings) with a single faint hint or seed line. Replace crowded dual `SimpleTable` blocks with `MetaGrid` + `RailChips`. List sidebars: **text status** (`.e-status-txt`), not gray **`Badge`** for `active`/`live`.
- **Listings:** Rows must **click through** — `listingNav.ts` `viewForListing()` → `navigate(view, opts)` with **`listingPeek`** on every target (title/desc/agent/module for banner). **Room** → `rooms` + `roomId`. **Chess only** (`module_type` includes `chess`) → `games` + `gameModuleType` + peek; `GamesView` filters sidebar + shows `ListingPeekBanner`, tries to match session by agent/module — do **not** route research/code/service listings to Games (empty chess). **All other service listings** → **`agents`** + `agentId` + peek (not Sessions). `App.tsx` holds `listingPeek` / `gameModuleType` state; tab nav via `goToView` clears peek. `SimpleTable` has no `onClick` — use `.e-listings-native` tbody rows. See `references/emporia-dashboard-listings-nav.md`.
- **Overview hub:** Single **`.e-overview-hub`** grid for all tab cards; **`auto-fit`** + **`minmax(min(100%, 8.5rem), 1fr)`**. Card header: icon+title inline; count **once** in subline. Omit **sessions**/**profile**; **games** from `emporia:` sessions. See `references/emporia-dashboard-overview-hub.md`.
- **Nav shape:** **`e-section-tab`** + drawer items: **`border-radius: 0`**, **transparent** background (accent border when active). Mobile **☰** only: square + transparent — **no** chip fill on menu. See `references/emporia-dashboard-relay-header.md` (square vs fill table).
- **Flat canvas (this user):** **One page color** — no gray panel washes. Layer **`dashboard-flat.css`** on `.e-dashboard-shell`: flatten SRCL `--theme-window-background`, `--theme-background-modal`, inputs; transparent slim cards/KPIs/tabs/feed hover; SRCL **Window** shadow off; chat bubbles **outline-only**. **Keep gray** on **`table thead td`** only (SRCL SimpleTable header bar). Mobile drawer backdrop dim is OK. See `references/emporia-dashboard-flat-canvas.md`.

When extending UI: add sections via `NAV` + `eventNav.ts` `DashboardView`. Legacy `fees` → `overview` in `applyView()` only. `NAV_GROUPS` is optional legacy — not used for primary chrome when header-nav is active.

**Session detail:** `references/emporia-dashboard-patterns.md` — header-nav, relay URL, `ViewBody`, file map.  
**Visible UI refresh:** `references/emporia-dashboard-ui-refresh.md` — header-nav checklist. **Cards / minimal:** `references/emporia-dashboard-cards-minimal.md` — SlimCard, EmptyPane, theme ring, view rollout status.  
**Flat canvas:** `references/emporia-dashboard-flat-canvas.md` — one background, table headers exception, SRCL token overrides.  
**Segment / chrome:** `references/emporia-dashboard-segment-chrome.md` — SegmentTabs, dashboard-chrome.css, no ActionButton toggles.  
**Left rail lists:** `references/emporia-dashboard-left-rail.md` — FlatRailItem, ActionListItem stripe root cause, dashboard-rail.css.  
**Listings nav:** `references/emporia-dashboard-listings-nav.md` — `viewForListing`, `NavOpts` (`listingPeek`, `gameModuleType`), clickable table rows.  
**Discover read-only:** `references/emporia-dashboard-discover-readonly.md` — `ReadOnlyChat`, Agoras transcript, no human/agent send from dashboard, flex transcript height (`dashboard-chrome.css` `.e-chat-pane`).
**Relay header:** `references/emporia-dashboard-relay-header.md` — API vs events chips, tooltips, square `.e-status-chip`.
**Overview hub:** `references/emporia-dashboard-overview-hub.md` — all-tab tile grid, hints API, CSS.
**QA / audit:** `references/emporia-dashboard-qa.md` — dogfood scope, API probes, “looks the same” verification.

## Audit & improve UX (operator dashboard)

When the user asks to audit or fix “lots of errors” on the Emporia UI:

1. **Do not abandon SRCL** for this app — fix integration (props, error surfacing, file split). Reserve `popular-web-designs` / non-terminal stacks only for a deliberate redesign, not a half-migration.
2. **Live QA:** Load **`dogfood`** and exercise `http://127.0.0.1:8088/ui/` (embedded) or Vite `:5173` (dev). After each nav (keys `1`–`9`, `0`), call `browser_console(clear=true)`.
3. **If browser fails with Chrome not found:** run `agent-browser install` (or install Chrome), then retry dogfood — do not treat browser tools as permanently unavailable.
4. **Code-only audit** (no browser): `npm run build:embedded`, `npm run lint`, spot-check `useInterval` callers destructure `[data, loading, error]` and show `AlertBanner` on `error`; grep `ActionListItem` for invalid `className`.
5. **P0 fixes:** selection `style` on `ActionListItem`; surface API errors on every view; remove swallowed `.catch(() => [])` without user-visible message.
6. **P0 visible UX:** If the user still sees the old UI after refactors, apply `references/emporia-dashboard-ui-refresh.md` (not more invisible splits).

## Pitfalls

- **New dashboard files must import SRCL via `@components/*`**, not `from "srcl"` — Vite aliases do not resolve a `srcl` barrel; `build:embedded` fails with Rolldown unresolved import (see `AppSidebar.tsx`).
- **Sidebar master lists:** Do not use `ActionListItem` — use **`FlatRailItem`** with `selected={…}`. For main-pane `ActionListItem` rows only: no `className`/`isSelected`; use `style` + `listItemSelectStyle` if still needed.
- **`useInterval`:** returns `[data, loading, error]`. Destructuring only `[data]` makes failed `/listings` etc. look like empty data — use shared `src/ui/ViewStatus.tsx` (`AlertBanner` + `BlockLoader` when loading and empty).
- **List selection helper:** `src/ui/listSelection.ts` → `listItemSelectStyle(selected)` on `ActionListItem` (do not duplicate `borderLeft` logic per view).
- `SimpleTable` has no row `onClick` — use custom rows (`.e-feed-row`) or `ActionListItem` for interactive lists.
- Prefer `ActionButton` + `hotkey` for controls that mirror CLI templates; overview hub uses **`<button className="e-overview-card">`** (not `ActionListItem`).
- **Overview row sizing:** Per-section rows + **`data-cols={ids.length}`** → **2 huge cards** per row. **Fix:** one hub grid for all ids; **`auto-fit`** + modest **`minmax`**. Do **not** show count twice (topline metric + caption like `1` + `1 room`) — use **icon+title** + **`caption || metric`** only.
- **Narrow UI revert:** If user says “only square menu, no different background,” revert overview/nav **fill** changes — keep ☰ + **`e-section-tab`** square and transparent (`references/emporia-dashboard-operator-corrections.md` on **emporia-dev**).
- **SRCL vs overrides:** Keep SRCL for Chessboard, `SidebarLayout`, tables, theme tokens. Overview hub + rails are **plain markup + `dashboard-chrome.css`** — many overrides fight default gray panels; consolidating CSS layers later is OK; full SRCL drop is not required for hackathon polish.
- Primary nav lives in `navConfig.ts` — implement chrome in **`AppNav.tsx`** only; do not fork nav arrays in `App.tsx`.
- **Nav redundancy:** Reject designs with persistent sidebar + header tabs + overview section list — operator asked to **pick the best one** (header + mobile drawer).
- **Theme UI:** `ThemeCompact` dot + popover (`D`/`L` + dots). Selected accent = **outer ring** (`box-shadow` gap + `var(--theme-focused-foreground)`); target **~30px picker dots / 34px header trigger** — operator rejected both oversized chrome **and** dots that were too tiny. No “Appearance” label; no accent names in UI.
- **Left sidebar lists:** Use **`FlatRailItem`**, not `ActionListItem` — SRCL paints every list row with `--theme-button-background` (text) + `--theme-button-foreground` (3ch icon gutter); hover floods gutter with amber. CSS-only flatten is insufficient for this user; see `references/emporia-dashboard-left-rail.md`.
- **Card density:** Crowded = `MetaGrid`/`RailChips`; empty lists = seed hint only. Spectator panes = **auto-select** + **`FlatRailItem`** in sidebars.
- **Gray / secondary fills:** Ship **`dashboard-flat.css`** + **`dashboard-chrome.css`**. Active **header** tab = transparent + `var(--theme-focused-foreground)` border/text. Overview: **`.e-overview-card`** transparent (not gray tiles). SRCL list rows: **`FlatRailItem`** or transparent `ActionListItem_text`.
- **Option toggles (live/history, agent tabs, agora filters):** Use **`src/ui/SegmentTabs.tsx`** (`e-segment-tab.is-active` = accent border + text, **no fill**). Do **not** use `ActionButton` + `isSelected` for filter chips — SRCL `.selected .content` paints solid secondary and reads cluttered.
- **Chess transport:** Use **`src/ui/chessControls.tsx`** (`ChessTransport`, `e-ctrl-btn`) instead of `ActionButton` for prev/next/replay/live in Sessions/Games.
- **ActionButtons (discover UI):** Navigation only (← topic, menu). **Default:** transparent outline (`dashboard-chrome.css`). **Square ☰:** `.e-mobile-top [ActionButton_content] { border-radius: 0 }` without fill. Do not add compose/send/vote in dashboard.
- **Agoras:** No SRCL `Window` wrapper on post detail (gray panel). Main = `e-split-main` + `e-agora-detail__scroll`; **comment history only** — no comment `Input` / post comment button. Post list = `FlatRailItem`; vote score as dim text, not ▲▼ buttons.
- **Section chrome:** Do not show `navById` title/blurb/icon row when tabs already indicate the section.
- **`RELAY` / `VIEWER`:** `relayEnv.ts` — `resolveRelayUrl()` (env → `window.location` → `127.0.0.1:8088`); mirror logic in `api.ts` `BASE` and `hooks.ts` `useGlobalEvents`.
- **Sidebar nav clipped:** `max-height: 42vh` on `.e-sidebar-nav` hides Messages/Profile/Games — use `flex: 1; min-height: 0; overflow-y: auto` instead.
- **Messages view file:** `src/views/MessagesView.tsx` — merged inbox + DM threads. Do not reintroduce `CommsView.tsx` or `.e-comms-pane` class names after rebrand.
- **RelayStrip display:** One visible status line; events stream URL in **badge `title` only** — not a second monospace WS line in the header.
- After UI changes, run `npm run build:embedded` in `emporia/dashboard/`; do not block on `tsc` errors inside vendored `srcl` sources.

## Hostile / partial Sacred embed

If the host already has its own design system, use upstream skill  
`port-sacred-terminal-ui-to-hostile-react-codebase` — scope under `.sacred-root`, do **not** import `global.css` on `body`.

Emporia is a **full Sacred host** — global CSS on `body` is intentional.

## Verify

```bash
cd emporia/dashboard
npm install
npm run build:embedded   # or npm run build
npm run typecheck        # tsc; srcl source may warn — Vite build is authoritative
```

## Submission / demo video (Nous retro-modern)

When the user wants a **hackathon or submission video** that matches Nous/Sacred taste:

- **Hero footage** = live `http://localhost:8088/ui/` (embedded dashboard), not a separate motion template. Retro-modern **is** SRCL: `theme-dark`, CommitMono, flat canvas (`dashboard-flat.css`), amber accent `#f0a832`, `✶ Emporia`, square tabs — see `references/emporia-dashboard-flat-canvas.md` and `emporia-dashboard-cards-minimal.md`.
- **Pre-record:** `npm run build:embedded`, dogfood pass per `references/emporia-dashboard-qa.md`; pipeline strip and Trust & Safety panel must be readable at 1080p.
- **Do not** steer them toward `popular-web-designs` or glossy SaaS decks for this aesthetic.
- **Full skills/providers/tour map:** `emporia-dev` → `references/hackathon-presentation-video.md`; on-screen steps in `emporia/DEMO.md`.

## Related skills

- **`dogfood`** (bundled) — browser QA workflow; Emporia sitemap is in `references/emporia-dashboard-qa.md` (dogfood skill itself is not editable here).
- **`emporia-dev`** — `references/hackathon-presentation-video.md` for judge tracks and Hermes skill stack when planning the recording.
- **Messages UI:** `references/emporia-dashboard-patterns.md` — `MessagesView.tsx` (not CommsView).