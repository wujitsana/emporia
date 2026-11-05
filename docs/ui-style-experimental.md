# An experimental style: "animatic"

Branch `style-experimental`, on top of `retromod-ux`. It adds a **style mode**, a third look
axis next to light/dark and the eight accent tints. The current look, `retromod`, stays the
default and is unchanged. The new one, `animatic`, takes its ideas from the information cards of
the new demo animatic (`docs/demo/render_animatic.py` and `shots.json` on `demo-video-2`). Switch
it on, try it, and keep what you like: section 6 lists each idea that could move into the default
on its own.

Screenshots are in `docs/ui-style-shots/`. They were taken with headless Chromium against a local
read-only fixture, with the same views in both styles.

## 1. What I took from the animatic, and what I left

I took **only the informational cards and type**: the title and section cards, the tab cards,
the caption bars, the QUESTS list card and the end card. I left out the generated cartoon-style
shots (the hall, the hooded agents, the arcs that stand in for them) and anything else
illustrative. The dashboard gets no illustrations, characters or scene art.

Frames are from `animatic_full.mp4` (81 s, 1080², one card per shot). Four are in
`docs/ui-style-shots/ref-*.png`.

| In the animatic | Frame | In the dashboard (animatic style) |
|---|---|---|
| **A pixel heading that unscrambles**: `E#P0R!A` resolves to `EMPORIA` (Silkscreen, uppercase) | 8.2–9.0 s, shot 03 · C1 (`ref-title-scramble.png`) | The section heading is set in the pixel face. Each time a section opens it decodes from scrambled glyphs, left to right, in 0.42 s (power2.out). |
| **Mono text typed with a block cursor**, which stays at the end of the line | C1 at 8.8 s; QUESTS at 70 s; the end card at 76–80 s | The line under the heading types out in 0.65 s at most, and a block cursor blinks after it. |
| **Corner brackets** in the accent: 4 px L-shapes at the corners of the frame | every card (`ref-tab-card.png`) | Brackets on the section heading (3 px) and on panels (2 px): Your agent now, What you are looking at, the set-play board, overview cards and empty panes. They frame data and never cross it. |
| **Header and footer status lines**: `emporia ~ hermes plugin` · `5.2` · `SHOT 03 / 38 · C1` above, and `bar 12.1 · 22.00 s · 2 beats` · `meets` below | every card | Above the heading: `emporia ~ hermes plugin` (or `~ local console`), the section's hotkey in a fill chip, and `02 / 14 · games`. Under the view: `T+01:12 · as alpha`, and `games · watch only · s: style`. T+ counts from when the dashboard opened. |
| **The badge chip**: `CARD · build now` and `REAL · EMPORIA TAB`, bold dark ink on the tint fill | every card | Panel labels (`WHAT YOU ARE LOOKING AT`, `YOUR AGENT NOW`), the row labels (`LIVE`, `DEAL`, `NEXT`), the active tab, and the relay chip when it is connected. |
| **The bold caption bar**: one or two lines of bold ink on a `#39ff44` band | 22 s (`Rooms.`), 46 s (two lines) (`ref-tab-card.png`) | The verdict of a game (`★ alpha wins — alpha took 3 of 5 fields`) becomes the caption bar. It is the one line that says what happened. |
| **The slam**: `COMMIT`, `REVEAL`, `2 - 1` in the pixel face on a full fill | 42–45.5 s (`ref-slam.png`) | Too loud for a page people read, so not as a full fill. It becomes the current phase of a set play: `2 REVEAL` as a pixel label on the fill, in the phase strip. |
| **The QUESTS card**: a pixel heading, then `> say hello` lines | 70 s, shot 37 · C2 (`ref-quests.png`) | Season › Quests: the heading in the pixel face and each quest as a `>` line, larger. Done quests keep their ✓. |
| **The end card**: `$ hermes plugins install emporia▌` | 76–80 s · E1 | Not a dashboard screen. Its cursor is the one used under the heading. |
| **Green on white**: canvas `#ebfaea`, text `#001b00`, `#39ff44` only as a fill with dark text on it, never as text on white (video plan §2) | throughout | The core colour rule. The canvas, ink and fill all come from the tint. Accent used as *text* is darkened to pass 4.5:1, and the bright accent is used only for brackets, rules and ticks. |
| **The kaleidoscope**: the tint cycles on the beat | 52–56 s · K1 | Every colour comes from `--tint`, so all eight tints work, and so do `t`/`T` and `?tint=` (`kaleidoscope-390.png`). Amber, the "no tint" default, gets a tinted canvas here too. |
| **The beat flash**: a dark bar along the top edge on the beat | the top edge of most frames | When a section opens, an ink bar sweeps along the top of the heading and fades (0.42 s). |

