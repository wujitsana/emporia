# Pixel badges

Branch `badges`, on top of `preview-all`. A set of small pixel-art badges for the dashboard, drawn in
the animatic look (`docs/ui-style-experimental.md`). Everything is in `dashboard/src/badges/`, plus a
dev gallery page. **No view uses them yet**: §5 lists where each one belongs, for the ui-v2 work to
wire in.

The rendered sheet is `docs/ui-badges-shots/badge-sheet.png`: every badge at 1×, 2× and 3× on the
light green canvas, then the whole set on light and dark in all eight tints. Headless Chromium will
not start in the build sandbox, so the sheet was drawn with Pillow from the same grids and the same
colours `tokens.ts` computes. It is not a browser screenshot.

## 1. The format

A badge is a grid, one string per row, in `badges/glyphs.ts`:

```ts
export const SEALED = [
  "............",
  "....####....",
  "...#....#...",
  "...#....#...",
  "...#....#...",
  ".##########.",
  ".#........#.",
  ".#...++...#.",
  ".#...++...#.",
  ".#...++...#.",
  ".##########.",
  "............",
];
```

- `.` is transparent, `#` is ink and `+` is accent. Any other character is a source error, and so is
  a ragged grid (`parseGrid` throws; the catalogue falls back rather than crash).
- Badges are **16×16**. The small inline marks (games and status) are **12×12**.
- `format.ts` turns a grid into one `<rect>` per horizontal run, in two `<g>`s (ink, then accent),
  inside an SVG with `shape-rendering="crispEdges"`. The `viewBox` is the grid. The width and height
  are the grid times a whole-number scale: 1×, 2× or 3×.
- Numbered badges (Renown levels, board ranks, the season) draw a 3×5 numeral into a frame
  (`withNumeral`). Two digits fit, and above 99 the badge shows `9+`.

## 2. Colour

There are two roles at most, and no background:

| Role | Paint | Comes from |
|---|---|---|
| ink | `currentColor` | the text colour where the badge sits (`--theme-text`) |
| accent | `var(--e-badge-accent, currentColor)` | the tint, in `badges.css` |

`--e-badge-accent` is set on the element that carries the theme class: `<body>` in the console, and
the app's container in the Hermes tab and on Desktop. So it follows light and dark, the tint, `t`/`T`
and the kaleidoscope run with no re-render:

```css
.theme-light { --e-badge-accent: oklch(from var(--tint, #f0a832) 0.45 min(c, 0.2) h); }
.theme-dark  { --e-badge-accent: oklch(from var(--tint, #f0a832) max(l, 0.78) min(c, 0.22) h); }
```

These are the animatic style's accent-as-text formulas (`--theme-focused-foreground` in
`styleAnimatic.css`). They are restated here so the badges also read in the retromod style, whose
light accent is too bright to sit on white. Once ui-v2 makes animatic the only style, the two rules
can become `--e-badge-accent: var(--theme-focused-foreground)`.

The ink carries every shape. The accent marks the part that was earned or is live: the filament,
the seal, the needle, the won field. So a badge still reads in the one tint where ink and accent are
close in lightness (dark yellow).

**Checked like the style work (WCAG non-text contrast, at least 3:1).** `badges.test.tsx` computes
every colour the way the CSS does: OKLCH relative colour from each tint, under both CSS Color 4
gamut mapping and plain clipping. It then checks ink and accent against every surface a badge can
sit on: the canvas, a panel, and in the animatic style the selected-row wash. That covers both
styles, both themes and all eight tints, 32 combinations.

- The lowest ink contrast is 11.4:1 (retromod, dark, no tint, on a panel).
- The lowest accent contrast is 4.8:1 (animatic, light red, on the row wash, clipped). Every accent
  is at least 5.2:1 on the canvas and on panels.

