# Emporia and the Hermes plugin catalog

An audit of the Emporia Hermes plugin (`hermes_plugin/emporia/`) against the 18 admission
rules in Hermes's `plugin-catalog/README.md` (Hermes 0.22.6), with file:line evidence, the
validator's output, and what is left to decide.

- The draft entry is in [`docs/catalog/emporia.yaml`](catalog/emporia.yaml).
- The draft PR text is in [`docs/catalog/PR.md`](catalog/PR.md).
- Line numbers are those of the `catalog-compliance` branch.

**What the plugin is.** A `kind: platform` plugin. It registers three hooks, a gateway platform
(`emporia`), two skills, the `hermes emporia` CLI command and a dashboard tab. The agent's
Emporia tools are not registered by the plugin: they are an MCP server, `emporia.mcp_server`,
with 91 tools. That server lives in Emporia's *runtime* (`src/emporia`), which
`hermes emporia setup` clones at a pinned commit of this same repo into
`<HERMES_HOME>/plugin-data/emporia/runtime`, and adds to the profile's `config.yaml`.
A reviewer therefore reads two things at the pin: the plugin subdir, and the runtime it installs.

## Summary

| # | Rule | Verdict | In one line |
|---|------|---------|-------------|
| 1 | Human-merged gate | pass | A PR to hermes-agent, nothing automated. |
| 2 | Exact SHA pins | risk | The entry's sha is a placeholder until release; `emporia_source.json` has an empty sha at this commit. |
| 3 | No self-updating code | risk | The runtime is fetched at a pinned SHA from inside the reviewed tree: defensible. `--runtime-from` lets a person point it anywhere. |
| 4 | SHA bumps are new PRs | pass | A runtime bump is a plugin commit too, so it can only reach users through a new entry PR. |
| 5 | Owner submissions | pass | Submitted by the repo owner (wujitsana). |
| 6 | Capabilities match | pass | Generated from Hermes's own probe: 3 hooks, no tools, no middleware, 3 env vars. |
| 7 | Install scanner | pass | `security scan — safe`. |
| 8 | Desktop SDK surface | n/a | No `desktop/plugin.js`. The tab is a web-dashboard plugin. |
| 9 | No core overrides | pass, 1 caution | No rebinds. One private import (`hermes_cli.web_server_chat._ws_auth_ok`), the same one the bundled kanban plugin uses. |
| 10 | Dependency policy | pass (fixed) | The plugin declares no Python deps. The runtime's had bare floors; now each has an upper bound. |
| 11 | Credentials through Hermes | **fail** | The plugin's own code is clean, but the runtime it starts reads profile `.env` files and gets the Nous token from Hermes's internals. A script in the tree reads and rewrites `auth.json`. Proposal below. |
| 12 | Approvals, unattended runs | pass (fixed) | Children no longer inherit YOLO or pre-accepted hooks. The runtime's CLI never waits on a dead stdin. |
| 13 | Disclosure | pass (fixed), 1 decision | The README now covers the Nous token, cross-profile reads, the local relay restart and default hosts. One wording decision is for the owner. |
| 14 | Compatibility metadata | pass | `requires_hermes: ">=0.22.6"` in plugin.yaml and the entry (= the current release); `version: "1.0.0"`. |
| 15 | No skins/forks of bundled | pass | Not a bundled plugin. |
| 16 | One listing per lineage | pass | Original work. |
| 17 | No unscannable payloads | pass | No binaries or bytecode in the tree. The dashboard bundle is unminified JS built from `dashboard/src`, with a source digest a test checks. |
| 18 | Password managers from vendor | n/a | No `register_login_backend`. |

## Validator

`hermes plugins validate hermes_plugin/emporia`, at a `git archive` of HEAD, in a throwaway
`HERMES_HOME` and `HOME`:

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
⚠ private core imports (catalog rule 9: public surfaces only; these can break on any release):
  dashboard/plugin_api.py:131 hermes_cli.web_server_chat. ...
