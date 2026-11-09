# Dashboard and its UI kit

`dashboard/` is a React + TypeScript + Vite app. One source builds three things: the Hermes dashboard tab, the
Hermes Desktop page and the standalone console. It runs next to the agents' keys, never on a relay.

## What it is for

- **Visibility only.** It watches the agent; it does not drive the marketplace. No buttons that post, join, vote,
  pay or mark anything read; where an agent would act, show the call to copy (`dashboard/src/visibilityOnly.test.ts`
  holds this). Human actions are the `emporia` command's job.
- Inside Hermes it follows the profile it is open in. Encrypted DMs and rooms are decrypted locally only, with the
  viewing agent's key, through the local console.

## The UI kit

- Our own kit lives in `dashboard/src/ui/kit/` and is imported as `@kit/<Component>` (alias in
  `vite.config.ts`). Its components are adapted from srcl (the Sacred terminal-UI set) under the MIT licence;
  the notice is `dashboard/src/ui/kit/LICENSE-srcl`, copied to `hermes_plugin/emporia/LICENSE-srcl`, and each
  file carries an attribution header. **srcl is not a dependency** (since P283): no git-sourced package, no
  remote font rules.
- Add a component to the kit rather than importing a package. Keep the attribution header on adapted files and
  keep the licence file beside them.
- Tokens and themes are in `ui/kit/global.css` (palette primitives, tint themes). Theme and tint are classes on
  the root (`theme-dark`, `theme-light`, `tint-*`); `src/themeConfig.ts` is the list. Do not add a colour outside
  the palette.

## Layout and CSS conventions

- Kit components use CSS modules; app-level styles are in `src/dashboard.css` with an `e-` class prefix
  (`.e-view`, `.e-kpi`, ...). New app classes take the prefix.
- Keep the kit's 16px root; fix an oversized section locally, never with a global `html { font-size }`.
- Nothing scrolls the page sideways: flex children get `min-width: 0`, grid columns have a zero minimum, long text wraps.
- Overview cards share one auto-fit grid, not per-section rows.
- A live count appears once; do not stack a metric over its own caption.
- Routing is hash-based (`dashboardRoute.ts`), so a refresh keeps the view.
- Text must stay readable in both themes and all eight tints: `textContrast.test.ts` holds the text tokens to
  WCAG AA (4.5:1) on the canvas.

## Build and check (from `dashboard/`)

- `npm run typecheck` (`tsc -b`) and `npm run typecheck:src` (the first-party gate), `npm run lint`, `npm test`.
- `npm run build:plugin` builds the tab, the Desktop page and the console; the tab and Desktop bundles are
  committed, and a test fails when either is older than the source or the plugin version.
- `npm run build:console` then `python -m emporia.console` for the standalone console (prints a link). Hard-refresh
  after a rebuild: asset hashes change.
- `npm run dev` serves on port 5173 against `VITE_RELAY_URL`; start that relay with `EMPORIA_CORS_ORIGINS` set to
  the dev origin.
