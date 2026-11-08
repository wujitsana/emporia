# Review pass, 2026-11-06: the dashboard's UI (pass 2)

An independent read of the dashboard on `preview-2` (2eeb6f1): layout, accessibility, state handling,
consistency, the visibility-only rule and bundle health, plus what would read best in a 90-second demo.
Done on branch `review2-ui`. Nothing here is pushed or merged.

Small, confident fixes are committed, each with a test that fails without it. Design calls and anything
larger are **proposals** (§ 5).

**How it was checked.** Headless Chromium stops here with "No usable sandbox" and is not run without its
sandbox, so nothing was looked at in a browser. Instead:
- the source and every stylesheet were read (dashboard.css, the chat and kit modules, badges.css);
- the whole app was rendered in jsdom, offline, at a phone's and a desktop's width (`appShell.test.tsx`);
- the CSS was walked with postcss (`frame.test.tsx`, `textContrast.test.ts`);
- contrast was computed from the CSS token formulas (OKLCH → sRGB, both gamut treatments) for both themes
  and all eight tints;
- the source was searched for every non-GET request and every socket send (`visibilityOnly.test.ts`).

What a browser would show at 390, 768 and 1280 px (§ 3) is therefore read from the CSS, not measured.
Look at those widths in the sandbox tab before filming.

## 1. Findings

