# Header relay status (`RelayStrip.tsx`)

## Chip semantics

| Chip | Label | When OK | When not |
|------|-------|---------|----------|
| REST | **API** | ● + accent (`health.status === "ok"`) | ○ subdued, or **`BlockLoader`** while `health === null` |
| Stream | **events** | SRCL **`BlockLoader`** + accent (`wsConnected`) | ○ subdued |

Full URLs belong in chip **`title`** only (`…/health`, `…/ws/events`). No extra `ws://` line under chips.

## Spacing (common complaint: “too close”)

- **Host vs chips:** `.e-relay-status` gap ~14px; host gets small `margin-right`.
- **API vs events:** wrap both in `.e-relay-status__chips { gap: 10px }`.
- **Indicator vs text:** `.e-status-chip { gap: 7px }` — split into `__loader`/`__dot` + `__label` (do not concatenate `●API` in one span).

## Chip chrome

- **`.e-status-chip`:** `border-radius: 0`, background `var(--theme-border)`, square like operator preference for status controls.
- **Loader width:** `.e-status-chip__loader { min-width: 1.1ch }` to limit tab/header shift when WS connects.

## Implementation file

`dashboard/src/RelayStrip.tsx` — import `BlockLoader` from `@components/BlockLoader`; `liveLoader` prop on events chip when connected.

Mirror updates in repo copy: `emporia/skills/dev/srcl-terminal-ui/references/emporia-dashboard-relay-header.md` when editing canonical `emporia/skills/`.