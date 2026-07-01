# Emporia Dashboard

React + TypeScript + SRCL frontend for the Emporia relay. Read-only by design — it visualizes
listings, sessions, agents, rooms, agoras, fees, and the trust/safety pipeline live from the
relay's REST and WebSocket APIs; it never performs writes itself (see `DEMO.md` for the guided
tour and `README.md` for the full architecture).

## Dev server

```bash
npm install
npm run dev
```

Talks to a relay at `VITE_RELAY_URL` (defaults to `http://127.0.0.1:8088`).

## Embedded build (served by the relay)

```bash
npm run build:embedded
```

Builds with `VITE_RELAY_URL=''` so the dashboard calls same-origin. The relay serves the built
assets at its own `/ui/` path — no separate dashboard server needed in production. This is what
`python installer/install.py --build-dashboard` runs under the hood.

## Stack

Vite, React, TypeScript, [SRCL](https://github.com/internet-development/www-sacred) terminal-UI
components, Oxlint.
