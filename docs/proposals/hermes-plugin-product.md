# Proposal: Emporia as one Hermes plugin, installed from the catalog

Status: **steps 1 and 2 built (2026-11-04)**, untested on a fresh machine yet. Branch `hackathon-v2`; it becomes the release line `main` takes after the manual testing.

## 1. Why

A Hermes user should get all of Emporia from one listing:

```
hermes plugins install emporia
hermes emporia setup
```

**Today** Emporia reaches Hermes only from a repo checkout:
- **Two plugins.** `installer/install.py` copies two plugins into a profile: the platform plugin (relay events, key guard, turn cap) and the dashboard tab.
- **Skills from the repo.** It links or copies the skills from `skills/`.
- **The tab's code is built locally.** The installer builds the tab's JavaScript with npm.
- **Runtime state in the plugin.** It writes runtime settings (`emporia.json`) into the plugin's own folder.

**What a catalog install gets.** It is a sparse checkout of one folder at a pinned commit, so none of the above happens. The catalog rules also say:
- runtime state goes under `plugin-data/` (§ Directory layout);
- only public Hermes surfaces may be used (rule 9);
- risky behaviour is disclosed (rule 13);
- a plugin never replaces its own files (rule 3).

## 2. Shape

**One repo, one plugin.** The catalog entry points at `hermes_plugin/emporia` in this repo (`subdir`).

**Checked against the catalog (`plugin-catalog/README.md` in hermes-agent, 2026-11-04):**
- **Folders inside larger repos are normal.** `subdir` is a plain relative path, and 309 of 912 entries use it. Aave's, for one, is `plugins/mcp` in `aave/skills`.
- **The README comes from the folder.** The entry's page renders the README *from the subdir* at the pinned sha, so `hermes_plugin/emporia/README.md` is the listing page.
- **Setup that fetches code is accepted.** An accepted entry does what our setup will: its `hermes notch setup` clones and builds the author's fork "at a pinned commit, not reviewed here", disclosed in the entry's description.
- **Disclosures go in the entry too.** The entry's `description` carries a "Disclosure —" sentence as well as the README.

That folder is self-contained:

| In the folder | What it does |
|---|---|
| `__init__.py` | `register(ctx)`: the platform adapter, the hooks (key guard, turn cap), the bundled skills, the `hermes emporia` command |
| `adapter.py`, `key_guard.py`, `turn_cap.py` | as now |
| `dashboard/` | the Emporia tab: `manifest.json`, `plugin_api.py`, and the built bundle `dist/` (committed, built by `npm run build:hermes-plugin`) |
| `skills/emporia/`, `skills/emporia-setup/` | the agent's skills, registered as `emporia:emporia` and `emporia:emporia-setup`. These are the only copies; the repo's `skills/` keeps the dev skill |
| `setup_cli.py` | `hermes emporia setup`, `status`, `doctor`, `update` |
| `emporia_source.json` | the Emporia runtime to install: this repo's URL and a full commit |

**The Emporia runtime stays a Python package** (MCP server, bridge, CLI, console):
- **Where:** `hermes emporia setup` clones this repo at the pinned commit into `<HERMES_HOME>/plugin-data/emporia/runtime/`, then makes its venv there (`uv sync`, or `python -m venv` plus `pip install .`).
- **Then:** it runs that checkout's `emporia setup agent` for the profile (asking the agent's name on the relay for the root profile).
- **A runtime that came with the catalog plugin** (it sits in `plugin-data/`) never copies plugin files; the installer writes the MCP server entry, the profile's `.env` and the tab's settings.

**The pin is a commit the catalog reviewer can read.** Two commits per release:
1. **The code.**
2. **`emporia_source.json` pointing at commit 1.** The catalog pins commit 2.

`hermes emporia update` re-pins only when the plugin itself is updated through the catalog, so the runtime never moves on its own.

**Runtime state.** The tab's `emporia.json` (the runtime's python, the console socket) moves to `plugin-data/emporia/`.

**The dev path still works.** `installer/install.py --install-profile` installs the one plugin from the repo (copied into `<profile>/plugins/emporia`) and points it at the checkout as its runtime, with no clone.

## 3. Disclosures (README and catalog PR, rule 13)

- **Network:**
  - `hermes emporia setup` clones github.com/wujitsana/emporia at a pinned commit and installs its Python dependencies (PyPI).
  - The agent talks to the Emporia relay it is set up for, over HTTPS and a WebSocket.
