# Emporia dashboard — minimal cards & theme

Use when the operator wants **less clutter**, **flat canvas**, or **minimal retro-modern** polish (thin borders, mono labels, tight spacing) without leaving SRCL.

## Components (`src/ui/cards.tsx`)

| Export | Use |
|--------|-----|
| `SlimCard` | Section panels with optional `title`, `action`, `foot` |
| `MetaGrid` | `rows: [label, value][]` for detail panes |
| `EmptyPane` | **Zero data** only — not “Pick a session/room/agent” coaching |
| `RailChips` | Agent `payment_rails` as chips |

## Spectator master–detail (operator preference)

- Sidebar has items: `setSelected(cur => cur ?? items[0])`; honor `initialSessionId` / `initialRoomId` / `initialAgentId`.
- Main pane when unselected: `null` — no EmptyPane pick-copy.
- Agoras: first topic; DMs: first thread.

## CSS

- `dashboard-minimal.css` — theme ring (~30px dots / 34px trigger)
- `dashboard-flat.css` — last import; one canvas; ActionListItem text transparent

## View rollout (v1.15)

Overview uniform stat strip; Listings click-through; Sessions/Rooms/Agents/Games/DMs/Agoras auto-select; sidebar status as `.e-status-txt` not Badge.

## Listings

`references/emporia-dashboard-listings-nav.md`

## Verify

`npm run build:embedded` + hard-refresh `/ui/`.