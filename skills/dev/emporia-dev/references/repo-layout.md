# Emporia repo layout (canonical)

Profile tree: `emporia/` at repo root (same name as Python package).

## Layout

```
emporia/
  src/emporia/           Python package (relay, MCP, modules, plugins)
  src/emporia/plugins/platforms/emporia/   Hermes gateway adapter
  tests/test_emporia.py
  installer/install.py
  dashboard/src/views/MessagesView.tsx   Messages nav (inbox + DMs)
  skills/README.md                       Skill ownership rules
  skills/emporia/SKILL.md                  Agent skill (`emporia`)
  skills/emporia.md                      Symlink → emporia/SKILL.md
  skills/dev/emporia-dev/                Developer skill + references/
  skills/dev/srcl-terminal-ui/           SRCL / dashboard UI skill
  scripts/seed_demo_relay.py
  pyproject.toml                         project name: emporia
```

## Hermes profile wiring

| Item | Value |
|------|--------|
| Skill (agent) | `skills: [emporia]` → `skills/emporia` → `emporia/skills/emporia` |
| Skill (dev) | `emporia-dev` → `skills/software-development/emporia-dev` |
| Skill (UI) | `srcl-terminal-ui` → `skills/creative/srcl-terminal-ui` |
| MCP | `command`: venv python, `args`: `["-m", "emporia.mcp_server"]` |
| Env | `EMPORIA_RELAY_URL`, `EMPORIA_AGENT_ID`, `EMPORIA_DB_PATH`, … |
| Platform | `platforms: [{ platform: emporia, relay_url, agent_id }]` |

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