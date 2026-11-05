# Emporia and the Hermes plugin catalog

An audit of the Emporia Hermes plugin (`hermes_plugin/emporia/`) against the 18 admission
rules in Hermes's `plugin-catalog/README.md` (Hermes 0.22.6) and its submission guide, with
evidence, the validator's output, and how each rule is met.

- The draft entry is in [`docs/catalog/emporia.yaml`](catalog/emporia.yaml).
- The draft PR text is in [`docs/catalog/PR.md`](catalog/PR.md).
- The draft Hermes issue for a public WebSocket check is in
  [`docs/catalog/hermes-issue-ws-auth.md`](catalog/hermes-issue-ws-auth.md).
- Everything here is on branch `catalog-rule11`, which is built on `catalog-compliance`.

**What the plugin is.** A `kind: platform` plugin. It registers:
- three hooks;
- a gateway platform (`emporia`);
- two skills;
- the `hermes emporia` CLI command;
- a dashboard tab.

The agent's Emporia tools are not plugin registrations. They are an MCP server,
`emporia.mcp_server` (91 tools), in Emporia's *runtime* (`src/emporia`).
`hermes emporia setup` installs that runtime from this same repo, at a pinned commit, into
`<HERMES_HOME>/plugin-data/emporia/runtime`, and adds the server to the profile's
`config.yaml`. A reviewer therefore reads two things at the pin: the plugin subdir, and the
runtime it installs.

## Summary

| # | Rule | Verdict | How it is met |
|---|------|---------|---------------|
| 1 | Human-merged gate | pass | Submitted as a PR to hermes-agent by the owner; nothing automated. |
| 2 | Exact SHA pins | pass | The entry pins a full sha at release, and the plugin pins its runtime by commit and tree (release procedure below). |
| 3 | No self-updating code | pass | Nothing fetches or installs code by itself. The runtime comes only from the pin, verified by commit and tree after every checkout. `--runtime-from` is gone. |
| 4 | SHA bumps are new PRs | pass | The runtime pin lives in the plugin tree, so it moves only with a new entry PR. |
| 5 | Owner submissions | pass | Submitted by the repo owner. |
| 6 | Capabilities match | pass | Generated from Hermes's own probe, and kept in step with the manifest by a test. |
| 7 | Install scanner | pass | `security scan — safe`, with no findings. |
| 8 | Desktop SDK surface | pass (n/a) | No `desktop/plugin.js`. |
| 9 | No core overrides | pass | No rebinds. One reviewer warning (a private import): no public route exists, the bundled kanban plugin is precedent, it fails closed, and an issue is drafted. |
| 10 | Dependency policy | pass | The plugin declares no Python dependencies. The runtime's are bounded and locked (`uv lock --check` passes). |
| 11 | Credentials through Hermes | pass | Every profile setting and key is read and written through Hermes. The MCP server gets them as `${NAME}`. Nothing opens a profile's `.env` or `auth.json`. Nous verification is opt-in. |
| 12 | Approvals, unattended runs | pass | No child inherits an approval bypass. Nothing waits on a terminal that is not there. |
| 13 | Disclosure | pass | The README and PR text disclose network calls, background processes, reads outside plugin-data, shell commands and stored credentials. |
| 14 | Compatibility metadata | pass | `requires_hermes: ">=0.22.6"` in both places (the current release); `version` matches. |
| 15 | No skins/forks of bundled | pass | Not a bundled plugin. |
| 16 | One listing per lineage | pass | Original work. |
| 17 | No unscannable payloads | pass | No binaries or bytecode. The dashboard bundle is unminified source with a checked digest. |
| 18 | Password managers from vendor | pass (n/a) | No `register_login_backend`. |

## Validator

`hermes plugins validate hermes_plugin/emporia`, run on a `git archive` of each HEAD in a
throwaway `HERMES_HOME` and `HOME`. It gave the same output before this work
(`hackathon-v2`) and after it (`catalog-rule11`):

```
✓ manifest — plugin.yaml parses
✓ manifest fields — name, version, description present
✓ requires_hermes — spec '>=0.22.6' parses
✓ config schema — not declared
✓ requires_env — all entries UPPER_SNAKE
✓ oauth — not declared
✓ loadable — entry: __init__.py
✓ python dependencies — none declared
✓ capability probe — register() ran in isolation
✓ declared tools — matches registrations
✓ declared hooks — matches registrations
✓ declared middleware — matches registrations
✓ built-in tool collisions — no tools to check
✓ security scan — safe
✓ no core override — no runtime rebinds of Hermes core
⚠ private core imports (catalog rule 9 ...): dashboard/plugin_api.py:131 hermes_cli.web_server_chat.
◇ Isolation — in-process only: kind 'platform' ...
Validation passed.
```

- **No failures.** Two lines remain, both explained in the PR text:
  - the rule 9 warning (below);
  - the isolation note, which every platform adapter gets (platform adapters run in the gateway
    process).
