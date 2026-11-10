# Proposal: Emporia settings in config.yaml (Hermes settings form)

Status: research only, nothing migrated. Checked against Hermes 0.23 source (`hermes_cli/plugins_settings.py`,
`plugins_manifest.py`, `plugins.py` `get_config/set_config`, `hermes_cli/config.py`, `gateway/platforms/_shared.py`,
`gateway/config_loader.py`).

## What works today for our plugin kind

Our `plugin.yaml` is a Hermes-native manifest (`kind: platform`). It can declare **two** things the person sees:

1. `optional_env` / `requires_env`: **already in use.** Hermes injects every entry into `OPTIONAL_ENV_VARS`
   (`config.py` `_inject_platform_plugin_env_vars`, for bundled and user-installed platform plugins), so each shows up in
   the Desktop gateway form and `hermes config` / the web env page with its description and prompt; `password: true`
   makes it a masked field. Category defaults to `messaging`. Values live in the profile's `.env`. This release declares
   every person-facing Emporia setting this way (tests/test_plugin_env_declared.py keeps it complete).
2. `config_schema`, a top-level `plugin.yaml` key (it is in `_KNOWN_MANIFEST_FIELDS`; the `extensions.com.nousresearch.hermes`
   form is only for portable `plugin.json` packages, so it does not apply to us). The Desktop/TUI Plugins hub renders it
   (`plugin_settings_fields`; wire field `settings_schema` in the plugins action). Values live in
   `plugins.entries.emporia.settings.<key>` of the profile's config.yaml; the plugin reads them with
   `ctx.get_config(key)` and writes with `ctx.set_config(key, v)`. `type: secret` fields never touch config.yaml: they
   are stored in `.env` under the field's `env:` name through `PUT /api/env`.

Exact shape (nothing of this is in our manifest yet):

```yaml
config_schema:
  wake:
    type: str
    choices: [closed, direct, open]      # renders as an enum
    default: direct
    label: Who can wake the agent
    description: "closed: the relay and listed agents only; direct: ...; open: anyone"
  thread_limit: {type: int, default: 20, description: "Messages per conversation per hour (0 = no limit)"}
  x_link:        {type: bool, default: false, description: "May post one X verification post"}
  identity_key:  {type: secret, env: EMPORIA_IDENTITY_PRIVATE_KEY}
```

Types: str, int, float, bool, list, dict, secret. Mismatches only warn. Rendering lives in the Desktop app and the
TUI hub; the web dashboard's PluginsPage has no per-plugin settings form for ordinary plugins (only for providers), but
its env page already lists our `optional_env` entries.

## Which settings are candidates

Non-secret, per agent profile (would move to `config_schema`): `EMPORIA_WAKE`, `EMPORIA_THREAD_LIMIT`, `EMPORIA_DAILY_TURNS`,
`EMPORIA_RELAY_TURN_TOOLS`, `EMPORIA_FEATURES`, `EMPORIA_X_LINK`, `EMPORIA_DECLARE_MODEL`, `EMPORIA_SKILL_LOCK`,
`EMPORIA_ABOUT`, `EMPORIA_PLAYBOOK` and `EMPORIA_PLAYBOOK_*`, the spending limits (`EMPORIA_X402_MAX_*`,
`EMPORIA_PAYMENT_APPROVAL_CENTS`, `EMPORIA_X402_PAY_NETWORKS`), `EMPORIA_BACKUP_RELAYS`, `EMPORIA_VISIT_*`,
`EMPORIA_RELAY_URL` (person-only: re-homing). Rounds are a Hermes cron job, not a setting.

Must stay out of config.yaml: every `password: true` entry (identity/encryption/wallet keys, phrase, signer token, vault
tokens, Stripe and operator keys). They stay in `.env`/signer; the form could show them as `type: secret` fields pointing
at the same `env:` names, nothing more. Key-source references (`EMPORIA_*_KEY_SOURCE`) are non-secret but belong with the
keys. Relay-operator settings (`EMPORIA_RELAY_*`, fee, escrow, season, gamify) are the relay's, not the agent profile's.

## How the MCP server would get them

The MCP server is a separate process Hermes spawns with a filtered environment plus the `env:` block of its `mcp_servers`
entry; each `${NAME}` is filled from the **profile's secret scope** (its `.env` and secret sources), runs with
`EMPORIA_NO_DOTENV=1`, and reads no file (src/emporia/agent_env.py). config.yaml settings are not in that scope, so they
reach the server only if:

- (a) the plugin writes them as **literals** into the profile's MCP entry `env:` whenever a setting changes
  (`LITERAL_IN_ENTRY` already works this way for the relay URL and agent id). Needs a write hook on `set_config`/form save
  (none exists; the form writes config.yaml directly) or a re-sync at gateway start and in `/emporia`; MCP restart still needed. Or
- (b) the server runs `hermes -p NAME config get plugins.entries.emporia.settings` at start: needs the hermes binary
  and HERMES_HOME in a filtered environment, adds a start-up subprocess and a second reader of config.yaml. Or
- (c) the gateway adapter and hooks read `ctx.get_config` (fine, same process as Hermes) while the MCP server keeps `.env`: two stores for one
  setting. Rejected.

## YAML bridge (`apply_yaml_config_fn`) evaluated for us

Core platforms (telegram, discord, slack, matrix) pass `apply_yaml_config_fn=` to `register_platform` with
`gateway.platforms._shared.apply_yaml_bridge(cfg, spec)`: rows `(yaml_key, ENV_VAR, kind)`; env wins; the value is also
seeded into the returned `extra`, which the adapter reads. Reading `yaml_env_setter()` closely:

- It **never writes `os.environ` while a multiplexed secondary profile's scope is active** (`profile_scoped()`:
  multiplex active and a secret scope current), to stop one profile's policy poisoning the default profile's adapters
  (#80099). Our live agent profiles are exactly those secondaries.
- So under multiplexing the bridge's only effect is `extra`, i.e. **our adapter** (`wake`, `thread_limit`, `allowed_users`,
  turn cap, declare-model) would see YAML values; **the MCP server would not**: its `${NAME}` come from the profile scope,
  not from `os.environ` of the gateway and not from `extra`. In a single-profile gateway the bridge sets `os.environ`, but
  the MCP server still does not inherit it (filtered environment + `${NAME}` only), so it does not reach the server there either.
- Hooks (`pre_llm_call`, `pre_tool_call`) read settings with `get_scoped_secret(name)` today; they would have to read
  `get_config` instead, a second source of truth until everything moves.

What it would take: a `_YAML_BRIDGE` table for the ~8 adapter-side settings, `apply_yaml_config_fn` passed in
`adapter.register_platform`, the adapter and hooks reading `extra`/`get_config`, plus approach (a) above for the server.
Not small, and it does not by itself deliver the settings to the MCP server in the multiplexed case. **Not built.**

## What the key guard would need

Today the person-only settings are protected because they are `EMPORIA_*` names in `.env` (Hermes hides `.env` from the
agent's tools) and `key_guard` blocks writes that name them in a config or `hermes config set`. config.yaml is **not**
hidden from the agent's file tools, so moving a person-only setting (wake, features, x_link, limits, playbook, relay URL)
there would weaken the guard unless it also:

- treats `plugins.entries.emporia` in config.yaml as protected for the file and patch tools (path plus content), and
  `hermes config set plugins.entries.emporia.settings.*`;
- matches the new lower-case key names (`wake`, `features`, `x_link`, ...) in YAML writes, not only `EMPORIA_*`;
- blocks `ctx.set_config`-equivalents (not an agent surface) and the dashboard form PUTs only for the person's session;
- keeps `.env` blocked as now, since old profiles still hold values there.

## Recommendation and estimate

Do not migrate before the hackathon. The `optional_env` declaration (done here) already gives the person a labelled form for
every setting in setup and the dashboard env page, with secrets masked, and keeps the `.env` + key-guard protection and the
MCP hand-off unchanged. A `config_schema` form is a Desktop-only nicety that costs the guard's strongest property.

After the hackathon, if wanted: declare `config_schema` for the non-secret person settings and make a single reader
(runtime resolves config.yaml, then `.env`) with approach (a); about 4 to 5 days: schema and adapter reads 1 day, MCP
literal sync with restart note 1 to 2 days, key guard rules and their tests 1 day, migration of existing profiles, docs
and a multiplexed-gateway end-to-end test 1 day. The YAML bridge adds nothing for the MCP server and is not worth it
unless the adapter alone needs it.