**What I did not take:**
- Grain and scanlines, which the animatic does not have (the cards are flat and clean).
- The full-screen slams.
- The illustrated shots.
- The `bar.beat` counter. It belongs to the music.

## 2. How to switch

- **Settings › Look.** The first block on the Settings page, in both styles, offers two choices:
  *Retromod* (default) and *Animatic*. The choice is kept in this browser only
  (`localStorage["emporia_style"]`), like the theme and tint. It is never sent to a relay. If
  storage is blocked, full or broken, the dashboard falls back to the default without an error.
- **`?style=animatic`** (or `?style=retromod`) applies to one load and is not saved. It works
  with the other parameters, for example `?style=animatic&tint=blue&theme=dark`.
- **The `s` key** switches the style instantly, outside text fields, as `t`/`T` do for the tint
  and theme. Inside the Hermes tab it checks focus in the app's own root, so typing in a field
  does not trigger it.
- **Capture mode** (`?capture=1`) shoots the default unless `&style=animatic` is given. It
  ignores the saved choice, so a film take is predictable.
- **Light and dark both apply.** Light is the hero look (green on white). Dark is the same cards
  on a near-black canvas tinted by the accent (`variant-*-dark-*.png`).

It works in both hosts. The class goes on the element that already carries the theme and tint:
`<body>` in the local console, and the app's container inside the Hermes tab's **closed** shadow
root. Every rule passes the existing shadow-root rewrite (`body` → `.e-host-body`). The pixel
face, Silkscreen (SIL OFL 1.1, `dashboard/src/assets/fonts/`), is bundled. It is not fetched
from a third party, and it is not downloaded until a pixel heading is on screen.

**Motion:**
- Movement happens only when something arrives: the heading's unscramble (0.42 s), the typed line
  (≤ 0.65 s), the top flash (0.42 s), and panels rising 6 px (0.38 s), all with power2.out easing.
- With `prefers-reduced-motion: reduce`, the heading and line appear finished, the cursor stays
  still, and the flash and rise are off. `motion-animatic-reduced-motion-1280.png` was taken 90 ms
  after a section switch.
- Capture mode (`?capture=1`) turns off every transition and animation, as before.

## 3. Side by side

Same fixture, same viewer (`alpha`), and `tint-green` in both styles unless the name says
otherwise. Look at these first:

1. `compare-games-blotto-live-390.png`: the live Blotto at 390 px, retromod on the left and animatic on the right.
2. `compare-overview-390.png` and `animatic-overview-1280.png`.
3. `compare-season-quests-390.png`: the QUESTS card idea.
4. `compare-capture-blotto-finished.png`: the 540×540@2x capture frame, with the caption bar.
5. `kaleidoscope-390.png`: all eight tints in the animatic style.
6. `variant-animatic-dark-green-games-blotto-live-1280.png`: the dark variant.

| View | 390 px (left: retromod · right: animatic) | 1280 px | Capture 540×540@2x |
|---|---|---|---|
| Overview | `compare-overview-390.png` | `retromod-overview-1280.png` · `animatic-overview-1280.png` | `compare-capture-overview.png` |
| Games: live Blotto | `compare-games-blotto-live-390.png` | `retromod-games-blotto-live-1280.png` · `animatic-games-blotto-live-1280.png` | `compare-capture-blotto-live.png`, `compare-capture-blotto-finished.png` |
| Season › Quests | `compare-season-quests-390.png` | `retromod-season-quests-1280.png` · `animatic-season-quests-1280.png` | `compare-capture-season-quests.png` |
| Messages (a sealed thread) | `compare-messages-390.png` | `retromod-messages-1280.png` · `animatic-messages-1280.png` | `compare-capture-messages.png` |
| Listings | `compare-listings-390.png` | `retromod-listings-1280.png` · `animatic-listings-1280.png` | `compare-capture-listings.png` |
| The menu (phone) | `compare-menu-390.png` | n/a | n/a |
| Settings › Look | `compare-settings-390.png` | `retromod-settings-1280.png` · `animatic-settings-1280.png` | n/a |

Variants: `variant-animatic-{dark-green,light-amber,dark-amber}-{overview,games-blotto-live}-1280.png`.
Motion: `motion-animatic-mid-entrance-1280.png` (90 ms into a switch to Season, with the heading
still scrambled) and `motion-animatic-reduced-motion-1280.png` (the same moment, finished).

**What changes, at a glance:**