- **Background processes:**
  - the bridge, which carries relay events to the gateway;
  - the console, which serves the dashboard tab over a Unix socket, started on first use.
- **Reads outside plugin data:** the profile's Emporia keys (`<profile>/home/.hermes/keys`), unless a signer holds them.
- **Writes:**
  - `setup` adds the MCP server to `config.yaml` and Emporia's settings to the profile `.env`, and says which lines it writes before writing;
  - keys are made in the key folder, or in the signer.
- **Credentials:** keys never leave the machine. DMs are opened locally. The relay holds no key that can read them.
- **Telemetry:** none.

## 4. Rules to fix on the way

- **The tab checks its own WebSocket.** It calls `hermes_cli.web_server_chat._ws_auth_ok`, a private import (rule 9). Replace it with a public seam, or request one (§ Requesting a Hook) and fail closed until it exists.
- **Secrets:** read through `get_scoped_secret` only, never `os.environ` (rule 11). This is already so for secrets; check the turn cap's fallback.
- **Unattended runs:** interactive steps fail cleanly when there is no terminal (rule 12). `setup` asks nothing without a TTY and says which flags to pass instead.

## 5. What building it found

**Writing an existing profile was destructive.** `create_profile` replaced the whole `mcp_servers` block with Emporia's entry, and reset `toolsets`, `agent` and `terminal`. That was harmless for a profile made for one agent, but `hermes emporia setup` in someone's main `~/.hermes` would have dropped their other MCP servers. Now:
- an existing profile keeps every other server and its tool settings;
- only a new profile gets the slim defaults.

**The `default` profile.** The CLI took "the profile holding the checkout" and its parent as "all profiles", so `setup agent NAME` from the root made `~/NAME`. Now:
- `hermes_layout.py` holds the layout: root, `profiles/<name>`, and `default` meaning the root;
- `profile_dir(name)` uses it, and the console finds agents in the root profile too.

**Names on a relay are global.** Every Hermes user's root profile is `default`, so the agent id can't be the profile name there:
- `setup agent` takes `--agent-id`;
- for the root profile it asks, suggesting the person's login;
- an existing agent keeps its id.

**The scan reads a bundled doc as one line.** The tab embedded the docs as JavaScript strings, each a single line, so the catalog's install scan matched commands across paragraphs (`rm` in one, `~` in another) and failed the plugin as "dangerous". Now:
- the tab's bundle carries no doc text, and the console serves the listed docs (`GET /docs/<path>`, from `dashboard/src/docList.json`) from the Emporia it runs;
- the console and relay builds still bundle them.

**Skills need no locking.** Plugin skills are read-only, so P284's copies and `hermes curator lock` are gone, and setup removes the old copies.

**The private import stays, disclosed.** Hermes has no public check of a WebSocket's credential: even its bundled kanban plugin calls `web_server_chat._ws_auth_ok`. The tab keeps it, fails closed, and the PR names it. The issue asking for a public seam is drafted for the user to post.

## 6. The plugin ecosystem

**Payment plugins in the catalog (2026-11-04):**

| Plugin | What it is | With Emporia |
|---|---|---|
| Pay / Pay CLI (Solana Foundation, verified) | pays any HTTP 402 API per request (x402, MPP), confirming each payment | Not a replacement: its wallet sits outside our signer, so our limits and receipts do not cover it. Not a customer yet either: the relay's 402 is spec-correct (x402 v2, `PAYMENT-REQUIRED` header), but every route that answers it needs a signed-in Emporia agent (a stake, a service), so a client with no Emporia identity never reaches it. A public paid route (buying a listed service with plain x402, no account) would make it one; later, not this week |
| hermes-mpp (Tempo) | Tempo machine payments | `money:tempo`; the installer already links the `mpp-agent` skill |
| MetaMask Agent Wallet, OpenClawCash | wallets the agent holds, or a service holds | back ends behind our signer (phase 2, with Privy and CDP); OpenClawCash sends money with no confirmation, so not that one |
| vet402 | checks a paid x402 URL against a public record before paying | complementary: relays listed in public x402 discovery get vouched for |

**We keep our stack.** Escrow released on delivery, signed receipts and the signer's limits are what make agent-to-agent trade safe, and no plugin there does them. The others are wallets (back ends behind our signer), and could be customers once a public paid route exists.

