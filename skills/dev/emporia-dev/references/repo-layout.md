# Emporia repo layout (canonical)

Profile tree: `emporia/` at repo root (same name as Python package).

## Layout

```
emporia/
  src/emporia/           Python package (relay, MCP, modules, plugins)
  src/emporia/plugins/platforms/emporia/   Bridge for the Hermes adapter (tunnel, frames)
  hermes_plugin/emporia/                   The Hermes plugin (P295): platform, hooks, skills/, dashboard/, `hermes emporia`
  tests/test_emporia.py
  installer/install.py
  dashboard/src/views/MessagesView.tsx   Messages nav (inbox + DMs)
  dashboard/src/ui/kit/                  vendored Sacred-style UI kit (12 components, global.css)
  skills/README.md                       Skill ownership rules
  hermes_plugin/emporia/skills/emporia/  Agent skill (`emporia:emporia`, registered by the plugin)
  skills/dev/emporia-dev/                Developer skill + references/
  skills/dev/srcl-terminal-ui/           dashboard UI kit (Sacred style; vendored in dashboard/src/ui/kit)
  scripts/seed_demo_relay.py
  pyproject.toml                         project name: emporia
```

## Hermes profile wiring

| Item | Value |
|------|--------|
| Skill (agent) | `emporia:emporia`, registered by the plugin from its own `skills/` (P295) |
| Skill (dev) | `emporia-dev` → `skills/software-development/emporia-dev` |
| Skill (UI, dashboard UI kit: Sacred style, vendored in dashboard/src/ui/kit) | `srcl-terminal-ui` → `skills/creative/srcl-terminal-ui` |
| MCP | `command`: venv python, `args`: `["-m", "emporia.mcp_server"]` |
| Env | `EMPORIA_RELAY_URL`, `EMPORIA_AGENT_ID`, `EMPORIA_DB_PATH`, … |
| Plugin | `<profile>/plugins/emporia`, `plugins.enabled: [emporia]` |

## Verify after path or env edits

```bash
cd emporia
uv run --group dev python -m pytest tests/test_emporia.py -q
```

Then `/reload-mcp` in Hermes.

## Full rebrand / naming sweep (operator request)

When the user asks to drop old product names entirely:

1. **Do not** keep env fallbacks to retired prefixes in `env_config` — `EMPORIA_*` only.
2. **Do not** add migration docs that enumerate retired directory or package names in the active repo (deletes confusion; git history holds the old names).
3. **Dashboard:** Messages tab → `MessagesView.tsx`; CSS `.e-messages-pane*`, not `CommsView` / `e-comms-pane`.
4. **Sweep:** ripgrep retired strings across `emporia/`, `skills/`, `config.yaml`, `.env` (exclude `node_modules`, `.venv`).
5. **Tests:** `tests/test_emporia.py` (not `test_comms.py`).

Wording: prefer **in-session chat**, **Messages**, **DM threads** — avoid generic “comms” as a product label in user-facing copy and file names.