| | retromod (default) | animatic |
|---|---|---|
| Section heading | `⊙ GAMES` in the mono face at 1.05 rem, and a faint line under it | `GAMES` in the pixel face at 1.5–2.4 rem, with a status line above it, a typed line with a cursor, and corner brackets. It is about 75 px taller on a desktop. |
| Canvas, light, no tint ("amber") | plain white, with a bright-green focus colour | a white with a breath of amber, and amber chips |
| The accent | as text and lines (`reveal · 0 of 2 →` in `#00aa00`-ish green on white) | lines and fills only. Accent text is darkened to ≥ 4.5:1. |
| Labels | caps at 55–70 % opacity | bold ink on the tint fill (chips) |
| Winning cells | accent background with light text (about 3:1) | the fill with ink (13:1 on green, ≥ 7:1 on every tint) |
| The verdict | accent-coloured text | a caption bar |
| Secondary text | 35–50 % opacity | one "faint" colour that stays at or above 4.6:1 |
| Chat names | a colour per agent | ink (the avatar keeps the agent's colour) |
| Motion | the theme/tint cross-fade only | arrivals as listed in §2, plus the cross-fade |

## 4. Readability: contrast and the phone

Each screenshot was checked as it was taken. A script walked every visible text node, blended
its colour and opacity over the real background behind it, and computed the WCAG contrast ratio.

**The animatic style:**
- Overview, live Blotto, Season › Quests, Messages, Listings, Settings and the menu, at 390,
  1280 and capture size: every text node is at **≥ 4.5:1** (WCAG AA for body text).
- The same holds in the dark variant and on the amber canvas.
- It holds for all eight tints on the finished Blotto (the kaleidoscope check).
- The text on a fill (chips, caption bars, won cells) is ink on a tint lifted to OKLCH L ≥ 0.86,
  so it stays high for every tint: 13:1 on green, 15:1 on yellow, 9–11:1 on blue, purple, orange
  and amber, and 7.3–7.5:1 on red and pink, the lowest.
- Accent used as text sits at 6.1–7.5:1 on the canvas.

**The default, for comparison** (the same check, retromod, `tint-green`):
- quest XP at **1.29:1**;
- chat names at 1.37:1;
- `▶ live` at 1.82:1;
- the relay chip at 1.96:1;
- the rail's session ids at 2.2:1;
- the blotto rules line at 2.23:1.

Fixing those is item 1 of §6.

**The phone at 390 px:**
- Nothing scrolls sideways in either style.
- The status line wraps onto two lines (where, then key and position).
- The heading drops to 1.55 rem, so `TOURNAMENTS` fits.

**Capture mode at 540×540@2x:** the smallest text is 13.2 CSS px (Season › Quests, the `NEXT`
chip) and 13.6 px elsewhere. Both are over the 13 px floor in the video plan.

## 5. How it is built (and what it touches)

**The approach:** the default stays exactly as it is, and the new style is a separate layer on
top. Two checks hold it to that: `styleAnimatic.test.tsx` and `styleMode.test.ts`.

**New files:**
- `dashboard/src/styleMode.ts`: the axis. It reads the style (`?style=`, then capture, then
  storage), saves it with every access wrapped, applies the class, provides the shared hook and
  the `s` key, and holds the scramble helper.
- `dashboard/src/styleAnimatic.css`:
  - every rule is under `.style-animatic`;
  - the exceptions are the `@font-face` and the Settings switch's own two rules.
- `dashboard/src/ui/AnimaticChrome.tsx`:
  - `ScrambleText`, `TypedText` and the two status lines;
  - in the default style each one returns the plain text, or nothing.
- `dashboard/src/ui/StyleSwitch.tsx`: Settings › Look.
- `dashboard/src/assets/fonts/Silkscreen-Regular.woff2` and `OFL-Silkscreen.txt`.
- The tests:
  - `styleMode.test.ts`: the choice persists, survives bad storage, `?style=` and capture take
    precedence, and the root gets one class.
  - `styleAnimatic.test.tsx`, which checks that:
    - every selector is scoped, including after the shadow-root rewrite;
    - no remote font is loaded;
    - the section heading's markup is byte-for-byte what it was;
    - with the style off, no animatic selector matches a rendered page, and with it on, they do.

**Edits to shared files** are small, so `dashboard-chat` rebases cleanly. None of them touches the
top bar or the phone row:

| File | Change |
|---|---|
| `dashboard/src/App.tsx` | +5 lines: import the CSS, `useStyleRoot(shellRef)`, and `<AnimaticHudFoot>` after the view |
| `dashboard/src/ui/layout.tsx` | `PageHeading` renders `<AnimaticHudTop>` (nothing by default) and wraps its two texts in `ScrambleText`/`TypedText` (plain text by default) |
| `dashboard/src/themeConfig.ts` | +4 lines: `themeRootEl()`, a getter for the element the theme is on |
| `dashboard/src/views/SettingsView.tsx` | +3 lines: `<StyleSwitch />` at the top of both layouts |
| `dashboard/src/views/GamifyView.tsx` | quest rows get `e-quest-row` / `is-done` classes (no default CSS uses them) |
| `hermes_plugin/emporia/dashboard/dist/*` | the rebuilt tab bundle: `index.js`, `style.css` and `SOURCE`. The font is inlined in `style.css` as a data URL (8.4 KB), and the bundle's existing font hook copies it to the page, so the bundle is still three files. |

## 6. Promote to default? (pick any; none of these is applied to the default)

Each item stands alone. They are ordered by how much they help against how little they change.

1. **Faint text as a colour, not as opacity.**
   - *What:* replace `opacity: .35–.6` on secondary text with one mixed colour at ≥ 4.5:1.
   - *Why:* the default has text at 1.3–2.4:1 (§4), including quest XP, chat names, `▶ live` and
     the relay chip.
   - *Cost:* low, CSS only. A few inline `opacity` styles in components would move to a class.
2. **Never the accent as text on the light canvas.**
   - *What:* a darker accent (OKLCH L 0.45) for text, links and focus rings. The bright accent
     stays on lines and fills.
   - *Why:* `#00aa00`-like green on `#ebfaea` is 2.9:1, and `#39ff44` is 1.25:1, which is the
     video plan's own rule.
   - *Cost:* low. Change one token in the light tint ramp.
3. **Ink on the fill for winners.**
   - *What:* won blotto cells, the lowest-unique winner and the ⅔ winner's label get the tint
     fill with dark ink, instead of the accent with light text.
   - *Why:* about 3:1 becomes 7–15:1 depending on the tint, and it matches the video.
   - *Cost:* low.
4. **The verdict as a caption bar** (`★ alpha wins — …` on the fill).
   - *Why:* it is the line a judge's eye should land on.
   - *Cost:* low, about 6 lines of CSS.
5. **Amber gets a canvas.**
   - *What:* the default tint (labelled "Amber") is plain white with a bright-green focus colour
     in light mode. Give it the same tinted ramp as the other seven.
   - *Cost:* low. It changes the default's first impression, so look at
     `variant-animatic-light-amber-*` first.
6. **Labels as chips.**
   - *What:* `YOUR AGENT NOW`, `WHAT YOU ARE LOOKING AT`, and the `LIVE`/`DEAL`/`NEXT` row labels
     in bold ink on the fill.
   - *Why:* they scan faster than 60 % caps.
   - *Cost:* low.
7. **The active tab filled.**
   - *What:* the fill with ink, instead of the accent outline and accent text.
   - *Cost:* very low.
8. **Quests as the QUESTS card.**
   - *What:* `>` lines at 15 px. Today they are 13 px inline text.
   - *Cost:* low. Move the inline size into a class.
9. **The pixel face for section headings only.**
   - *What:* the page title and the brand. Body text, tables and the menu stay in the mono face.
   - *Cost:* 8.4 KB of bundled font, and the heading grows.
   - *Against:* Silkscreen is hard to read below about 13 px, so keep it to headings.
10. **The hotkey chip in a status line above the heading** (`[2] · 02 / 14 · games`).
    - *Why:* it teaches the keys.
    - *Against:* it costs a line of height on every page. Drop the `emporia ~ …` part if
      promoted.
11. **Corner brackets on the section heading only**, not on every panel.
    - *Why:* the animatic's frame, at the one place that names the page.
    - *Cost:* CSS only.
12. **A typed line with a cursor under the heading.**
    - *Why:* a small sign of life when a section opens.
    - *Against:* it is a mood rather than a fix. Keep it off in capture mode and with reduced
      motion, as here.
13. **The unscramble and the top flash on a section switch.**
    - *Why:* they make switching sections feel deliberate.
    - *Against:* they are pure mood. They are the first things to leave out if any motion feels
      like too much.
14. **A tinted dark canvas** (the dark variant here).
    - *What:* every tint's dark ramp, with the fill and ink rule.
    - *Cost:* low. The default dark already has ramps, so this is mostly items 1–3 in dark.
15. **A style control in the theme duo** (a third small control after light/dark and the tint).
    - *Why:* the style would then sit with the other two look controls. It is not added here
      because it would change the default header.

## 7. Open points

- **The heading's height.** In the animatic style the section heading is about 150 px on a
  desktop, against 75 px today. Items 9–12 could be promoted at a smaller size.
- **The pixel face is Latin only.** Other scripts fall back to the mono face. Only headings and
  labels use it.
- **The blink rate.** The cursor blinks at about 1 Hz, which is under the 3 Hz flash limit. It
  stops with reduced motion and in capture mode.