| # | Severity | Where | Scenario | Status |
|---|---|---|---|---|
| U1 | Medium | `App.tsx` | A route to a section the viewer may not see (`#/more/operator`, `#/more/relay` for someone who does not run the relay; `#/market/fees`, `#/talk/messages` for a spectator) drew the heading over an empty page. A spectator's `#/talk/messages` rendered the inbox view. | Fixed, d6e665b: `ui/GatedNote.tsx` says whose section it is. No redirect: the operator's sections open only after the signed whoami answers, so a redirect would bounce the real operator. |
| U2 | Medium | `views/OverviewView.tsx` | Home's section cards ignored the nav's visibility rule. **Every viewer saw an Operator card**, and a spectator a Messages card; both led to U1's empty page. | Fixed, cad8cdd: cards go through `navConfig.canSee`. |
| U3 | Medium (a11y) | `App.tsx`, `chat/ChatDrawer.tsx` | The phone menu and the chat took no focus when opened and gave none back. With the menu open over full-page chat, **Escape closed the chat underneath, not the menu** (the focus had stayed on the chat's ☰). Tab walked into the view behind both overlays. | Fixed, d6e665b: opening moves the focus in (the chat: its composer), closing gives it back; what an overlay covers is `inert`; widening the window closes the menu, so the view is never left inert (`ui/overlayFocus.ts`). |
| U4 | Medium (a11y) | dashboard.css, chat module | 17 text rules still dimmed by opacity (0.4–0.55) outside the look's faint rule: detail-panel labels (`.e-meta-grid dt`), Home card lines, empty-state hints, the chat's speaker/tool-time/session lines, a read inbox row, chess move numbers. On light green 0.45 is **2.9:1** and 0.55 **4.0:1** (AA is 4.5). | Fixed, ff96bcb: they take the look's faint colour (6.1:1 at worst). `textContrast.test.ts` fails on any new text rule under 0.6 opacity the look does not cover. |
| U5 | Low | chat module, dashboard.css | iOS zooms the page when a field under 16 px takes focus: the chat's clarify answer box (13.8 px) and the viewer select in the phone menu (12 px). The composer was already 16 px. | Fixed, 4bb0bea. |
| U6 | Low | `hooks.ts` | `useRoomWs` returned a `send()` on the room's socket. Nothing called it, but it was the one write path to a relay socket left in a dashboard that only watches. | Fixed, 71db0c0: removed. `visibilityOnly.test.ts` holds the rule over every source file. |
| U7 | Low (a11y) | `ui/McpCommandHint.tsx` | The "your agent would run" hints (Games, Agents, Profile, a listing's peek) copied only on a click of the `<code>`, which a keyboard cannot reach, and "copied" was not announced. | Fixed, 6feda21: a copy button, as `CopyCommand` has, and a status line. |
| U8 | Low (a11y) | `views/GamifyView.tsx` | A craft row on a character sheet opened its ladder on a click only. | Fixed, 6feda21: focusable, Enter/Space, named. |
| U9 | Low | `desktop/views.tsx` | The Desktop page's turn notification opened `#/games/<id>`, a v1 route; everything else on the page writes v2. | Fixed, 8a9d974. |
| U10 | Low | `views/OverviewView.tsx` | In a Hermes profile whose plugin host carries no sockets (by design), Home said "Event stream reconnecting…" forever. | Fixed, cad8cdd: it says the live feed is off there and the page refreshes on a timer. |
| U11 | Low | Desktop page, Hermes tab | Every breakpoint is a `@media` query on the **window**. On the Desktop page the pane is narrower than the window (Desktop's sidebar), so a 1000 px window gets the desktop header in a ~740 px pane: brand, six tabs and the actions wrap to two or three rows. The same holds in the Hermes tab if Hermes draws a sidebar. | Proposal P4. |
| U12 | Low | dashboard.css § 4, chat module | At 1280 px with the chat open beside the view (min(27rem, 40%) = 432 px), the header keeps 848 px for brand + six tabs + four controls, about 1000 px of content, so it wraps to a second row when the chat opens. | Proposal P5. |
| U13 | Low (a11y) | `ui/SetPlayPanel.tsx`, `ui/BargainPanel.tsx` | The "whose turn · 0:43 · 3 offers left" line is `role="status"` and re-renders on every 4 s poll, so a screen reader re-announces the clock every 4 s. | Proposal P9. |
| U14 | Info | dashboard.css | **Light green**: the look's graphics accent (`--anim-line`, brackets and the active sub-tab underline) is 2.89:1 on the canvas, under 3:1 for UI graphics. The active sub-tab is also bold, so its state does not rest on the line alone. | Proposal P10. |
| U15 | Info | dashboard.css, badges/tokens.ts, frame.test.tsx | Dead code left from earlier layouts: 111 classes in dashboard.css that no source file names (the left sidebar, the theme popover and its backdrop, the theme panel, accent swatches, nav cards, the stat strip, vote buttons, the relay strip, …). `frame.test.tsx` still asserts the dead theme-popover backdrop is the only `position: fixed`. `badges/tokens.ts` keeps an unchecked retromod branch. § 24 is still headed "RETROMOD". The `s` key and the classic style are gone from the code. | Proposal P8. |

Checked and found sound:
- **The user's five reports.** The chat drawer is frame-tall with the composer pinned; full-page chat has
  "← back to the dashboard" and a ☰ that opens the menu above it (z 140 over 120); the menu, its backdrop
  and full-page chat are absolute inside the frame's `contain: layout paint`, so nothing reaches Hermes's
  page; six destinations; the composer is one 42 px line. Inside the Desktop page `.e-desktop-root` is a
  second contained frame, and the page draws no chat (Desktop's own chat is the agent's).
- **Visibility-only.** The only non-GET requests are the local console's `…/open` routes (sealed text opened
  with the viewer's own key, on this machine) and the dashboard sign-in challenge. The console itself
  refuses every non-GET to the relay (`console/app.py`). Socket sends are keep-alive pings; the chat's
  socket is Hermes's own chat, not the relay's. Approvals in the chat go to Hermes, not the relay.
- **Keys while typing.** `isTyping` looks at the focus inside the closed shadow root as well as the
  event's target, so section keys never fire in the composer, the docs search or a select. On the Desktop
  page only keys pressed inside the page count.
- **Motion.** The brand decode, the heading scramble, the typed line, the beat flash and the panel rise
  are all off under `prefers-reduced-motion` and in `?capture=1`; screen readers read the words, never
  the glyphs.
- **Badges.** `role="img"`, the badge's human name as the label, the earn line as the tooltip; the svg
  is hidden from assistive tech. Their colours derive from the look's accent (`badges.test.tsx`, ≥ 3:1).
- **Focus ring.** One 2 px ring in the accent everywhere (≥ 6:1, below).
- **States.** Season says when the relay does not run the gamify module, and when the season, a ladder,
  a sheet or a list did not load ("not an empty list"); Home marks a collection that failed apart from one
  that is empty. An older relay without `relay_modules` lands on "does not run the gamify module".
- **Old routes.** Every v1 route still resolves (`eventNav.test.ts`); an unknown one opens Home.
- **Wording.** "testnet escrow" in the Deals blurb; Bargain says "no stakes"; no money promises found.

## 2. Contrast (the look, both themes × eight tints)

Computed from the CSS formulas, both gamut treatments (clip and CSS Color 4 mapping); the worse is shown.
Worst case across the 16 combinations, then where it occurs:

| Pair | Needs | Worst | Where |
|---|---|---|---|
| text on canvas | 4.5 | 16.55 | light red |
| faint (68 % ink) | 4.5 | 6.13 | light green |
| status lines, key hints (80 % ink) | 4.5 | 9.34 | light green |
| accent as text (links, "connected", small buttons) | 4.5 | 6.06 | light green |
| canvas on accent (unread badge, send button) | 4.5 | 6.06 | light green |
| ink on fill (active tab, chips, caption bands) | 4.5 | 7.25 | light red |
| text on the wash (selected row, winning seat) | 4.5 | 11.34 | light pink |
| accent as a UI graphic (focus ring, borders) | 3 | 6.06 | light green |
| graphics line (brackets, sub-tab underline) | 3 | **2.89** | light green (U14) |
| inactive tabs (0.8 opacity) | 4.5 | 9.34 | light green |
| chat meta line (0.62 opacity) | 4.5 | 5.00 | light green |
| text at 0.6 opacity (`.e-note`, typeahead meta) | 4.5 | 4.68 | light green — passes, narrowly |
| text at 0.55 / 0.5 / 0.45 / 0.4 (before U4) | 4.5 | 3.98 / 3.41 / 2.94 / 2.55 | light green |

The dark theme is comfortable everywhere (faint ≥ 7.8, accent ≥ 7.5). `textContrast.test.ts` now checks
the token rows above for every theme, tint and gamut.

## 3. The layout at 390, 768 and 1280 px (read from the CSS)

- **390** (phone): one top row (☰, brand + section, chat, relay chip; wraps if it must); the section
  strip under it with 44 px tabs; split views list-then-detail (≤ 700 px) with a 44 px "← back to list";
  the heading's title 1.55 rem. Full-page chat covers the frame with its back row; the composer is 16 px
  text in a 42 px line. The menu is min(85 %, 16 rem) wide above a 55 % backdrop.
- **768** (tablet, a narrow Hermes tab): still the phone header and full-page chat (≤ 900 px), but split
  views sit side by side (> 700 px): a 22–30 ch rail beside the detail.
- **1280**: one header row: brand, six tabs, then theme, viewer, chat and relay. The chat opens beside the
  view; the header then wraps (U12).
- **Inside the Desktop page**: the same rules keyed to the window, not the pane (U11).

## 4. What the fixes changed, file by file

- `App.tsx`: the gated note; the menu's focus and `inert`; closing the menu when the window widens.
- `ui/GatedNote.tsx`, `ui/overlayFocus.ts` (new), `navConfig.canSee` (new).
- `chat/ChatDrawer.tsx`, `chat/AgentChat.tsx`: focus on open and back on close; `covered` while the
  menu is above.
- `views/OverviewView.tsx`: the cards' visibility; the live-feed line.
- `dashboard.css`, `chat/ChatDrawer.module.css`: faint as a colour; 16 px fields on a phone.
- `hooks.ts`: no `send` on a room socket. `ui/McpCommandHint.tsx`: a copy button. `views/GamifyView.tsx`:
  the craft rows. `desktop/views.tsx`: the v2 route.
- Tests: `appShell.test.tsx`, `textContrast.test.ts`, `visibilityOnly.test.ts`, `ui/McpCommandHint.test.tsx`
  (new); additions to `chat/ChatDrawer.test.tsx`, `frame.test.tsx`, `views/GamifyView.test.tsx`,
  `desktop/page.test.tsx`.
- The tab, Desktop and console bundles are rebuilt (`npm run build:plugin`).

## 5. Proposals, ranked by value for the demo over effort

| # | Proposal | Value | Effort |
|---|---|---|---|
| P1 | **Season lands on the boards on a phone.** Season's rail opens first below 700 px (`initialPane` is "list" unless an agent was linked), so a phone shows "Season · Quests · Bug reports …" before any board. Open the detail (the season's boards) by default; the rail is one "← back" away. | High: the second thing filmed | Tiny (`GamifyView` `initialPane`) |
| P2 | **A first frame for Home.** Home opens on the intro, "your agent now", a status line and a long pipeline sentence before the cards. For the video: lead with "your agent now" and the live game, fold the pipeline sentence into a one-line chip, and give the cards the brackets the panels have. | High | Small |
| P3 | **Show the badges where the agent is.** Badges live in Season's sheet and More › Badges. A strip of the viewer's earned badges in Home's "your agent now" (the same `PixelBadge`, size 2, with the earn pop) puts the gamify layer in the first five seconds. | High | Small |
| P4 | **Breakpoints from the frame, not the window** (U11). Give `.e-app-shell`'s child a `container-type: inline-size` and move the 900 / 700 px rules to `@container`; `isNarrow()` reads the frame's width with a ResizeObserver. Fixes the Desktop pane and a Hermes tab beside a sidebar. | Medium | Medium (three stylesheets, one hook) |
| P5 | **The header when the chat opens** (U12). Below ~1400 px with the chat open, drop the tabs' text to their glyphs or the chat button's label, so the header stays one row and nothing jumps when the chat slides in on camera. | Medium | Small |
| P6 | **A "testnet" chip in the header** beside the relay chip, from `/health` (`x402_escrow_network`), so every frame that shows an amount also shows it is test money. | Medium (and it protects the no-money-promises line) | Small |
| P7 | **Minify the tab and Desktop bundles.** They ship unminified (`minify: false`): the tab is 1.43 MB (316 kB gzip), the Desktop page 966 kB; the console, minified, is 714 + 222 kB. If the catalogue does not need readable bundles, minifying roughly halves them and speeds the first paint of the tab. | Medium | Tiny, if allowed |
| P8 | **Remove the dead CSS and code** (U15): the 111 unreferenced classes (found by a search for each class name in the source; re-check each before removing it, since some names are built at runtime), the theme popover and its stale `frame.test.tsx` assertion, the retromod branch of `badges/tokens.ts`, the "RETROMOD" heading of § 24. About a fifth of dashboard.css. | Low for the demo | Small–medium (verify each class) |
| P9 | **Quieter live regions** (U13): keep `role="status"` on the verdict, and take it off the clock line (or announce only a change of turn). | Low | Tiny |
| P10 | **Light green graphics line** (U14): `--anim-line` at L 0.58 under the green tint (or for all light tints), which takes it past 3:1 with almost no visible change. | Low | Tiny |

Not proposed: a different navigation. The six destinations work; the fixes above keep them.

## 6. Checks

Run in this worktree (its own `.venv` repointed at its `src`, and the main checkout's
`dashboard/node_modules` symlinked, both gitignored):

- `npx vitest run`: 57 files, 389 tests passed (preview-2: 53 files, 370 tests).
- `npx tsc --noEmit -p tsconfig.app.json`: clean.
- `npx oxlint --max-warnings=0`: clean.
- `npm run build:plugin`: tab, Desktop page and console built; the staleness tests pass.
- `tests/test_dashboard_plugin.py`: 11 passed.
- `python3 scripts/secret_scan.py --range preview-2..HEAD`: clean.

Not run: anything in a browser (above); the live relay, the gateway and the sandbox were not touched.
