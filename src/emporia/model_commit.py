"""A declared model on a game, committed with each move and revealed at the end.

A player that declares its model signs, with each move, `model_commit = sha256(model || salt)` (a salt it
keeps). It hides the model while the game is on: an opponent reading the record cannot see it and adapt. When the
game is over, each player reveals, signed, `{step, model, salt}` for the steps it committed; the relay checks each
reveal against the commit it stored and the record shows the models. The outcome never depends on it: a reveal
that is missing or does not match is shown as such, and the game stands as played.

Everything here is **declared, never verified**: the relay cannot tell which model produced a move, only that the
player is bound to the name it revealed.

Pure: no I/O. `LocalSalts` is the client's own memory of its salts (a JSON file beside its keys).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
from pathlib import Path
from typing import Any

PREFIX = b"emporia:model:v1"
_HEX = re.compile(r"[0-9a-f]{32,64}")
MAX_REVEALS = 2000

#: how a player's reveal reads on the record
REVEALED = "revealed"      # every committed step revealed and matching
MISMATCH = "mismatch"      # at least one reveal does not match its commit
MISSING = "missing"        # committed, and no (or a partial) reveal
NONE = "none"              # the player declared no model on this game


def new_salt() -> str:
    return secrets.token_hex(16)


def commit(model: str, salt: str) -> str:
    """`sha256(model || salt)` as hex, domain-separated: `emporia:model:v1`, 0x00, the model, 0x00, the salt."""
    return hashlib.sha256(PREFIX + b"\0" + str(model).encode() + b"\0" + str(salt).encode()).hexdigest()


def commit_problem(value: Any) -> str:
    """Why this is not a `model_commit`, or ""."""
    if not isinstance(value, str) or not _HEX.fullmatch(value):
        return "model_commit is a sha-256 hex digest"
    return ""


def reveals_problem(reveals: Any) -> str:
    """Why this is not a list of reveals (`[{step, model, salt}]`), or ""."""
    from emporia import identity_card
    if not isinstance(reveals, list) or not reveals or len(reveals) > MAX_REVEALS:
        return f"reveals is a list of 1 to {MAX_REVEALS} of {{step, model, salt}}"
    seen: set[int] = set()
    for r in reveals:
        if not isinstance(r, dict) or set(r) != {"step", "model", "salt"}:
            return "each reveal is {step, model, salt}"
        if not isinstance(r["step"], int) or isinstance(r["step"], bool) or r["step"] < 0 or r["step"] in seen:
            return "each reveal names its own step (a whole number, once)"
        seen.add(r["step"])
        if not identity_card.clean_model(r["model"]):
            return "a revealed model is a model name (letters, digits and . _ : / @ + -, at most 80)"
        if not isinstance(r["salt"], str) or not 8 <= len(r["salt"]) <= 128 or not r["salt"].isprintable():
            return "a salt is 8 to 128 printable characters"
    return ""


def check_reveals(commits: dict[int, str], reveals: list[dict[str, Any]]) -> dict[int, str]:
    """`{step: "ok" | "mismatch" | "unknown"}`: each reveal against the commit stored for that step. A step
    with no stored commit is `unknown` (it commits nothing, so it reveals nothing)."""
    out: dict[int, str] = {}
    for r in reveals:
        want = commits.get(int(r["step"]))
        out[int(r["step"])] = ("unknown" if want is None else
                               "ok" if commit(r["model"], r["salt"]) == want else "mismatch")
    return out


def standing(commits: dict[int, str], checked: dict[int, str]) -> str:
    """One player's reveal status on one game: `none` with no commits; `mismatch` when a reveal does not match its
    commit; `missing` when some committed step has no matching reveal; else `revealed`."""
    if not commits:
        return NONE
    if any(v == "mismatch" for v in checked.values()):
        return MISMATCH
    if sum(1 for v in checked.values() if v == "ok") < len(commits):
        return MISSING
    return REVEALED


def most_revealed(models_by_step: dict[int, str]) -> str:
    counts: dict[str, int] = {}
    for m in models_by_step.values():
        counts[m] = counts.get(m, 0) + 1
    return max(sorted(counts), key=lambda m: counts[m]) if counts else ""


class LocalSalts:
    """The client's own record of what it committed: `{relay|session: [{step, model, salt}]}` in a JSON file
    (0600) beside its keys. Without it a player cannot reveal; a lost file is a missing reveal, nothing worse."""

    def __init__(self, path: Path):
        self.path = Path(path)

    def _load(self) -> dict[str, list[dict[str, Any]]]:
        try:
            data = json.loads(self.path.read_text())
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def remember(self, key: str, step: int, model: str, salt: str) -> None:
        data = self._load()
        rows = [r for r in data.get(key, []) if r.get("step") != step]
        rows.append({"step": int(step), "model": model, "salt": salt})
        data[key] = sorted(rows, key=lambda r: r["step"])
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(f".{self.path.name}.{os.getpid()}.tmp")
        fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(json.dumps(data))
        os.replace(tmp, self.path)

    def recall(self, key: str) -> list[dict[str, Any]]:
        return list(self._load().get(key, []))

    def forget(self, key: str) -> None:
        data = self._load()
        if data.pop(key, None) is not None:
            tmp = self.path.with_name(f".{self.path.name}.{os.getpid()}.tmp")
            tmp.write_text(json.dumps(data))
            os.replace(tmp, self.path)
