# Dashboard UI audit: retromod pass

Branch `retromod`. This audit came before the changes; each finding is marked with what was done
about it.

## Direction: retromod

We keep the retro terminal identity: the vendored kit's look, monospace throughout, a
box-drawing feel, and the existing light/dark themes with their eight tints
(`themeConfig.ts`). On top of that goes modern polish: clean spacing and a clear hierarchy,
smooth readable type, visible focus and state, and comfortable touch targets on a phone. Nothing
should feel like a dated imitation of an old terminal. Nothing new comes from the network:
fonts stay local (`postcss.noRemoteFonts.ts`), and the pass adds no design system and no
dependency.

The dashboard stays visibility-only. Every control this pass adds changes what you are
*looking at* (a section, a list or detail pane, a menu). None of them changes the relay.

## How it was checked

- Headless Chromium (the copy Hermes ships with) over the DevTools protocol, at 390×844 and
  1280×800. It ran against the dev server and a small read-only fixture relay (not committed)
  that serves agents, sessions, listings, the season, sheets and bug reports, so populated
  views could be seen. Screenshots: `docs/ui-retro-shots/`.
- Each shot also measured the page's scroll width against its viewport width, to catch
  horizontal page scroll. No shot after the pass scrolls sideways, at 390px or at 1280px.
- **The Hermes tab itself:** the committed bundle (`hermes_plugin/emporia/dashboard/dist`) was
  loaded into a stand-in Hermes page. A stub of the plugin SDK mounted it into its *closed*
  shadow root, just as Hermes does. The list-then-detail panes, the menu and the agent picker
  all worked there (`after-tab-*.png`).
- Files in `docs/ui-retro-shots/`: `before-*` is the branch point and `after-*` is this branch.
  Each name gives the section and the width.

## Findings

### Broken (fixed)

1. **Every kit override in `dashboard.css` was dead.** After the kit was vendored (P283), CSS
   modules named classes `_root_1gl67_1`, with no component name. Every
   `[class*="SidebarLayout_…"]`, `[class*="ActionButton_…"]` and `[class*="Badge_root"]`
   selector (about 30 of them) matched nothing. Three things followed:
   - **On mobile the split views never collapsed.** The "stack SidebarLayout under 700px" rule
     never applied. At 390px Gamify, Sessions, Agents, Rooms, Messages, Docs and Settings showed
     a 20–30ch rail beside a crushed detail pane. On Sessions the chessboard was cut off at
     column A.
   - The flat outline style meant for buttons never applied either, so the kit's grey fills
     showed through.
   - The mobile **menu** button had the same problem.
   → Fixed: kit module classes are now named `<Component>_<local>_<hash>` in the dev build, the
   console build, the tab build and the tests (`src/cssModuleName.ts`, with a test).
2. **A view that throws blanks the whole app.** There was no error boundary, so one bad
   payload left a white page, header included, until a reload.
   → Fixed: each view renders inside a boundary that names the section and keeps the nav working.
3. **Section hotkeys fired while typing in the Hermes tab.** In the closed shadow root a
   window key listener sees the host element as the target, so "type `2` into the agent
   search" switched the section to Listings.
   → Fixed: the app asks its own root node what has focus (`src/keyTarget.ts`, with a test
   that uses a closed shadow root). Modifier keys never trigger a section, and Escape closes
   the menu.
4. **The agent search box drew a second, empty box.** The kit Input passes `className` to its
   transparent overlay `<input>`, and `.e-search-bar` gave that overlay a border and padding.
   → Fixed: the box is drawn on the display layer, and the overlay exactly covers it.
5. **Dev proxy gaps.** `vite.config.ts` did not proxy `/relay`, `/season`, `/bugs`,
   `/security`, `/ratings`, `/results`, `/leaderboard`, `/bounties`, `/likes` or `/operator`.
   Under `npm run dev` the Gamify tab said "the relay did not answer".
   → Fixed: they are now proxied.

### Confusing for a first-time viewer (fixed)

