"""Where Hermes keeps its profiles (P295), in one place.

Hermes has a root home (``~/.hermes``, the profile named ``default``) and named
profiles under ``<root>/profiles/<name>``. Emporia used to assume it was
installed inside a named profile, so it took that profile's parent as "every
profile" and could not set up the default one: ``setup agent NAME`` from the
root made ``~/NAME``. Installed by ``hermes emporia setup``, the runtime sits
in ``<home>/plugin-data/emporia/runtime`` of whichever profile ran it.
"""

from __future__ import annotations

from pathlib import Path

DEFAULT = "default"


def root_of(profile: Path) -> Path:
    """The Hermes root a profile belongs to (the profile itself for the root)."""
    return profile.parent.parent if profile.parent.name == "profiles" else profile


def name_of(profile: Path) -> str:
    return profile.name if profile.parent.name == "profiles" else DEFAULT


def profiles(root: Path) -> list[Path]:
    """Every profile under a root, the root itself first when it is one."""
    named = sorted(p.parent for p in (root / "profiles").glob("*/config.yaml"))
    return ([root] if (root / "config.yaml").exists() else []) + named


def containing_profile(path: Path) -> Path | None:
    """The Hermes profile a file sits in: the nearest parent with a config.yaml."""
    for parent in path.resolve().parents:
        if (parent / "config.yaml").exists():
            return parent
    return None


def key_dirs(root: Path) -> list[Path]:
    """Every profile's Emporia keys folder that exists."""
    return [d for p in profiles(root) if (d := p / "home" / ".hermes" / "keys").is_dir()]
