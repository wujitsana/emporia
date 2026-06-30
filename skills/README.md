# Emporia skills (canonical in this repo)

Skills **ship with the repo**. The Hermes profile uses **symlinks** into this tree — edits here update the profile in place. Reload skill in chat after big edits (`skill_view` or new session).

## Layout

```
emporia/skills/
  README.md
  emporia.md                 → emporia/SKILL.md
  emporia/SKILL.md             skill: emporia
  dev/
    emporia-dev/
    srcl-terminal-ui/
```

`related_skills` in frontmatter is optional (Hermes hints only). Repo skills omit it — load by name when needed.

## Profile symlinks

| Repo | Profile |
|------|---------|
| `skills/emporia` | `skills/emporia` |
| `skills/emporia.md` | `skills/emporia.md` |
| `skills/dev/emporia-dev` | `skills/software-development/emporia-dev` |
| `skills/dev/srcl-terminal-ui` | `skills/creative/srcl-terminal-ui` |

## Installer

```bash
python installer/install.py --install-profile
python installer/install.py --install-profile --dev-skills
```