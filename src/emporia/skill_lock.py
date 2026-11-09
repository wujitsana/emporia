"""Skill locks: Hermes 0.23 `hermes curator lock`, so no other agent can talk this one into rewriting its skills.

A lock is a person's declaration in the profile's `skills/.usage.json`. While it holds, the agent cannot change
the skill through `skill_manage`, `write_file`, `patch` or the terminal, in a normal turn or a background pass; the
skill stays readable and auto-loadable, and the person can still edit it. Hermes enforces it (`tools/skill_lock`);
this module only asks Hermes for it, through its public CLI, and never writes `.usage.json` itself.

What can be locked. A lock is keyed by a skill's folder name in the profile's own `skills/` folder. Emporia's own
skills (`emporia:emporia`, `emporia:emporia-setup`) are not in that folder: the plugin registers them read-only from
its own folder, which no agent tool can edit, and `hermes curator lock` does not find them ("no skill named ...").
So the lock goes on what *is* in the profile and steers the agent: the payment skills setup links in (`stripe-link-cli`,
`stripe-projects`), each only if it is there. Nothing is claimed for a skill that is not.

Person-only: the key guard holds `emporia skill-lock on|off` from an agent's tools, and Hermes refuses
`hermes curator unlock` to the agent itself.
"""

from __future__ import annotations

from pathlib import Path

from emporia import rounds as _hermes

#: Skills setup links into a profile beside the plugin's own (installer `_install_skill_link`).
PAYMENT_SKILLS = ("stripe-link-cli", "stripe-projects")
#: Emporia's own skills: registered by the plugin, read-only, not lockable (see the module note).
PLUGIN_SKILLS = ("emporia:emporia", "emporia:emporia-setup")
UNDO = "emporia skill-lock off  (or `hermes -p NAME curator unlock SKILL`)"


class SkillLockError(RuntimeError):
    """What went wrong, safe to show."""


def present(profile_dir: Path) -> list[str]:
    """The lockable skills in this profile's `skills/` folder, by folder name."""
    root = Path(profile_dir) / "skills"
    if not root.is_dir():
        return []
    found: set[str] = set()
    for md in root.rglob("SKILL.md"):
        name = md.parent.name
        if name in PAYMENT_SKILLS:
            found.add(name)
    return sorted(found)


def _curator(profile: str, verb: str, skill: str) -> str:
    try:
        return _hermes.run_hermes([*_hermes._scope(profile), "curator", verb, skill])
    except _hermes.RoundsError as e:
        raise SkillLockError(str(e)) from e


def lock(profile: str, profile_dir: Path) -> tuple[list[str], list[str]]:
    """Lock every lockable skill of the profile: (the names locked, the lines to tell the person)."""
    locked, notes = [], []
    for name in present(profile_dir):
        try:
            _curator(profile, "lock", name)
            locked.append(name)
        except SkillLockError as e:
            notes.append(f"{name}: not locked ({e})")
    if not locked and not notes:
        notes.append("no Emporia payment skill is in this profile, so there is nothing to lock")
    notes.append("Emporia's own skills (" + ", ".join(PLUGIN_SKILLS) + ") come from the plugin, read-only: Hermes "
                 "has no lock for them and no agent tool can edit them")
    return locked, notes


def unlock(profile: str, profile_dir: Path) -> list[str]:
    done = []
    for name in present(profile_dir):
        try:
            _curator(profile, "unlock", name)
            done.append(name)
        except SkillLockError:
            pass
    return done
