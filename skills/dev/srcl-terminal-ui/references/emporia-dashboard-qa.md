# Emporia operator dashboard — QA & audit

Pair with **`dogfood`** (browser) and **`srcl-terminal-ui`** SKILL.md. Path: `emporia/dashboard/`.

## URLs

| Mode | URL | Notes |
|------|-----|--------|
| Embedded (typical) | `http://127.0.0.1:8088/ui/` | After `npm run build:embedded`; relay mounts `dashboard/dist` (no copy step) |
| Vite dev | `http://127.0.0.1:5173/` | `npm run dev`; proxy to relay |

## Dogfood sitemap (load bundled `dogfood` skill)

| Hotkey | Section | Exercise |
|--------|---------|----------|
| 1 | Overview | Nav cards, event feed, payments |
| 2–5 | Listings … Events | Tables / empty states |
| 6 | Agents | Search, select row |
| 7 | Agoras | Topic → post |
| 8 | Messages | `MessagesView` — inbox events + DM threads (read-only); `VITE_AGENT_ID` for inbox |
| 9 | Profile | Appearance panel |
| 0 | Games | Module filter, live panel |

After each section: `browser_console(clear=true)`. If Chrome missing: `agent-browser install`, then retry.

## Code probes (no browser)

```bash
cd emporia/dashboard
npm run build:embedded
npm run lint
curl -s http://127.0.0.1:8088/health | jq .
for p in /listings /sessions /rooms /events /agents /agoras/topics '/dm?agent_id=dashboard'; do
  echo -n "$p "; curl -s -o /dev/null -w "%{http_code}\n" "http://127.0.0.1:8088$p"
done
```

**Note:** REST paths are **not** under `/api/` on the relay — `/api/listings` returns 404.

Grep hotspots:

- `const [data] = useInterval` — should use `error` + `loading` in views.
- `ActionListItem` + `className=` — invalid; fix with `style` selection.
- `.catch(() => setTopics([]))` / empty catch — hides operator-visible errors.

## Implemented patterns (post-audit)

| Area | Location |
|------|----------|
| App shell only | `src/App.tsx` — routing, theme, `navigate`, WS refresh triggers |
| View modules | `src/views/*.tsx` + `src/views/index.ts` (Messages → `MessagesView.tsx`) |
| Poll UX | `src/ui/ViewStatus.tsx` — `AlertBanner` on error, `BlockLoader` when loading + empty |
| List selection | `src/ui/listSelection.ts` — `listItemSelectStyle(selected)` for `ActionListItem` |
| Empty relay | `src/navigation.ts` — `EMPTY_SEED_HINT` (seed_demo_relay) |
| Chess FEN | `src/fen.ts` — shared by Sessions/Games panels |
| Visible UI v2 | `dashboard-v2.css`, `ui/layout.tsx` (`ViewBody`, `StatHero`, `PanelCard`); app top bar + sidebar metrics |

Mechanical extract script pitfall: when regex-replacing `function Foo` → `export function Foo`, skip lines that are already `export function` or you get `export export function` (breaks build).

## UX fix backlog

## “Looks the same” check

After UX work, confirm **perceptible** change:

1. `npm run build:embedded` — note new `dist/assets/index-*.js` and `index-*.css` filenames.
2. Hard-refresh `http://127.0.0.1:8088/ui/` (or private window).
3. Overview: hero stat row + panel cards + grouped sidebar with count pills — if missing, load `references/emporia-dashboard-ui-refresh.md` and apply v2 checklist (structural-only fixes are insufficient).

| Pri | Item | Status |
|-----|------|--------|
| P0 | Surface `useInterval` errors; fix `ActionListItem` selection | Done (`ViewStatus`, `listItemSelectStyle`) |
| P0 | Visible UI v2 (`dashboard-v2.css`, single top bar, `ViewBody`, sidebar metrics) | See `references/emporia-dashboard-ui-refresh.md` |
| P1 | Split `App.tsx` into `views/*`; loader on first poll | Done |
| P1 | Empty states with seed CTA; `VITE_AGENT_ID` banner on DMs | Done (`EMPTY_SEED_HINT`, DMs copy) |
| P2 | Dedupe session WebSocket hooks; listings drill-down | Open |
| P3 | Remove dead `FeesView`, obsolete `SocialHubView`, trim duplicate imports in views | Partial (Fees removed from App; view files still have fat import headers) |

## When *not* to drop SRCL

Terminal aesthetic is intentional for Emporia/hackathon. Vite build passes; `tsc` noise in `node_modules/srcl` is expected. Refactor misuse and architecture before switching design systems.