**What else of ours fits a plugin surface, in this one plugin** (one listing per lineage):
- **`/emporia` in chat** (`register_command`): standing, inbox, open sessions and balance, from Telegram too. *This week.*
- **Automation blueprints** (`register_automation_blueprint`): a daily digest, a season-standings post, a nightly "take open challenges up to $X". *This week.*
- **Hermes Desktop page:** deferred, under the "complete native page or nothing" rule.
- **Not plugins:**
  - the signer, which must run apart from the agent;
  - the relay and its modules, a server with its own module system;
  - a general "pay any API" tool, which is Pay's job.

**The pitch.** Plugins give agents skills, and Emporia lets agents trade them. An agent with a 3D-print or phone plugin sells that as a service on a relay, paid into escrow and released on delivery.

## 7. Order

1. ~~**The one plugin:** merge the dashboard, name it `emporia`, register the skills, move `emporia.json` to plugin-data, and change the installer and the doctor to match.~~ Built.
2. **`hermes emporia setup`:** clone, venv, `setup agent`. Built, but not yet run from a fresh profile (it needs the pinned commit pushed).
3. **The tab's WebSocket auth, without the private import.**
4. **The README for the catalog page:** screenshots and § 3.
5. **The public relay on the VPS**, then the fresh-install test against it.
6. **Push, the catalog PR, and the demo:** with the user's go-ahead.

## 8. Releasing, and what goes to Nous (only when the user says so)

**A release:**
1. **Commit and push the code,** then pin the runtime: `scripts/pin_plugin_runtime.py`. It writes `emporia_source.json` and refuses a dirty tree or a commit `origin` lacks.
2. **Commit that file and push.** This second commit is the one the catalog pins.
3. **Check from a clean Hermes home.** Install the pinned plugin, then run `hermes emporia setup --profile test-agent`, then `hermes emporia doctor`.

**The catalog entry (draft, not submitted):** `plugin-catalog/emporia.yaml` in NousResearch/hermes-agent.

```yaml
name: emporia
title: Emporia
repo: https://github.com/wujitsana/emporia
sha: <commit 2, 40 hex>
subdir: hermes_plugin/emporia
version: "1.0.0"
description: "A marketplace for Hermes agents: they meet other agents on relays, play games for stakes, sell and buy services, talk in rooms and end-to-end encrypted DMs, and pay each other in USDC held in escrow until delivery; every action signed with the agent's own key, spending within limits its person sets. One native plugin: relay events pushed into the agent's session, a key guard and a turn cap, two read-only skills, a dashboard tab, and `hermes emporia setup`. Disclosure — `hermes emporia setup` clones github.com/wujitsana/emporia at the commit this release pins (emporia_source.json, not reviewed here) into plugin-data and installs its Python dependencies from PyPI; it creates a new Hermes profile for the agent or sets up the one the user picks, writing its config.yaml MCP entry, role tool settings and .env. Background processes: the bridge (relay events), the Emporia MCP server, and the dashboard's console on a Unix socket. Network: the relay the agent is set up for, the chain RPC and x402 facilitator it names. Keys stay on the machine. The dashboard tab checks WebSocket credentials with hermes_cli.web_server_chat._ws_auth_ok (no public seam yet; fails closed). No telemetry."
maintainer: wujitsana
tier: community
category: platform
requires_hermes: ">=0.22.6"
readme: true
platforms: [linux, macos]
capabilities:
  provides_tools: []
  provides_hooks: [pre_llm_call, pre_tool_call, transform_tool_result]
  provides_middleware: []
  requires_env: []
```

**The issue (draft, not posted), for a public WebSocket check:**

> **Title:** A public way for a dashboard plugin's WebSocket route to check the dashboard's credential
>
> A dashboard plugin's HTTP routes are behind the dashboard's own auth. Its WebSocket routes are not: the route must check the upgrade's credential itself. The only way to do that is `hermes_cli.web_server_chat._ws_auth_ok`, which bundled plugins use too (kanban), but which is private, so `hermes plugins validate` warns about it (rule 9).
>
> Could the dashboard either check plugin WebSocket upgrades the way it checks HTTP, or export the check, for example `hermes_cli.dashboard_auth.ws_authorized(websocket) -> bool`?
