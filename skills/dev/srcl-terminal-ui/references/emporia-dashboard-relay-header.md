# Emporia dashboard — header relay status (`RelayStrip.tsx`)

## What the chips mean

| Chip | Label | When OK | Meaning |
|------|-------|---------|---------|
| REST | **API** | ● + accent text | Relay **HTTP** — `api.health()` poll (~5s). `status === "ok"` → online. |
| Stream | **events** | `BlockLoader` + accent | **WebSocket** `/ws/events` — `useGlobalEvents()` live dashboard feed. |
| Offline | **API** / **events** | ○ + subdued | Health failed or WS disconnected. Chip `title` = full URL (`…/health` or `…/ws/events`). |

Host left of chips = `relayHost(RELAY)`. Wrapper `title` = full HTTP base.

**On ask:** REST = health poll, not “generic API”; events = WS stream, not calendar/events tab.

## Square corners vs background (operator preference)

| Control | `border-radius` | Background |
|---------|-----------------|------------|
| **`.e-status-chip`** (API, events) | `0` | **`var(--theme-border)`** — operator likes these |
| **`e-section-tab`** (header + drawer nav) | `0` | **transparent** — active = accent **border** only, no chip fill |
| **Mobile ☰ `ActionButton`** (`.e-mobile-top`) | `0` | **transparent** — square hit target only |
| **Other `ActionButton_content`** | default SRCL / transparent outline | **Do not** blanket `--theme-border` fill unless user asks |

**Pitfall:** Shipping filled square chips on **all** nav tabs + overview tiles after user said they only wanted the **menu button square with no different background** — revert fills; keep square on section tabs + ☰ without gray wash.

## Implementation

- **`StatusChip`** in `RelayStrip.tsx` — class **`e-status-chip`** (not SRCL `Badge`).
- **`dashboard-chrome.css`**: chip styles + `.e-dashboard-shell .e-section-tab { border-radius: 0 }` without forcing chip background on tabs.

## Pitfall

No second monospace `ws://…` line under chips (`showWs` deprecated).