6. **No page title anywhere.** `ViewBody` says the "title lives in app top bar", but the top bar
   shows only the brand. On mobile the section tabs are hidden too, so nothing on screen says
   which section you are in or what it is for.
   → Fixed: every section opens with a heading: its icon and name (`★ GAMIFY`) and, below them,
   one plain line on what it shows. Both come from the nav's own label and blurb. On a phone the
   top bar also names the current section under the brand.
7. **Fourteen flat tabs with no grouping.** `NAV_GROUPS` existed but nothing rendered it.
   "Sessions", "Agoras" and "Gamify" mean nothing to someone new, and the Gamify blurb still
   described only character sheets.
   → Fixed: the tabs and the menu now follow the groups (Start, Market, Community, You,
   Help), with a quiet rule between groups on desktop and group names in the menu. The blurbs
   were rewritten in plain words. The Gamify blurb leads with the main game: improving Emporia
   (accepted bug reports), then Renown from likes, then games. Gamify now opens on the Season,
   whose boards come in that order. The ids, hotkeys and tab labels are unchanged, so links and
   muscle memory still work.
8. **The Overview opens with internals:** "Every inbound action: Guardrails → Ed25519 →
   Stripe gate → …". Nothing says what Emporia is or that this screen only watches.
   → Fixed: a short "what you are looking at" box comes first. It says what Emporia is, that
   this console only watches, and where to go first (Gamify, Listings, Sessions). The
   pipeline line stays below it, quieter.
9. **Empty states that say "nothing" but not why** ("No reports yet.", "Nobody on this board
   yet.", "Select a conversation.").
   → Fixed for the Gamify boards and lists, Messages, Sessions and the Overview's live
   activity: each now says what would appear and what makes it appear. Rail sections also got
   small labels ("Which game", "Character sheets").
10. **Status chips "API" and "events"** read as jargon.
   → Fixed: they read "relay" and "live", with the full meaning in the tooltip.

### Mobile (fixed)

11. **The mobile top bar wrapped onto two rows**: menu, brand, theme toggle, viewer switcher,
   relay host and two chips. It took about 90px before any content.
   → Fixed: one row holding the menu button, the brand with the current section beneath it,
   and the relay/live chips. The theme control and the viewer switcher moved into the menu.
12. **Split views on a phone** (see 1).
    → Fixed: list first, then the detail on its own screen with a `← back to list` control.
    Tapping a row in the rail opens its detail. Filters (the game types on Sessions) keep you
    on the list and show as a row of chips. A deep link (from the Overview feed or another
    section) opens straight onto the detail. Desktop is unchanged: rail beside detail. This
    lives in the kit's `SidebarLayout`, so all nine split views get it.
13. **Tables squeezed a column to one character per line.** Listings' price column ran down
    the page "$ / U / e / ( / S".
    → Fixed: on narrow screens, tables scroll inside their own box and short cells do not wrap.
14. **Tap targets** for rail rows, segment tabs, section tabs and the menu were 24–34px tall.
    → Fixed: on a phone or touch screen, rail rows are 48px, menu items and buttons 44px, and
    segment tabs and kit buttons 40px.
15. **The menu drawer** had no way to close it other than the backdrop.
    → Fixed: it now has a close control, Escape closes it, and it lists sections in their groups.

### Consistency and polish (fixed)

16. Focus was invisible on most controls, since `outline: none` was set on rail items.
    → Fixed: one `:focus-visible` ring in the accent colour everywhere.
17. Section headings inside views mixed kit `Text` with ad-hoc inline sizes.
    → Fixed: the Gamify view's headings use one shared heading class
    (`e-subhead`: small caps, a box-drawing rule). Other views can adopt it later.

### Not done (deliberately)

- **No new tab labels or ids.** Renaming "Sessions" to "Games" would change hash routes,
  hotkeys and the event-to-view mapping that agents and tests rely on. Instead, the blurbs and
  headings explain the labels.
- **No container queries.** Media queries work inside the shadow root, and the Hermes tab is
  nearly full width on a phone. Container queries would also make the shell the containing
  block for the fixed menu drawer.
- **No redesign of individual views** (Fees, Relay, Profile, the chess panel) beyond the shared
  fixes. They now reflow because the shared layout and tables do.
- **The desktop sidebar's resize handle** stays off, as before.
