# Emporia skills

**The agent's skills ship in the Hermes plugin** (P295):
- `hermes_plugin/emporia/skills/emporia`: using Emporia.
- `hermes_plugin/emporia/skills/emporia-setup`: installing, checking and repairing it.

The plugin registers them read-only as `emporia:emporia` and `emporia:emporia-setup`, so neither the agent's own tools nor Hermes's background review can rewrite them, and nothing is copied into a profile's `skills/`. An installer from before P295 copied or linked them there; `emporia agent refresh NAME` removes those copies.

This folder keeps the **developer** skills, linked into a profile with `--dev-skills`:

| Repo | Profile |
|------|---------|
| `skills/dev/emporia-dev` | `skills/software-development/emporia-dev` |
| `skills/dev/srcl-terminal-ui` | `skills/creative/srcl-terminal-ui` |

## Installer

```bash
hermes emporia setup                                   # from the catalog plugin
python installer/install.py --install-profile          # from a checkout
python installer/install.py --install-profile --dev-skills
```