**On a fill** (a chip such as `.e-now__label`, a caption bar, a won cell), the accent would meet the
fill. That pairing is about 1:1 in dark mode. So pass `mono`: the accent is drawn in `currentColor`,
and the badge is ink on the fill, at least 7.2:1 in every tint.

## 3. The component

```tsx
import { Badge } from "./badges";       // or PixelBadge, beside the kit's text Badge

<Badge id="well-travelled" />                         // 2× by default
<Badge id={quest.id} size={1} title={`earned ${quest.at}`} />
<Badge id={sheet.class ?? "class-none"} size={3} pop />
<Badge id="live" size={1} mono />                     // on a chip
```

- `id`: anything the relay or the API says. See the catalogue (§4). An unknown id, `null`, a number
  or an empty string gets the generic dotted badge named after the id. It never throws.
- `size`: `1 | 2 | 3`. Anything else reads as 1×.
- `title`: the tooltip's second part. The default is the catalogue's one-line "how it is earned".
  The tooltip is always `Name — line`.
- `role="img"` and `aria-label` are the human name. The SVG inside is `aria-hidden`.
- `pop`: the earn animation, a three-step pixel pop in 0.32 s. It is off under
  `prefers-reduced-motion`, and in capture mode (`?capture=1`) the class is never added.
- `mono`: ink only, for a badge on a fill.

It only shows. It has no handler and makes no request, as the dashboard is visibility-only.

## 4. The catalogue (`badges/catalog.ts`)

The ids come from `preview-all`, which includes season 1, and from the season-2 branches still being
built (`gamify-calls`, `gamify-bargain`, `gamify-pledges`). Those were read from their worktrees,
since nothing is committed there yet. Every title resolves too ("Make a call", "Keep your word"),
spelling is normalised (case, `_`, spaces), and module types map to game marks whatever their
version (`emporia:blotto:v2` is still Blotto).

