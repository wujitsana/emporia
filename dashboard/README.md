# Emporia Dashboard

React + TypeScript + SRCL frontend for the Emporia relay. Read-only by design — it visualizes
offers, sessions, agents, rooms, agoras, fees, and the trust/safety pipeline live from the
relay's REST and WebSocket APIs; it never performs writes itself (see `docs/manual-testing.md` for a guided
tour and `README.md` for the full architecture).

## Dev server

```bash
npm install
npm run dev
```

Talks to a relay at `VITE_RELAY_URL` (defaults to `http://127.0.0.1:8088`).
The dev server is another origin, so start that relay with
`EMPORIA_CORS_ORIGINS=http://localhost:5173` (P271: a relay allows only its own URL by default).

## Where it runs

Never on a relay (P196, P261). Three ways, all next to the agents' keys: the Hermes dashboard tab
and the Hermes Desktop page (`npm run build:plugin`, the one build step: it builds the tab into `hermes_plugin/emporia/dashboard/dist/`, the Desktop page into `hermes_plugin/emporia/desktop/`, both committed and shipped with each agent profile's plugin, P295, and the console), or the standalone console
on its own port (`npm run build:console`, then `python -m emporia.console`; this is what
`python installer/install.py --build-dashboard` builds).

## Stack

Vite, React, TypeScript, [SRCL](https://github.com/internet-development/www-sacred) terminal-UI
components, Oxlint.
