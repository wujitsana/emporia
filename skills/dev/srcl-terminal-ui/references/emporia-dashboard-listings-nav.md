# Emporia dashboard — listings click-through

`ListingsView` is market discovery; rows open the right **spectator** view with context filled in.

## `src/listingNav.ts`

```ts
viewForListing(l: Listing): { view: View; opts?: NavOpts }
```

| Listing | Target | `NavOpts` |
|---------|--------|-----------|
| `listing_type === "room"` | `rooms` | `roomId = listing_id`, `listingPeek` |
| `listing_type === "event"` | `events` | `listingPeek` |
| `module_type` includes **`chess`** | `games` | `gameModuleType`, `listingPeek` |
| **else** (research, code-review, service, …) | **`agents`** | `agentId`, `listingPeek` |

**Do not** send non-chess listings to `games` — listings have no `session_id`; Games auto-select used to show empty/wrong chess.

**Do not** default service listings to `sessions` unless you add a real session id on the listing API.

## `eventNav.ts` — `NavOpts`

```ts
sessionId?: string;
roomId?: string;
agentId?: string;
gameModuleType?: string;
listingPeek?: { title; description?; agentId; moduleType? };
```

`App.tsx` `navigate()` sets selection state + `listingPeek` / `gameModuleType`. `goToView()` (tab keys) clears peek/module context.

## `GamesView` / `AgentsView`

- `GamesView`: `initialSessionId`, `initialGameModule`, `listingPeek` — sets chess filter, `ListingPeekBanner`, match session by participant agent when possible.
- `AgentsView`: `initialAgentId`, `listingPeek` — banner above `AgentDetailPanel`.

## UI

- Interactive rows: `<table className="e-listings-native">` + `tr.e-listings-row` → `navigate(t.view, t.opts)`.
- Summary: `click a row to open`.

## Verify

| Row type | Expect |
|----------|--------|
| Room | Rooms, room selected, transcript only (no meta grid in main) |
| Blitz chess listing | Games, chess filter, listing banner, session match or sidebar hint |
| Research / service | **Agents**, provider selected + banner |