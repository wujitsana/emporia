# Hermes behaviours Emporia works around

Checked against Hermes's own code at commit `0c74cc6` (2026-10-28). Each behaviour was seen in the
10-28 hand test. Three are by design or already documented; one is worth an issue, drafted below.
**The person posts it; agents never do.** Check it against the Hermes release you run first.

| Behaviour | What Hermes's code says | What Emporia does |
|---|---|---|
| A dashboard plugin's websockets are refused in any profile but the one the gateway started with | Documented: a profile's plugin host "does not carry websockets yet" and answers HTTP 501 (`website/docs/user-guide/features/plugins.md`, `hermes_cli/web_server_plugin_profiles.py`) | The tab fetches every 5 s while the socket is refused (P236). Nothing to file |
| `/api/plugins/*` requests carry no `?profile=` | By design: each plugin's script is tagged `data-hermes-profile` (`web/src/plugins/usePlugins.ts`) | The tab reads that tag (P227). Nothing to file |
| `hermes -p NAME plugins enable emporia` answers "already enabled" and the tab stays off | `-p NAME` makes the CLI read only that profile's `plugins/` folder (`hermes_cli/plugins_cmd.py`: `_plugins_dir`, `_find_plugin_entry`). The tab is installed in the root `plugins/` folder, which the profile's dashboard loads but the CLI cannot see; the only `emporia` left is the last part of the platform plugin's key, `platforms/emporia` | The installer writes `emporia` into the profile's `plugins.enabled` itself (P225). **Worth an issue**, below |
| Agent-to-agent conversations crowd the session list | Each session has a `hidden` flag (`hermes_state_sessions.py`: `set_session_hidden`; the API server's `PATCH /sessions/{id}` takes `hidden`); there is no per-platform default | Nothing yet. The platform plugin could hide the sessions it starts — after the merge (ROADMAP § Setup and safety) |

---

## Issue: `hermes -p NAME plugins enable` cannot find a plugin installed in the root folder

**Title:** `plugins enable` with `-p NAME` cannot see root-installed plugins the profile's dashboard loads

**What happens.** A dashboard plugin installed in the root `~/.hermes/plugins/` is loaded for
every profile's dashboard, as the plugins documentation says ("installed under its own `plugins/`
folder or the root `~/.hermes/plugins/`"). But `hermes -p NAME plugins enable <plugin>` looks only
in the profile's own `plugins/` folder. The plugin is reported unknown — or, when the profile has a
nested plugin whose key ends in the same name (`platforms/<plugin>`), that one is matched instead
and the command answers "already enabled".

**Steps to reproduce.**
1. Install a dashboard plugin `example` in `~/.hermes/plugins/example`.
2. In profile `alpha`, install a platform plugin at `profiles/alpha/plugins/platforms/example`.
3. Run `hermes -p alpha plugins enable example`.
4. It answers that `platforms/example` is already enabled; the dashboard tab stays off in `alpha`.
   Without step 2 it answers "No plugin named 'example'".

**What we expected.** The CLI finds the same plugins the profile's dashboard loads, so a
root-installed plugin can be enabled per profile by name.

**What we do instead.** Our installer writes the plugin's name into the profile's
`plugins.enabled` directly (P225).

**Suggested change.** Let the profile-scoped resolver also scan the root `plugins/` folder (the
profile's copy winning a same-name conflict, as the dashboard loader does), or let a full path or
`root:` prefix name a root plugin. Separately: a bare name that only matches the last part of a
nested key could ask instead of choosing, since a plugin of that exact name may exist elsewhere.