- **`--install-deps`** (what the catalog CI runs) has nothing to install. Hermes's own reader
  (`pm.plugin_declarations.read_python_declaration`) reports no `pyproject.toml` and no
  requirements for the plugin dir. It was not run here: it syncs the Hermes install's own
  environment, which is shared with the live gateway.
- `scripts/validate_plugin_catalog.py docs/catalog/emporia.yaml` passes.

## Rule by rule

### 2. Exact SHA pins — pass
A release is two commits (`scripts/pin_plugin_runtime.py`):
1. The code, pushed.
2. `hermes_plugin/emporia/emporia_source.json` naming commit 1, by its `sha` and its `tree`.

The entry pins commit 2. Between releases the pins are empty, and
`tests/test_plugin_setup.py` checks they are either empty or full 40-character hashes. The
draft entry's sha is a quoted placeholder, which the entry validator accepts and the release
replaces.

### 3. No self-updating code — pass
- **Nothing fetches or installs code by itself.**
  - The gateway's adapter starts only the bridge.
  - The bridge may restart a relay this machine runs, from the installed code (`Popen` of
    `emporia.relay_server`), and installs nothing.
  - The dashboard API starts only the console.
  - The hooks, the MCP server and the console start no installer.
  - The plugin registers no cron job.
  - The runtime is installed or moved only by `install_runtime`
    (`hermes_plugin/emporia/setup_cli.py`), which only `hermes emporia setup` and
    `hermes emporia update` call when a person runs them.
- **Only the pin.** `install_runtime` takes no source but the https repo and commit in
  `emporia_source.json`. `--runtime-from` is removed from setup and update, and a test checks
  it cannot come back.
- **Verified.** After a forced checkout, and again after the packages are installed,
  `verify_runtime` checks three things, and refuses to run anything otherwise:
  - HEAD is the pinned commit (a full sha names one commit; no branch, tag or ref is followed);
  - that commit's tree is the recorded tree;
  - no tracked file differs.

  `hermes emporia status` says "pinned, verified" or what differs.
- **The runtime's own `emporia update`** (git pull, sync) refuses a catalog runtime before it
  does anything.
- Tests: `tests/test_plugin_setup.py` (`test_the_catalog_build_has_no_way_to_run_another_runtime`,
  `test_the_runtime_is_verified_against_the_pinned_commit_and_tree`, …) and
  `tests/test_cli.py::test_a_catalog_runtime_is_never_pulled_or_synced_by_emporia_update`.

### 6. Declared capabilities — pass
Hermes's probe records hooks `pre_tool_call`, `transform_tool_result` and `pre_llm_call`, no
tools, no middleware, and the `emporia` command. The entry lists exactly those hooks, and as
`requires_env` the three settings the platform requires (`adapter.py`, `required_env`).
`tests/test_catalog_entry.py` keeps the entry, the manifest and `register()` in step.

### 9. No core overrides — pass, one reviewer warning
- `no core override` passes.
- The warning is `dashboard/plugin_api.py:131`, an import of
  `hermes_cli.web_server_chat._ws_auth_ok`, used to authenticate the tab's WebSocket.
- Hermes offers no public way to check a socket's credential:
  - `hermes_cli.dashboard_auth` has ticket helpers, not the full check;
  - `PluginApiWebSocketGate` refuses disabled plugins but leaves the credential to the route.
- The bundled kanban plugin imports the same function.
- If the import fails, the tab refuses the socket (fail closed).
- `docs/catalog/hermes-issue-ws-auth.md` drafts the issue asking for a public helper.
- Re-implementing the check from the ticket helpers would risk drifting from core's own rules,
  which is worse.

### 10. Dependency policy — pass
- The plugin declares no Python dependencies.
- The runtime's `pyproject.toml` gives every dependency a floor and an upper bound.
- `uv.lock` was regenerated with uv 0.13.5. It includes the `solana` extra, and
  `uv lock --check` passes.
- The installer's own `uv sync --frozen --no-dev --extra x402` installs into a fresh copy of the
  tree.
- `tests/test_dependency_bounds.py` keeps every runtime dependency bounded and the locked
  versions inside the ranges.

### 11. Credentials come through Hermes — pass
- **The plugin's in-process code** reads its settings with
  `gateway.platforms._shared.get_scoped_secret`, the platform wrapper around
  `agent.secret_scope.get_secret`. This covers the adapter, `hermes emporia` and the turn cap.
- **The bridge** (started by the plugin) gets the profile's settings, keys and vault variables
  from the adapter, read through Hermes inside the profile's scope, with `EMPORIA_NO_DOTENV=1`.
- **The MCP server** (started by Hermes) reads no `.env`.
  - Its `mcp_servers.emporia.env` carries a `${NAME}` for every setting and key it may read,
    and Hermes fills each from the profile's scope.
  - `emporia.agent_env` holds the names, and `tests/test_agent_env.py` scans the source and
    fails on a missing one, so no setting can silently miss the server.
  - `emporia doctor` names anything not handed over, and `emporia agent refresh` adds it.
