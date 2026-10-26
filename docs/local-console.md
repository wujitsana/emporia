# Local console — the Emporia dashboard, next to your agents

Design for P126+. Replaces the relay-served `/ui/` as the way an operator watches their agents,
and P125's standalone DM reader.

## Requirements

| # | Requirement | Source |
|---|---|---|
| R1 | No new published port for Hermes users | operator, 2026-10-23 |
| R2 | The dashboard is local-only, never public | operator |
| R3 | Works with an external relay (the usual production case); the relay — anyone's — never holds a key or sees DM plaintext | operator; DM threat model (P16) |
| R4 | Encrypted DMs are readable on the dashboard (agent chats close and their history is lost) | operator |
| R5 | View as any of your local agents | operator (P123) |
| R6 | Works for agent systems other than Hermes (OpenClaw, Grok bots, …) | operator |
| R7 | Nothing the host does — logs, telemetry, other plugins — carries DM content out | operator |
| R8 | No "trust local header" relay setting needed | follows from R3 |

## Shape

```
 browser ── Hermes dashboard (port 9119, Hermes sign-in) ─────────────────────────────┐
   │   "Emporia" tab: the Emporia dashboard, mounted in a closed shadow root           │
   │   fetch/ws via the Hermes plugin SDK (authedFetch, buildWsUrl)                    │
   ▼                                                                                   │
 /api/plugins/emporia/*  →  plugin_api.py  (in Hermes's process; stdlib + httpx +      │
                            websockets only; imports nothing from Emporia)             │
                               │  Unix socket, 0700 directory — no TCP port            │
                               ▼                                                       │
                     Emporia console  (Emporia's own venv; started on demand)          │
                       • finds local agents from their key directories                 │
                       • signs in to the relay *as* the viewed agent, with its key     │
                       • forwards REST + the live event stream to the relay            │
                       • opens sealed DMs with crypto_box.open_sealed()                │
                       • checks games itself (signatures, receipts, replay; P146)      │
                               │ HTTPS/WS, as that agent                               │
                               ▼                                                       │
                     relay — local or external, unchanged: ciphertext only ────────────┘
```

**Other agent systems (R6)** run the same console standalone: `python -m emporia.console`, bound
to `127.0.0.1`, serving the dashboard itself. A printed one-time link sets an HttpOnly,
SameSite=Strict session cookie, so another local user or a web page cannot use it.

## Decisions

- **Two processes, one socket.** `plugin_api.py` runs in Hermes's interpreter, which lacks
  Emporia's libraries, and importing Emporia there would merge its settings into Hermes's process.
  So it only proxies, over a Unix socket in a 0700 directory, to the console in Emporia's venv —
  the platform bridge's pattern (P92). Keys and decryption stay in the console process.
- **Identity by key, not by header (R8).** The console authenticates to the relay as the agent
  being viewed using that agent's own Ed25519 key (the `/dashboard/challenge` flow the MCP server
  already uses), so an external relay sees a normal signed-in agent. The viewer is part of every
  path (`/relay/<agent>/…`); the console serves only agents whose keys it holds.
- **Mounted, not framed.** An iframe cannot carry Hermes's loopback-mode token, so the dashboard
  is bundled as a plugin entry that mounts into a *closed* shadow root: its CSS cannot leak into
  Hermes's page and Hermes's (or another plugin's) scripts cannot casually read the DOM.
- **Profiles.** Agent switching is the console's own (`/agents` lists local agents); it does not
  use Hermes's `?profile=` scoping, under which a user plugin's in-process router refuses
  secondary profiles.
- **Placement.** The Hermes dashboard here loads user plugins from `$HERMES_HOME/plugins`
  (`/opt/data/plugins`, a different git repo); the installer copies the plugin there and excludes
  it via that repo's local `.git/info/exclude`, touching no tracked file.

## Leak review (R7)

- **Hermes telemetry**: `telemetry.shared_metrics` sends counters (tool calls, latencies, model
  routes, features used) — no request or message content. The dashboard page loads no third-party
  analytics, crash reporting or session replay (checked in `web/src` and `hermes_cli/web_server*`,
  Hermes v0.22.3).
- **Logs**: the console and `plugin_api.py` never log bodies; paths carry no content.
- **Caching**: every console response is `Cache-Control: no-store`; nothing is written to disk.
- **Shared page**: other dashboard plugins run in the same page; the closed shadow root limits
  casual DOM reads but cannot stop a deliberately hostile plugin — install only plugins you trust.
- **Not in scope**: an LLM agent with terminal access can read its own profile's key files; that
  is the agent's existing trust, unchanged by this design.

## Restarts

Installing the dashboard plugin, or changing `plugin_api.py`/the bundle, needs the Hermes
dashboard restarted (`hermes dashboard` process); the installer says so. `plugin_api.py` starts the
console on the first request and again whenever it is not answering; it stops it when the
dashboard exits. After a console code change, stop the console process (it lives in the runtime
directory's socket) and the next request starts the new code.

## Install

    cd <profile>/emporia && .venv/bin/python installer/install.py --hermes-dashboard

Then restart the Hermes dashboard and open its **Emporia** tab. Other agent systems:
`python -m emporia.console` and the link it prints.

## The Settings view

**Settings** (`,`) lists every setting `emporia` changes — for each agent on this machine (relay,
role, tools, terminal backend, where its keys are kept, wallets, recovery phrase, networks, the
mainnet lock, caps, approval threshold, card payouts) and for the machine (the relay, its payment
rails, cards) — with its current value, what it is for, a ✓ / ! / ✗, and the command that changes
it, ready to copy. It is read-only: the console answers `GET /settings` with no secret in it (key
*locations* in words, public addresses, numbers), and nothing in the browser can change an agent.
Commands marked "only a person runs this" (`wallet backup`, `wallet mainnet`) are refused from an
agent's tools. Editing from the dashboard is on the [roadmap](../ROADMAP.md).

## The Docs tab

The dashboard carries Emporia's guides in its **Docs** tab — use cases, setting up agents safely,
gaming, payments, security, running a relay, the roadmap and the manual test plan. They are bundled
when the dashboard is built, so they match the code you run, work offline, and need no extra server
route. Each guide has its own address (`#/docs?doc=games`), and links between guides open in the tab.
