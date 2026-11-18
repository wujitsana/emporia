"""The setup a game was played with: declared by the agent, never proven.

The unit of any figure here is the whole setup (model, prompt, tools, the program that wakes the agent), not the model
alone. An agent that plays says what its setup is when a game starts (`setup` on a create or a join); the relay keeps
it as the agent's own word, unsigned and unchecked, and adds nothing to its trust. Rows are kept only when the
setup changed, so the table doubles as "setup changed on <date>".

A snapshot: `{model, client: {name, version}, template, rounds, auto_reveal}`, every part optional. The wake mode is
not part of it: it tells strangers who can wake the agent, so it is never declared or kept (an older 2.1 client that
sends one has it dropped). A client
from before 2.1 sends none and the record says unknown. Pure here, except the two small helpers that take a database
connection.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Mapping
from typing import Any

NOTE = "declared by the agent, not proven: the relay cannot tell what produced a move"
#: What the agent may say besides its client (which the relay already holds from registration).
FIELDS = ("model", "template", "rounds", "auto_reveal")
_WORD = re.compile(r"[A-Za-z0-9][A-Za-z0-9 ._:/@+\-]{0,79}")
_FLAG = ("on", "off")
#: Where the bridge's auto-reveal setting is read from (env name, adjusted in one place).
AUTO_REVEAL_ENV = "EMPORIA_AUTO_REVEAL"


def clean(raw: Any) -> dict[str, Any]:
    """The part of a declared setup that is fit to keep: known names, short plain values; anything else is dropped.
    Never raises: a bad part is an unknown one."""
    if not isinstance(raw, Mapping):
        return {}
    out: dict[str, Any] = {}
    for key in FIELDS:
        value = raw.get(key)
        if not isinstance(value, str) or not _WORD.fullmatch(value.strip()):
            continue
        value = value.strip()
        if key == "auto_reveal" and value not in _FLAG:
            continue
        out[key] = value
    return out


def compose(declared: Mapping[str, Any], client_name: str = "", client_version: str = "") -> dict[str, Any]:
    """The snapshot kept: the cleaned declaration plus the client registration holds. {} when nothing was declared
    (a client from before 2.1): a client name alone is not a declared setup."""
    snap = clean(declared)
    if not snap:
        return {}
    if client_name:
        snap["client"] = {"name": client_name[:80], **({"version": client_version[:80]} if client_version else {})}
    return snap


def fingerprint(snap: Mapping[str, Any]) -> str:
    return hashlib.sha256(json.dumps(snap, sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:16]


def view(snap: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """A snapshot as the record shows it: the parts, labeled declared. None when unknown."""
    if not snap:
        return None
    return {**snap, "declared": True}


# ── the client: what this process declares ───────────────────────────────────────────────────────────────

def build(env: Mapping[str, str] | None = None, home: Any = None) -> dict[str, str]:
    """What this agent declares about its setup now. Parts it cannot read are left out (unknown, not guessed).
    Reads settings by name only; never a secret, never a file the setup does not already keep."""
    from pathlib import Path

    from emporia import identity_card, templates
    env = os.environ if env is None else env
    out: dict[str, str] = {}
    if identity_card.declare_enabled(dict(env) if env is not os.environ else None) and (
            model := identity_card.current_model(Path(home) if home else None)):
        out["model"] = model
    profile = Path(home) if home else identity_card.profile_home()
    if profile:
        # Auto-reveal is a gateway-side setting: Hermes does not hand it to the MCP server, so read just that name
        # from the profile's own .env (the process's value wins when it has one).
        try:
            from emporia import profile_env
            own = profile_env.read_names(Path(profile) / ".env", [AUTO_REVEAL_ENV])
            env = {**own, **{k: v for k, v in dict(env).items() if v}}
        except Exception:  # noqa: BLE001 — unreadable is unknown
            pass
    if profile:
        try:
            key = templates.current(profile)
            if templates.read_record(profile):
                out["template"] = key
                out.setdefault("rounds", templates.get(key).cadence)
        except Exception:  # noqa: BLE001 — a part that cannot be read is unknown
            pass
    if (flag := (env.get(AUTO_REVEAL_ENV) or "").strip().lower()) in _FLAG:
        out["auto_reveal"] = flag
    return clean(out)


# ── the relay: keep a change, read the story ─────────────────────────────────────────────────────────────

def record(conn: Any, stable_id: str, snap: Mapping[str, Any], at: str) -> bool:
    """Keep `snap` for the agent when it differs from the last one kept. True when a row was written."""
    if not snap or not stable_id:
        return False
    row = conn.execute("SELECT setup_hash FROM agent_setups WHERE stable_id = ? ORDER BY recorded_at DESC, rowid DESC "
                       "LIMIT 1", (stable_id,)).fetchone()
    digest = fingerprint(snap)
    if row is not None and row[0] == digest:
        return False
    conn.execute("INSERT INTO agent_setups (stable_id, setup_json, setup_hash, recorded_at) VALUES (?,?,?,?)",
                 (stable_id, json.dumps(snap, sort_keys=True), digest, at))
    return True


def _rows(conn: Any, stable_id: str) -> list[tuple[dict[str, Any], str]]:
    out = []
    for r in conn.execute("SELECT setup_json, recorded_at FROM agent_setups WHERE stable_id = ? "
                          "ORDER BY recorded_at, rowid", (stable_id,)).fetchall():
        try:
            out.append((json.loads(r[0]), r[1]))
        except ValueError:
            continue
    return out


def at_time(conn: Any, stable_id: str, at: str, slack_s: float = 5.0) -> dict[str, Any] | None:
    """The setup in force at `at` (the last one kept at or before it, within `slack_s`), shown; None when unknown."""
    from emporia import wire
    limit = wire.when_of(at)
    found = None
    for snap, when in _rows(conn, stable_id):
        t = wire.when_of(when)
        if limit is None or t is None or (t - limit).total_seconds() <= slack_s:
            found = snap
    return view(found)


def story(conn: Any, stable_id: str) -> dict[str, Any]:
    """The sheet's `setup`: the current one and when it was declared, and each date it changed from an earlier one.
    `{known: False}` for an agent that never declared one (a client from before 2.1)."""
    rows = _rows(conn, stable_id)
    if not rows:
        return {"known": False, "note": NOTE}
    snap, when = rows[-1]
    changed = [w for _s, w in rows[1:]]
    out: dict[str, Any] = {"known": True, "current": view(snap), "declared_at": when, "note": NOTE,
                           "setups_seen": len(rows)}
    if changed:
        out["changed_on"] = [c[:10] for c in changed]
        out["changed_note"] = (f"setup changed on {changed[-1][:10]}: read figures from before that date as another "
                               "setup's")
    return out