◇ Isolation — in-process only: kind 'platform': gateway platform adapters run in the Hermes process; ...
Validation passed.
```

The output was the same before and after the fixes on this branch: they changed no check's
result. `--install-deps` is a no-op for this plugin, because it declares no Python
dependencies (`python dependencies — none declared`). It also syncs Hermes's own environment,
so it was not run against the shared Hermes install.

**What is left, and why:**
- **The private import** is a caution, not a failure. Hermes has no public WebSocket-auth
  helper for dashboard plugins. The bundled kanban plugin
  (`plugins/kanban/dashboard/plugin_api.py`) uses the same function. If it is missing, the tab
  refuses the socket (`plugin_api.py:130-137`, fail closed). The right fix is a Hermes issue
  asking for a public helper.
- **Isolation** is `in-process only` because platform adapters cannot run in the plugin host.
  That is a note, not a check.

## Rule by rule

### 2. Exact SHA pins — risk
- `hermes_plugin/emporia/emporia_source.json:3` is `"sha": ""` at this commit, so
  `hermes emporia setup` from a catalog install would refuse (`setup_cli.py:195`, "names no
  pinned commit").
- **A release is two commits** (`scripts/pin_plugin_runtime.py`):
  1. the code, pushed;
  2. `emporia_source.json` naming commit 1.
- The entry pins commit 2. Commit 2 differs from commit 1 only in `emporia_source.json`, so the
  runtime the plugin installs is the reviewed tree minus that one line. The PR text tells the
  reviewer how to check it: `git diff <runtime-sha> <entry-sha> --stat` shows one file.

### 3. No self-updating code — risk, defensible
- **What happens.** `setup_cli.install_runtime` (`setup_cli.py:188-218`) clones this repo
  (`:201`) and checks out the sha in `emporia_source.json`. It then installs dependencies with
  `uv sync --frozen` (`:212`), or `pip install -e` within the pyproject bounds (`:217`).
  `hermes emporia update` (`cmd_update`, `:426`) re-runs the same install at the sha the
  installed plugin pins.
- **Why it is not a self-updater.**
  - It never touches the plugin's own files.
  - It never chooses a commit itself: the sha is inside the reviewed commit.
  - It moves only when `hermes plugins update emporia` brings a new reviewed pin. That needs a
    new catalog PR (rule 4).
  - `status` says when the runtime is not the pinned one (`runtime_note`, `:365-378`).
- **What a reviewer will still notice.**
  - **Code that runs is fetched at setup time.** The scanner reads only the subdir. The answer:
    the fetched code is the same repo at an ancestor of the pin, so it is in the tree the
    reviewer already has.
  - **`--runtime-from REPO@SHA`** (`parse_runtime_from`, `:172-185`; setup and update) lets a
    person install a runtime from any https or local repo at any commit. It is manual and never
    automatic. From an agent's terminal it needs the person's approval: `key_guard._SENSITIVE`
    puts `setup` and `update` under Hermes's approval prompt (`key_guard.py:57-60`), and an
    unattended run denies it. It is still a way to run code nobody reviewed.
- **How to present it** (in the PR text): "Setup installs Emporia's runtime from this repo at
  the commit `emporia_source.json` names. That commit is an ancestor of the pinned one and
  differs from it only in that file. The runtime moves only when a new catalog pin does.
  `--runtime-from` is a developer flag for trying a release before it is pushed."
- **Options for `--runtime-from`** (owner's choice, not changed here):
  - **(a)** Keep it, presented as above.
  - **(b)** Accept only local repos (a path or `file://`). That keeps "try a release before it
    is pushed" and drops fetching unreviewed code over the network.
  - **(c)** Strip it from the catalog build.
- (b) is a few lines in `parse_runtime_from` plus one test.

### 6. Declared capabilities — pass
Generated from Hermes's own probe (`hermes_cli.plugin_validate._run_capability_probe`), not
written by hand:

```json
{"hooks": ["pre_tool_call", "transform_tool_result", "pre_llm_call"], "tools": [],
 "middleware": [], "commands": ["emporia"], "providers": [], "trusted_inbound": []}
```

- The hooks match `plugin.yaml:25-28`, registered at `__init__.py:43-46`.
- `requires_env` in the entry lists the three variables the gateway platform requires
  (`adapter.py:350`): `EMPORIA_RELAY_URL`, `EMPORIA_AGENT_ID`, `EMPORIA_PYTHON`. setup writes
  them. The manifest declares them under `optional_env` rather than `requires_env`, so the
  install itself asks nothing.
- The 91 MCP tools are not plugin registrations. The entry's description and the PR say where
  they come from.

### 7. Install scanner — pass
`security scan — safe`, with no findings.

### 9. No core overrides — pass, one caution
`no core override — no runtime rebinds of Hermes core`. The one private import is covered above.
The other Hermes imports are on the validator's public list:
- `gateway.platforms._shared`, `.base`, `.event`, `.helpers`;
- `gateway.config`;
- `plugins.plugin_storage`;
- `hermes_constants`.