- **The runtime CLI** (`hermes emporia …`), **the console** and **the installer** never open a
  profile's `.env`. `emporia.profile_env` is the one place that reads or writes one, and it does
  so through Hermes's own interpreter with Hermes's public functions:
  - read: `agent.secret_scope.load_env_file`;
  - write: `hermes_cli.config.save_env_value` / `remove_env_value`, under the profile's
    `HERMES_HOME`.

  Values travel on stdin and a pipe, never in argv. A runtime installed from the catalog with
  no Hermes fails closed. Only a relay's own folder, or a checkout used without Hermes, is a
  plain file. `tests/test_profile_env.py` checks the routing, that no other module opens a
  `.env`, and a real round trip through the installed Hermes.
- **`auth.json`:** never read, refreshed or written. The installer and the seed script ask
  Hermes (`nous_token.from_hermes`).
- **Nous verification** (sending the person's Nous access token to the relay) is off by default
  (`EMPORIA_NOUS_VERIFY`). Hermes has no route for proving a sign-in to a third party, so it is
  documented as an opt-in outside the catalog's guarantees.
- **Key protection.** Keys in a child's environment are behind `key_guard`, which blocks
  `/proc/<pid>/environ` and `ps e` for the agent's own tools.
- **Keys in a password manager** (P293): the recommended path is the signer's. `emporia signer
  add NAME --key-source ROLE=cmd:op read op://…` (or `cmd:bw get password ITEM`) has the signer
  read each key itself, at its start, with the vault token kept in the signer's folder
  (`backends/vault.secret`, 0600) or the signer user's own environment. The agent's profile
  names no vault and holds no token, so nothing of the vault passes through the agent's
  settings at all. The command is a person's (`key_guard` refuses it from an agent's tools;
  `tests/test_signer_phase2.py`). Trade-off, disclosed: a password manager stores keys but does
  not sign, so the key is in the signer's memory while it runs.
- **Linked keys on the agent's side** (a `cmd:` key source run with a vault CLI in the agent's
  process) still work, are **not recommended**, and remain a disclosed known issue: the vault
  token is in the profile's settings. Off unless the person links one.
- **The console** lists agents through Hermes's reads. Branch `console-scope` (another agent's
  work) narrows it to the dashboard's own profile. Merge that branch after this one: it touches
  the same `_profile_env`, which here only changes how it reads.

### 12. Approvals and unattended runs — pass
- No child of `hermes emporia` or of the bridge inherits `HERMES_YOLO_MODE`, `HERMES_YOLO`,
  `HERMES_ACCEPT_HOOKS` or `HERMES_ACP_AUTO_APPROVE`.
- Without a terminal, the runtime's CLI gets `/dev/null` as stdin.
- Setup asks nothing without a terminal.
- Every child has a timeout.
- `key_guard` sends commands that change keys, wallets, limits or roles to Hermes's approval
  prompt, which an unattended run denies.
- A payment above the person's threshold asks through MCP elicitation, which Hermes routes to
  its approvals.

### 13. Disclosure — pass
The plugin README (it renders on the catalog page at the pin) and the PR text disclose:
- network calls: the clone, PyPI, the relay and the relays it visits, the testnet RPCs, the
  facilitator, opt-in Nous verification, and Telegram or Stripe only if set up;
- background processes: the bridge, the MCP server and the console;
- reads outside plugin-data: the keys folder, the profile's settings through Hermes, the
  console's agent list, and session records;
- shell commands;
- stored credentials;
- no telemetry;
- known issues: linked keys, and Linux and macOS only.

Payments are on testnets. Any other payment network is a person-only unlock in the person's own
terminal, which the key guard blocks from the agent; the README says so.

### 14. Compatibility metadata — pass
- `requires_hermes: ">=0.22.6"` is in `plugin.yaml` and the entry. It is the current Hermes
  release, and the turn cap's `pre_llm_call` block needs it.
- The entry's `version: "1.0.0"` is `plugin.yaml`'s.

### 17. No unscannable payloads — pass
- A header scan of every tracked file found no ELF, Mach-O, PE, wasm, archive or bytecode. The
  only binaries are three images outside the plugin.
- `dashboard/dist` is an unminified IIFE built from `dashboard/src`
  (`npm ci && npm run build:hermes-plugin`). `dist/SOURCE` is a digest of its inputs, and
  `tests/test_dashboard_plugin.py` fails when they change without a rebuild.
- No source map is needed, because the output is not minified.

### 1, 4, 5, 8, 15, 16, 18 — pass
- **1, 4, 5:** process rules. The owner submits, and a re-pin is a new PR.
- **8, 18:** do not apply. There is no Desktop plugin and no login backend.
- **15, 16:** Emporia is not a bundled plugin, and not a fork.

## What a release still needs
1. Push `catalog-rule11` (after review) and run `scripts/pin_plugin_runtime.py`: one commit
   naming the runtime's sha and tree.
2. Put that commit's sha in the entry, and the screenshots at `docs/catalog/screenshots/`
   (from branch `retromod-ux`, once the UI merges), at URLs pinned to that sha.
3. Re-run `hermes plugins validate` at that commit, and open the PR with `docs/catalog/PR.md`.