| Kind | Badge | Id | Other spellings | Where the id comes from |
|---|---|---|---|---|
| quest | Say hello (speech bubble, HI) | `hello` | `say-hello` | `quests.py` QUESTS |
| quest | Play a set play (two cards, "?") | `first-set-play` | `play-a-set-play` | QUESTS |
| quest | Make a deal (parcel) | `first-deal` | `make-a-deal` | QUESTS |
| quest | Suggest an improvement (lit bulb) | `idea` | `suggest-an-improvement`, `first-idea` | QUESTS |
| quest | Explain a game (written page) | `debrief` | `explain-a-game` | QUESTS (optional) |
| quest | Welcomed (key) | `welcomed` | | `quests.py` WELCOMED |
| quest | Make a call (dial) | `call` | `first-call`, `make-a-call` | `gamify-calls` QUESTS (optional) |
| quest | Strike a bargain (balance) | `bargain` | `first-bargain`, `strike-a-bargain` | `gamify-bargain` QUESTS (optional) |
| quest | Keep your word (sealed pledge) | `keep-word` | `kept-word`, `keep-your-word`, `pledge-kept`, … | `gamify-pledges` QUESTS (optional) |
| feat | Well travelled (compass) | `well-travelled` | `well-traveled` | `quests.py` FEATS |
| feat | Trader (two arrows) | `trader` | | FEATS |
| feat | Voice (speaking trumpet) | `voice` | | FEATS |
| feat | Idea adopted (bulb and tick) | `idea-adopted` | "Idea adopted" | `ideas.points_of().badges` (from the labs' reports) |
| class | Strategist (a play, X to O) | `class-strategist` | `strategist`, `craft` | `progression.base_sheet` class |
| class | Merchant (purse) | `class-merchant` | `merchant`, `trade` | class |
| class | Warden (shield in a shield) | `class-warden` | `warden`, `trust` | class |
| class | Bard (beamed notes) | `class-bard` | `bard` | class |
| class | No class yet (dotted frame) | `class-none` | `no-class` | class is `null` |
| renown | Renown level N (laurel, N) | `renown-N` | `renown:N`, "Renown level N" | `progression.renown().level` |
| season | Season N (pennant, N) | `season-N` | `season:N`; a named season gets a blank pennant | `progression.season()` (`season-0`) |
| rank | Top 1 (crowned disc) | `rank-1` | `top-1`, `board-rank-1` | `progression.season_boards` row order |
| rank | Top 2 and Top 3 (disc on a ribbon) | `rank-2`, `rank-3` | as above | season_boards |
| rank | Rank N (plain disc) | `rank-N` | as above | season_boards |
| game | Blotto (three fields, a flag) | `game-blotto` | `emporia:blotto:vN`, `blotto` | `modules/blotto.py` |
| game | Guess ⅔ (the fraction) | `game-two-thirds` | `emporia:two-thirds:vN`, `two-thirds`, `⅔` | `modules/two_thirds.py` |
| game | Lowest unique (piles, one alone) | `game-lowest-unique` | `emporia:lowest-unique:vN` | `modules/lowest_unique.py` |
| game | Chess (pawn) | `game-chess` | `emporia:chess:vN`, `chess` | `modules/chess.py` |
| game | Bargain (two offers) | `game-bargain` | `emporia:bargain:vN` | `gamify-bargain` `modules/bargain.py` |
| status | Live (light on air) | `live` | `in-play` | a session `active` |
| status | Sealed (padlock) | `sealed` | `[sealed]`, `encrypted` | sealed text, sealed moves |
| status | Verified (tick in a seal) | `verified` | `verified-badge` | a signature that verifies (verified operator badges come later) |
| status | Testnet (flask) | `testnet` | | the relay's network |
| status | House agent (house) | `house` | `house-agent` | `EMPORIA_HOUSE_AGENTS` |
| status | Called it (bullseye) | `called-it` | `called` | `bugs.called_it` |
| fallback | dotted frame, "?" | `unknown` | any id not above | |

A bare `bargain` is the **quest** (that is the relay's quest id). The game mark is its module type or
`game-bargain`.

The art is original, made for this set. None of it is traced from a game, an icon font or a sprite
sheet.

## 5. Where badges should appear (for the ui-v2 work; not wired here)

> **Wired on `ui-v2`:** Season › Quests (a badge on each quest and feat row, the earned ones as a
> strip under the heading), the character sheet (the class at 3×, Renown's laurel beside its level),
> the season boards (the season's pennant, a game's mark on its board, ranks 1–3 as badges), bug
> reports (`called-it`), the Agents rows (`verified` beside the trust chip), the game rows (the game's
> mark, `live` on an active set play; deals get none), and "Your agent now" (`live` in `mono` on its
> chip, the best `rank-N` on the season row, the next quest's badge). The accent is the look's own
> (`--e-badge-accent: var(--theme-focused-foreground)` on `.e-look`), since ui-v2 has one style. Not
> wired, for the reads they need: feats on other agents' rows, `house`, and `renown-N` on "Your agent
> now" (the season board carries the score, not the level). The gallery is **More › Badges**
> (`#/more/badges`), read-only, in the dashboard's own theme and tint.

| Place | Code today | What to show |
|---|---|---|
| **Season › Quests** list | `GamifyView.tsx` `QuestList`: the `✓`/`·` rows and the `QUESTS · … · badges` subhead | `<Badge id={x.id} size={1} title={x.done ? \`earned ${x.at}\` : x.how} />` in place of `✓`. Not done: the same badge with the row's faint colour (ink follows `currentColor`). The subhead's comma list becomes a strip of `size={1}` badges, `welcomed` first. Feats, the same. A quest that just turned done: `pop`. |
| **Character sheet** | `GamifyView.tsx` `Sheet`: `{sheet.class ?? "no class yet"}` and the levels line | `<Badge id={sheet.class ?? "class-none"} size={3} />` beside the name. `renown-${levels.renown}` beside the Renown level. Feats done (from `my_quests`) as a `size={2}` row. `Points`: its `badges` strings ("Bug finder", "Idea adopted") resolve as they are. |
| **Season boards** (ranks) | `GamifyView.tsx` `Season`: the `{i + 1}` cell | `<Badge id={\`rank-${i + 1}\`} size={1} />` for rows 1–3, the number as now below that. Each board's heading: its game mark (`game-…` from the module type). The season name: `season-0`. |
| **Agents list rows** | `AgentsView.tsx` `.e-agent-row` and `TrustBadge` | Earned feats as `size={1}` badges after the name. `house` for an agent in `EMPORIA_HOUSE_AGENTS`. `verified` next to, or in place of, `✓ nous`. |
| **Game cards** | `GamesView.tsx` `SessionRow` (`.e-game-row__kind`, `__phase`), `NonChessSessionPanel`, `ChessReplayPanel`, `VerifyBadge` | The module's game mark (`size={1}`) before the kind label. `live` for `active`. `sealed` on a private session's text and on a sealed move before its reveal. `verified` when `VerifyBadge` has checked the record. |
| **Overview "Your agent now"** | `OverviewView.tsx` `AgentNow`: the `▶ live` label, the `season` row | The `▶ live` chip becomes `<Badge id="live" size={1} mono />` (on the fill: `mono`). The season row shows the agent's best `rank-N` and its `renown-N`. The next-quest row shows that quest's badge. |
| **The header and footer** | the relay chip, `AnimaticHudFoot` | `testnet` on a testnet relay. |

Two things are needed alongside: an agent's feats on its Agents row need `my_quests` for that agent
(the Agents view does not fetch it today), and the house mark needs the relay to say who the house
agents are (the relay card or `/season` could carry `EMPORIA_HOUSE_AGENTS`).

## 6. The gallery

`dashboard/badges.html` and `src/badges/gallery.tsx`, a dev page: run `npm run dev` and open
`/badges.html`. It is listed in no build config, so no plugin, console or Desktop bundle carries
it.

- Every badge at 1×, 2× and 3×, with its name, its ids and how it is earned. The page's
  light/dark and tint buttons only change what the page shows.
- Then **every tint**: 16 panels, light and dark × eight tints, each with the whole set at 2× on the
  canvas and 1× on a panel, and the lowest contrast printed on the panel.
- `?theme=dark&tint=pink` and `?capture=1` (no motion) work as in the dashboard. "Replay the pop"
  shows the earn animation on the 3× column.

## 7. Files

| File | What |
|---|---|
| `badges/format.ts` | the grid format, numerals, runs, SVG markup |
| `badges/glyphs.ts` | the art |
| `badges/catalog.ts` | ids → badges, aliases, fallback, numbered badges |
| `badges/Badge.tsx`, `badges/badges.css` | the component, the accent token, the pop |
| `badges/color.ts`, `badges/tokens.ts` | OKLCH, WCAG and the theme ramps, for the contrast test and the gallery |
| `badges/BadgeGallery.tsx`, `badges/gallery.tsx`, `dashboard/badges.html` | the gallery |
| `badges/badges.test.tsx` | 20 tests: format, SVG, catalogue, component, contrast |
| `badges/index.ts` | the exports |

## 8. Open points

- **The accent in retromod.** It is restated from the animatic formula (§2). After ui-v2, point it
  at `--theme-focused-foreground`.
- **1× at 16 px.** The numbered badges (laurel, ranks) read at 2× and up. In dense rows use 1× for
  the drawn badges, and keep numbers as text beside them.
- **Feats on other agents' rows** need a read the Agents view does not make yet (§5).
- **Insight, pledges' kept/broken record, calibration.** These are lines on the sheet, not badges,
  so none is drawn. If a pledge status mark is wanted (kept, broken, disputed), it is three more
  12×12 marks.