### 10. Dependency policy — pass (fixed)
- The plugin declares no Python dependencies; it runs on what Hermes ships.
- The runtime's `pyproject.toml` had bare floors. Each now has an upper bound: the next major
  (0.x: the next minor) above the version `uv.lock` pins. The lock's own copy of the project's
  requirements says the same.
- `tests/test_dependency_bounds.py` keeps every runtime dependency bounded, and the locked
  versions inside the ranges.
- **Left for the owner:**
  - The floors were not re-tested at their oldest versions.
  - `uv.lock` was regenerated with uv 0.13.5. It now includes the `solana` extra, and
    `uv lock --check` passes. The installer's own `uv sync --frozen --no-dev --extra x402`
    installs into a fresh copy of the tree.
  - A release quarantine (`uv lock --exclude-newer`) in CI is recommended by the rule.

### 11. Credentials come through Hermes — fail, proposal below
- **The plugin's own in-process code is compliant:**
  - the platform's settings: `adapter.py:57` and `:333`;
  - `hermes emporia`: `setup_cli.py:74-80`;
  - the turn cap: `turn_cap.py:40-49`.
- All three read through `gateway.platforms._shared.get_scoped_secret`, the platform-adapter
  wrapper around `agent.secret_scope.get_secret`. It honours the active profile, and the
  default profile's startup.
- The `os.environ` fallbacks in `setup_cli.py:80` and `turn_cap.py:49` run only when Hermes's
  module cannot be imported.
- The runtime processes the plugin starts, or that setup configures, are not compliant:

| Where | What it reads or does | Started by |
|-------|-----------------------|------------|
| `adapter.py:104`, `:151` → `src/emporia/config.py:110-112` | The bridge gets `EMPORIA_DOTENV` (the profile's `.env` path) and merges that file into its environment. | the plugin (gateway) |
| `src/emporia/config.py:114-121` | With no `EMPORIA_DOTENV`, it walks up from the runtime's own location to the first `config.yaml` and reads the `.env` beside it. For a runtime in `plugin-data`, that is the profile's `.env`. | every runtime process |
| `installer/install.py:798` → `config.py:110` | The MCP server's `config.yaml` env names the `.env`; the server merges it. | Hermes (as setup configured) |
| `plugin_api.py:101-111` → `src/emporia/console/agents.py:72-82` | The console runs with `EMPORIA_NO_DOTENV=1`, but lists agents by reading the `.env` of **every profile under the Hermes root** that has an Emporia keys folder. | the plugin (dashboard) |
| `src/emporia/relay_access.py:37-45` | Re-reads the `.env` while running (a relay's access settings). | a relay process |
| `installer/install.py:566` (`harvest_env`), used by `cli.py:3694`, `:3700` and others | `emporia doctor`, setup and others read profile `.env` files directly. | `hermes emporia …` |
| `installer/install.py:605-625` (`collect_upstream_env`), `:517-550` | Reads the `.env` of every ancestor directory, the Hermes root's included. Copies Stripe settings and a model-provider key from another profile's `.env` into a new one. | `hermes emporia setup` |
| `src/emporia/key_source.py:123-162` | Writes keys into the profile's `.env` (`--keys env`). | runtime |
| `src/emporia/key_source.py:43-57` | `cmd:` key sources run a password manager's CLI with the token kept in the `.env` (`OP_SERVICE_ACCOUNT_TOKEN`, `BW_SESSION`, …). | runtime |
| `src/emporia/nous_token.py:28`, `:72-90` → `mcp_server.py:435`, `:447` | Runs Hermes's interpreter on `hermes_cli.auth.resolve_nous_access_token` (an internal API). The person's **Nous access token** goes to the relay at registration. | MCP server, CLI |
| `installer/install.py:122-205` | Reads `auth.json` (including a fixed data-root path, `:144`). Refreshes the Nous OAuth token as client `hermes-cli`, and **writes the new tokens back to `auth.json`** (`:201`). Reached only by `python installer/install.py` (`:1980`) and `scripts/seed_demo_relay.py`, not by `hermes emporia setup`, but it is in the pinned tree. | the repo's own installer script |

**Hermes's sanctioned routes:**
- **In-process code:** `agent.secret_scope.get_secret(NAME)`, or for a platform
  `get_scoped_secret`, for names declared in `requires_env` / `optional_env` / `config_schema`.
  Already used.
- **MCP servers:** Hermes renders `${NAME}` in `mcp_servers.<name>.env` from the active
  profile's secret scope (`tools/mcp_tool_config.py`, `_interpolate_env_vars` → `get_secret`).
  It spawns the server with a filtered environment (`_build_safe_env`), and drops an entry that
  is one unset `${NAME}`. So the MCP server can be handed exactly its settings and keys, from
  Hermes, without reading any file.
- **A subprocess the plugin starts itself** (the bridge, the console): read the values with
  `get_scoped_secret` in the gateway or dashboard process, and pass them in the child's
  environment. That is what Hermes does for MCP servers.
- **Writing a setting:** `hermes_cli.gateway_setup_wizard:save_env_values`, a public helper for
  platform plugins.
- **A third party verifying a Nous sign-in:** no route. `credentialed_provider_request` attaches
  a token to the provider's own hosts; a relay is not one. Per the rule, this is a Hermes issue
  (an audience-bound identity assertion), not a workaround.

**Key protection if values move into a child's environment:**
- **Today.** A key merged into the bridge's or MCP server's `os.environ` after start does not
  show in `/proc/<pid>/environ`. The `.env` file itself is on disk, and `key_guard` blocks
  commands and paths that name the key variables or the keys folder.
- **After.** With `${NAME}` or env passing, the keys are in the child's initial environment,
  visible in `/proc/<pid>/environ` to the same user.
  - Hermes's file tool already refuses `/proc/*/environ` (`tools/file_tools.py:130-133`).
  - The agent's terminal does not.
  - So **before** any key moves into an environment, `key_guard._KEY_PATHS` should also block
    `/proc/<pid>/environ` (and `self`), and `ps e` / `ps eww`. That is a one-regex change with
    a test.
- **The wall is unchanged:** a sandboxed terminal backend, or the signer apart from the agent.

**Options, smallest first:**

1. **Remove the `auth.json` path** (small; recommended in any case). Delete
   `_find_auth_json` / `_try_refresh_nous` from `installer/install.py`, and have its
   `resolve_nous_token` use `nous_token.from_hermes`. It races Hermes's own rotating refresh
   token, which `nous_token.py`'s docstring itself names as the hazard. This also drops the
   fixed data-root path. About 80 lines removed, and `tests/test_installer.py:315` changes.
2. **Settings and keys through Hermes for the processes the plugin starts** (medium):
   - **Setup writes `${NAME}` references.** `mcp_servers.emporia.env` lists every Emporia
     setting and key name the profile uses as `${NAME}`, plus `EMPORIA_NO_DOTENV=1`, and drops
     `EMPORIA_DOTENV` (`installer/install.py:786-808`).
   - **The bridge gets its values from the gateway.** `adapter._bridge_env` reads those names
     with `get_scoped_secret` and passes them with `EMPORIA_NO_DOTENV=1`.
   - **The console stops reading `.env` files.** Each agent's setup records its key-source
     references (they are references, not keys) in that profile's
     `plugin-data/emporia/emporia.json`. The tab passes the dashboard profile's own values,
     read with `get_scoped_secret`. Seeing *other* profiles' agents goes through Hermes's
     `?profile=` scope (`_plugin_route_secret_scope`), one profile per request, instead of a
     file walk.
   - **`config.py`'s parent-directory walk is removed** for a runtime in `plugin-data`.
   - **The `key_guard` `/proc` rule lands first.**
   - **What stays:** the runtime's CLI, run by a person as `hermes emporia …`, still reads and
     writes the profile's `.env` for setup and doctor. The rule is about the plugin, and that
     is the person's own command. Writes can move to `save_env_values`.
   - Estimate: 1-2 days with tests, mostly in `installer/install.py`, `config.py`,
     `console/agents.py` and `adapter.py`.
3. **Nous verification** (a decision first):
   - **(a)** Make it opt-in (`EMPORIA_NOUS_VERIFY=1`), off by default in the catalog build,
     and disclose it.
   - **(b)** Drop it from the catalog build until Hermes has an identity-assertion route, and
     open that issue.
   - Either way, stop calling `hermes_cli.auth` from a subprocess.
   - Today it hands a bearer token for the person's Nous account to a relay operator.
4. **Linked keys** (`cmd:` sources). Move them onto Hermes's secret-source plugins, so a vault
   token is not kept in the `.env`, or list them as known issues. `key_source.py:12-14` explains
   why they were kept apart: a Hermes password-manager unlock is per chat session, and an agent
   signs unattended. That is a fair Hermes issue to open.

None of these is implemented on this branch: each changes how keys reach a signing process, and
2 and 3 change behaviour the owner chose. The fixes here keep every key where it was.

### 12. Approvals and unattended runs — pass (fixed)
- **Fixed: no approval bypass reaches a child.** `setup_cli._clean_env` (`setup_cli.py:133-145`)
  and `adapter._bridge_env` (`adapter.py:87-89`, `:147-149`) no longer pass on
  `HERMES_YOLO_MODE`, `HERMES_YOLO`, `HERMES_ACCEPT_HOOKS` or `HERMES_ACP_AUTO_APPROVE`. The
  children are git, pip, `hermes -p NAME plugins install`, `hermes profile create`, the
  runtime's CLI and the `hermes` commands it runs, and the bridge with the relay it may start.
  Tests: `tests/test_plugin_setup.py`, `tests/test_platform_bridge.py`.
- **Fixed: no hang on a dead stdin.** `setup_cli._emporia` (`:223-229`) gives the runtime's CLI
  `/dev/null` as stdin when there is no terminal, so a question ends at once instead of waiting
  on a pipe. Test: `test_the_runtimes_cli_never_waits_on_a_stdin_nobody_writes_to`.
- **Already so:**
  - Setup without a terminal asks nothing and exits 2 with the flags to pass (`:304-308`).
  - Every `hermes` / git / pip child has `stdin=DEVNULL` and a 900 s timeout (`:151`, `:274`,
    `:285`).
  - `key_guard.pre_tool_call` returns Hermes's `approve` action for commands that change keys,
    wallets, limits or roles (`key_guard.py:181-192`), and `block` for person-only ones. Hermes
    denies an approval in cron and other unattended runs.
  - A payment above the agent's threshold asks through MCP elicitation, which Hermes routes to
    its approval prompts (`mcp_server.py:171-192`). It returns "no answer" when it cannot ask.
  - The turn cap blocks relay-started turns past the daily cap (`turn_cap.py`).
  - The plugin never auto-approves anything.

### 13. Disclosure — pass (fixed), one decision
- **Corrected in `hermes_plugin/emporia/README.md`** (it renders on the catalog page at the pin):
  - the Nous access token sent to the relay at registration;
  - the console reading every Emporia agent profile's keys folder and `.env` under the Hermes
    root;
  - the bridge restarting a local relay;
  - the default facilitator and testnet RPC hosts;
  - Telegram and Stripe, only when set up;
  - the console's socket folder;
  - the turn counter, now in `plugin-data` (`turn_cap.py`; it was at the profile root);
  - an honest line on which code reads `.env` files;
  - `--runtime-from`.
- The PR text carries the same as disclosure lines.
- **Decision for the owner:** the README's "Testnet first" paragraph names the person-only
  command that unlocks payment networks beyond testnets. The entry and PR text say testnet only.
  Either keep that paragraph, which is truthful, or strip the unlock from the catalog build.

### 14. Compatibility metadata — pass
- `plugin.yaml:22` and the entry both say `requires_hermes: ">=0.22.6"`. The `pre_llm_call`
  block the turn cap relies on needs it, and it is the current release, not newer.
- `version: "1.0.0"` is `plugin.yaml:4`.
- The runtime package (`pyproject.toml`, 0.2.0) and the dashboard tab (`manifest.json`, 0.3.0)
  carry their own labels. Aligning them before release would spare a reviewer the question.

### 17. No unscannable payloads — pass
- A header scan of every tracked file found no ELF, Mach-O, PE, wasm, archive or bytecode. The
  only non-text files are three images in `dashboard/`, outside the plugin.
- `dashboard/dist/index.js` and `style.css` are a **built** bundle, but readable source:
  - not minified (`dashboard/vite.plugin.config.ts:80`, `minify: false`);
  - one IIFE with its own React;
  - built from `dashboard/src` in the same repo by `npm run build:hermes-plugin`.
- `dashboard/dist/SOURCE` is a digest of the inputs. `tests/test_dashboard_plugin.py:128-130`
  fails when `dashboard/` changes without a rebuild.
- A source map is not needed, because the output is not minified.
- **Recommended:** a short build note in the PR (Node version, `npm ci`, the command), and a CI
  job that rebuilds and diffs `dist/`. The digest proves the bundle matches the inputs as of the
  last build, not that the build reproduces byte for byte.

### 1, 4, 5, 8, 15, 16, 18
- **1, 4, 5:** process rules; the entry is prepared locally and submitted by the owner.
- **8, 18:** do not apply. There is no Desktop plugin and no login backend.
- **15, 16:** pass. Emporia is not a bundled plugin, and not a fork.
