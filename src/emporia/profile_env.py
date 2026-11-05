"""The one place Emporia reads or writes a Hermes profile's `.env` (catalog rule 11).

A profile's `.env` is Hermes's: it is the store Hermes builds the profile's secret scope
from. So every read and write of one goes through Hermes (`hermes_settings`) whenever
Hermes is installed on this machine — which it always is for a runtime installed from
the plugin catalog. Only where there is no Hermes at all (a relay host, a checkout used
without Hermes) is the file read or written directly; a runtime installed from the
catalog never does that: it fails closed and says why.

A `.env` that is not a Hermes profile's (no config.yaml beside it: a relay's own folder,
a checkout's) is not Hermes's store, and is read and written as a file.
"""

from __future__ import annotations

import os
from pathlib import Path

from emporia import hermes_settings


class NoRoute(RuntimeError):
    """A catalog runtime asked for a profile's settings with no Hermes to ask."""


def is_profile_env(path: Path) -> bool:
    path = Path(path)
    return path.name == ".env" and (path.parent / "config.yaml").exists()


def _from_catalog() -> bool:
    here = Path(__file__).resolve().parents[2]       # <runtime>/src/emporia/profile_env.py
    return here.parent.name == "emporia" and here.parent.parent.name == "plugin-data"


def through_hermes(path: Path) -> bool:
    """Whether `path` is read and written through Hermes (True), or as a file (False)."""
    if not is_profile_env(path):
        return False
    if hermes_settings.available():
        return True
    if _from_catalog():
        raise NoRoute(f"{path.parent.name}'s settings are Hermes's to read, and Hermes cannot be found "
                      "(EMPORIA_HERMES_BIN, or `hermes` on PATH): nothing was read or changed")
    return False


def _parse(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip().removeprefix("export ").strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def read(path: Path, *, fresh: bool = False) -> dict[str, str]:
    """Every NAME=value of the `.env` at `path` ({} when there is none)."""
    path = Path(path).expanduser()
    if through_hermes(path):
        return hermes_settings.read(path.parent, fresh=fresh)
    try:
        return _parse(path.read_text(errors="replace"))
    except OSError:
        return {}


def lines(path: Path) -> list[str]:
    """The `.env` as lines, for code that edits a list of NAME=value lines and writes it back
    with `replace`. Through Hermes, one NAME=value line per setting (the file's comments stay
    in the file: `replace` only sets and removes names)."""
    path = Path(path).expanduser()
    if through_hermes(path):
        return [f"{k}={v}" for k, v in hermes_settings.read(path.parent).items()]
    try:
        return path.read_text().splitlines()
    except OSError:
        return []


def _write_file(path: Path, text: str) -> None:
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(text)
    os.chmod(path, 0o600)


def update(path: Path, updates: dict[str, str] | None = None, unset=()) -> list[str]:
    """Set `updates` and remove `unset`, keeping every other line. Returns the names whose
    value changed (set to something new, or removed)."""
    path = Path(path).expanduser()
    updates = {str(k): str(v) for k, v in (updates or {}).items()}
    unset = [str(n) for n in unset if str(n) not in updates]
    if through_hermes(path):
        # Fresh: a value read earlier in this process may have changed since, and a set judged
        # "already so" from it would be skipped.
        have = hermes_settings.read(path.parent, fresh=True)
        changed = [k for k, v in updates.items() if have.get(k) != v] + [n for n in unset if n in have]
        if changed:
            hermes_settings.write(path.parent, {k: updates[k] for k in changed if k in updates},
                                  [n for n in changed if n not in updates])
        return changed
    lines = path.read_text().splitlines() if path.exists() else []
    changed, seen, kept = [], set(), []
    for line in lines:
        bare = line.strip().removeprefix("export ").strip()
        key = bare.split("=", 1)[0].strip() if "=" in bare and not bare.startswith("#") else ""
        if key in unset:
            changed.append(key)
            continue
        if key in updates:
            seen.add(key)
            if line != f"{key}={updates[key]}":
                line = f"{key}={updates[key]}"
                changed.append(key)
        kept.append(line)
    for key, value in updates.items():
        if key not in seen:
            kept.append(f"{key}={value}")
            changed.append(key)
    if changed or not path.exists():
        _write_file(path, "\n".join(kept) + "\n")
    return list(dict.fromkeys(changed))


def replace(path: Path, lines: list[str]) -> list[str]:
    """Make the `.env` at `path` hold exactly what `lines` say (NAME=value lines; comments
    and blank lines are kept only for a plain file). Through Hermes, that is the names that
    differ set, and the names gone removed. Returns the names changed."""
    path = Path(path).expanduser()
    want = _parse("\n".join(lines))
    if through_hermes(path):
        have = hermes_settings.read(path.parent, fresh=True)
        return update(path, {k: v for k, v in want.items() if have.get(k) != v},
                      [n for n in have if n not in want])
    before = read(path)
    _write_file(path, "\n".join(lines) + "\n")
    return sorted({k for k in set(before) | set(want) if before.get(k) != want.get(k)})
