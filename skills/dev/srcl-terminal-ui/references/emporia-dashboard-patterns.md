# Emporia dashboard — session patterns (hackathon_hermes)

Pair with `srcl-terminal-ui` SKILL.md.

## Nav

`AppNav.tsx` — 10 items from `NAV`; active tab accent border, no gray fill.

## Messages (inbox + DMs)

**`MessagesView.tsx`** — not `CommsView`. CSS: `.e-messages-pane`, `.e-messages-pane__toolbar`, `.e-messages-subnav`, `.e-messages-body`.

## Deep link (`App.tsx` + `eventNav.ts`)

`NavOpts`: `sessionId`, `roomId`, `agentId`. Listings: `listingNav.ts` `viewForListing()`.

## Overview

Uniform `div.e-stat-strip__item` for all KPIs including WS `live` — not mix of button + div.

## Key files

`listingNav.ts`, `ui/cards.tsx`, `dashboard-flat.css`, `views/*` with auto-select first.

## Verify

`npm run build:embedded`; hard-refresh `8088/ui/`.