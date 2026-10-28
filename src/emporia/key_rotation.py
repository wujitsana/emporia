"""An agent changes its identity key (P245): `emporia agent rotate-key`.

The new key is made and written to disk before the relay hears of it, so a
failure after that point never loses it. The relay is then given one step of
the agent's key log, signed by the old key. Only then is the new key put where
the agent reads its key from — and the old key file is kept beside it, never
deleted: the person may remove it once they are sure.

A key kept somewhere else (a password manager, an environment variable, the
profile's .env) is not touched: the new key is left in a 0600 file for the
person to store there.
"""

from __future__ import annotations

import os
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives.asymmetric import ed25519

from emporia import identity, wire
from emporia.console.agents import LocalAgent, load_identity_key


class RotationError(Exception):
    """Said to the person as it is; nothing is half-done unless the message says so."""


def _public_hex(priv: ed25519.Ed25519PrivateKey) -> str:
    return priv.public_key().public_bytes_raw().hex()


def _session(http: Any, agent_id: str, priv: ed25519.Ed25519PrivateKey) -> dict[str, str]:
    """Sign in to the relay as the agent with `priv` — the same challenge the
    agent's own tools answer — and return the header that carries the token."""
    ch = http.post("/dashboard/challenge").json()
    signature = priv.sign(identity.challenge_bytes(ch["nonce"])).hex()
    r = http.post("/dashboard/session", json=wire.dashboard_session_body(agent_id, ch["challenge_id"], signature))
    if r.status_code != 200:
        raise RotationError(f"the relay did not sign {agent_id} in with that key ({r.status_code})")
    return {"Authorization": f"Bearer {r.json()['token']}"}


def load_agent_key(agent_id: str, key_dir: Path, source: str = "",
                   source_env: dict[str, str] | None = None) -> ed25519.Ed25519PrivateKey:
    """The agent's identity key as it is kept now: in `source` (a password
    manager, an environment variable) or in its key file in `key_dir`."""
    try:
        if source:
            from emporia.key_source import key32
            return ed25519.Ed25519PrivateKey.from_private_bytes(key32(source, source_env))
        return load_identity_key(LocalAgent(agent_id, key_dir, identity_source=source))
    except (OSError, ValueError) as e:
        raise RotationError(f"could not read {agent_id}'s identity key: {e}") from e


def install_identity_key(agent_id: str, key_dir: Path, new_file: Path, *, kept: str = "rotated") -> dict[str, Any]:
    """Put the new key (in `new_file`, which the relay has taken) where the agent
    reads its key from, the old one beside it as `<name>.<kept>-<time>`. Returns
    `installed` (True), `new_key_file` and `kept_old_key_file` (None if the old key
    was not a file here). Used after a rotation and after a recovery."""
    live = LocalAgent(agent_id, key_dir).identity_key
    out: dict[str, Any] = {"installed": True, "new_key_file": live, "kept_old_key_file": None}
    if live.exists():
        stamped = live.with_name(f"{live.name}.{kept}-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}")
        backup, n = stamped, 0
        while backup.exists():          # two changes in one second
            n += 1
            backup = stamped.with_name(f"{stamped.name}-{n}")
        identity._write_secret_atomically(backup, live.read_bytes())
        out["kept_old_key_file"] = backup
    os.replace(new_file, live)
    return out


def rotate_identity_key(
    agent_id: str, key_dir: Path, http: Any, *, source: str = "", source_env: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Change `agent_id`'s identity key on the relay `http` reaches (an
    `httpx.Client` with the relay as its base URL).

    `source`: where the key is kept when not in `key_dir` (an
    EMPORIA_IDENTITY_KEY_SOURCE reference); the new key is then left in a file
    and `installed` is False. Returns `old_public_key`, `new_public_key`,
    `new_key_file`, `installed`, `kept_old_key_file` (None if the old key was
    not a file here) and `key_log` as the relay now holds it."""
    stem = identity._keyfile_stem(agent_id)
    old_priv = load_agent_key(agent_id, key_dir, source, source_env)
    old_pub = _public_hex(old_priv)
    new_file = key_dir / f"{stem}.priv.new"

    # The key here is the agent's unless an earlier run got as far as the relay and
    # stopped: then the key waiting in the .new file is.
    held = [old_priv]
    if new_file.exists():
        try:
            held.append(ed25519.Ed25519PrivateKey.from_private_bytes(new_file.read_bytes()))
        except ValueError as e:
            raise RotationError(f"{new_file} is not a key; move it away and run this again") from e
    headers, first_error = None, None
    for priv in held:
        try:
            headers = _session(http, agent_id, priv)
            break
        except RotationError as e:
            first_error = first_error or e
    if headers is None:
        raise first_error  # type: ignore[misc]
    me = http.get(f"/agents/{agent_id}", headers=headers)
    if me.status_code != 200:
        raise RotationError(f"the relay would not show {agent_id} ({me.status_code}) — is it registered there?")
    profile = me.json()
    on_relay = profile["public_key_hex"]

    # The key that run chose is the relay's now. Finish with it.
    if new_file.exists() and on_relay != old_pub:
        new_priv = held[1]
        if _public_hex(new_priv) != on_relay:
            raise RotationError(f"the relay has a key for {agent_id} that is neither the one here nor the one in "
                                f"{new_file}; nothing was changed")
        key_log = profile.get("key_log") or []
    else:
        if on_relay != old_pub:
            raise RotationError(f"the relay has a different key for {agent_id} ({on_relay[:12]}…) than the one "
                                f"here ({old_pub[:12]}…); nothing was changed")
        new_priv = ed25519.Ed25519PrivateKey.generate()
        new_pub = _public_hex(new_priv)
        # On disk before the relay hears of it: this key is the agent from the next line on.
        key_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        identity._write_secret_atomically(new_file, new_priv.private_bytes_raw())
        at = datetime.now(UTC).isoformat()
        body = {"new_public_key_hex": new_pub, "at": at, "signature": identity.sign_payload(
            old_priv, wire.signed_key_rotation_payload(profile["stable_id"], old_pub, new_pub, at))}
        r = http.post(f"/agents/{agent_id}/key-rotations", json=body, headers=headers)
        if r.status_code != 200:
            if 400 <= r.status_code < 500:      # refused: the new key was never taken
                new_file.unlink(missing_ok=True)
            detail = r.json().get("detail") if "json" in r.headers.get("content-type", "") else r.text
            raise RotationError(f"the relay refused the key change ({r.status_code}): {detail}")
        key_log = r.json()["key_log"]

    out: dict[str, Any] = {"old_public_key": old_pub, "new_public_key": _public_hex(new_priv),
                           "new_key_file": new_file, "installed": False, "kept_old_key_file": None,
                           "key_log": key_log}
    if source:
        return out
    out.update(install_identity_key(agent_id, key_dir, new_file))
    return out
