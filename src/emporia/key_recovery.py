"""A recovery key for an agent whose identity key is lost or stolen (P246):
`emporia agent recovery-key` and `emporia agent recover`.

The recovery key is made while the identity key is still good: only its hash is
published (a commitment in the agent's key log, signed by the identity key), and
the key itself is written as one line to a file the person names, to print on
paper or keep in a password manager. It is never printed to the terminal.

Using it needs nothing from the lost key: the recovery key signs a step that
gives the agent a new identity key and says since when the old one cannot be
trusted. The new key is made and written to disk before the relay hears of it,
as for a rotation (`key_rotation`), and installed the same way.
"""

from __future__ import annotations

import base64
import binascii
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives.asymmetric import ed25519

from emporia import identity, wire
from emporia.key_rotation import RotationError, _public_hex, _session, install_identity_key, load_agent_key

#: What a recovery key looks like on paper or in a password manager.
RECOVERY_KEY_PREFIX = "emporia-recovery-v1:"


def encode_recovery_key(priv: ed25519.Ed25519PrivateKey) -> str:
    return RECOVERY_KEY_PREFIX + base64.urlsafe_b64encode(priv.private_bytes_raw()).rstrip(b"=").decode()


def decode_recovery_key(text: str) -> ed25519.Ed25519PrivateKey:
    """The key a recovery-key line holds. Says what is wrong, never what the text was."""
    line = text.strip()
    if not line.startswith(RECOVERY_KEY_PREFIX):
        raise RotationError(f"that is not a recovery key (it starts {RECOVERY_KEY_PREFIX!r})")
    body = line[len(RECOVERY_KEY_PREFIX):]
    try:
        return ed25519.Ed25519PrivateKey.from_private_bytes(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
    except (binascii.Error, ValueError) as e:
        raise RotationError("that recovery key is damaged: it should be one line, copied whole") from e


def _detail(r: Any) -> str:
    return r.json().get("detail") if "json" in r.headers.get("content-type", "") else r.text


def make_recovery_key(
    agent_id: str, key_dir: Path, http: Any, out: Path, *, source: str = "", source_env: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Make a recovery key for `agent_id`: write it to `out` (0600, never
    overwriting a file), then publish its commitment, signed by the identity key.
    If the relay refuses, `out` is removed again. Returns `commitment` and `out`."""
    if out.exists():
        raise RotationError(f"{out} is already there; name a file that does not exist yet")
    if not out.parent.is_dir():
        raise RotationError(f"the folder {out.parent} does not exist")
    priv = load_agent_key(agent_id, key_dir, source, source_env)
    headers = _session(http, agent_id, priv)
    me = http.get(f"/agents/{agent_id}", headers=headers)
    if me.status_code != 200:
        raise RotationError(f"the relay would not show {agent_id} ({me.status_code}) — is it registered there?")
    profile = me.json()
    if wire.live_commitment(profile.get("key_log") or []):
        raise RotationError(f"{agent_id} already has a recovery key. Using it (emporia agent recover) spends it, "
                            "and then you can make another.")
    recovery = ed25519.Ed25519PrivateKey.generate()
    commitment = wire.recovery_commitment_for(_public_hex(recovery))
    # On disk before the relay hears of it: from the next line on this is the only copy.
    identity._write_secret_atomically(out, (encode_recovery_key(recovery) + "\n").encode())
    at = datetime.now(UTC).isoformat()
    r = http.post(f"/agents/{agent_id}/recovery-commitment", headers=headers, json={
        "commitment": commitment, "at": at, "signature": identity.sign_payload(
            priv, wire.signed_recovery_commitment_payload(profile["stable_id"], commitment, at))})
    if r.status_code != 200:
        if 400 <= r.status_code < 500:          # refused: nothing was committed to
            out.unlink(missing_ok=True)
        raise RotationError(f"the relay refused the recovery key ({r.status_code}): {_detail(r)}")
    return {"commitment": commitment, "out": out}


def recover_identity_key(
    agent_id: str, key_dir: Path, http: Any, recovery: ed25519.Ed25519PrivateKey, *,
    compromised_since: str = "", source: str = "",
) -> dict[str, Any]:
    """Give `agent_id` a new identity key, authorised by its recovery key.

    `compromised_since` (an ISO time, optional): when the old key stopped being
    only the agent's. The old key is trusted for what it signed before then and
    not after; key changes made from then on are undone. Without it, the old
    key's time ends now. `source`: where the identity key is kept when not in
    `key_dir`; the new key is then left in a file and `installed` is False.
    Returns what `key_rotation.rotate_identity_key` does, and `superseded`."""
    stem = identity._keyfile_stem(agent_id)
    new_file = key_dir / f"{stem}.priv.new"
    r = http.get(f"/agents/{agent_id}/key-log")
    if r.status_code != 200:
        raise RotationError(f"the relay would not show {agent_id}'s key log ({r.status_code}) — is it registered there?")
    served = r.json()
    stable_id, log, on_relay = served["stable_id"], served["key_log"], served["public_key_hex"]
    windows = wire.key_log_windows(stable_id, log, identity.verify, on_relay)
    if not windows or windows[-1]["key"] != on_relay:
        raise RotationError("the key log the relay gave for this agent does not lead from its stable id to its key; "
                            "nothing was changed")
    recovery_pub = _public_hex(recovery)
    live = wire.live_commitment(log)

    # An earlier run that got as far as the relay and stopped: the key in the .new file is the agent's.
    if new_file.exists():
        try:
            held = ed25519.Ed25519PrivateKey.from_private_bytes(new_file.read_bytes())
        except ValueError:
            held = None
        if held is not None and _public_hex(held) == on_relay and any(
                s.get("by") == "recovery" and s.get("recovery_public_key") == recovery_pub for s in log):
            return _finish(agent_id, key_dir, new_file, source, old=_recovered_from(log), new=on_relay, log=log,
                           superseded=served.get("superseded") or [])
    if not live:
        raise RotationError(f"{agent_id} has no recovery key on this relay, so there is nothing to recover with")
    if wire.recovery_commitment_for(recovery_pub) != live:
        raise RotationError(f"that is not the recovery key {agent_id} committed to")

    at = datetime.now(UTC)
    since = compromised_since or at.isoformat()
    since_when = wire.when_of(since)
    if since_when is None or since_when > at:
        raise RotationError("--compromised-since must be a time (like 2026-10-28T09:30:00Z), not in the future")
    old = wire.key_before(windows, since)
    if not old:
        raise RotationError(f"{agent_id} had no key at {since}")
    new_priv = ed25519.Ed25519PrivateKey.generate()
    new_pub = _public_hex(new_priv)
    # On disk before the relay hears of it: this key is the agent from the next line on.
    key_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    identity._write_secret_atomically(new_file, new_priv.private_bytes_raw())
    stamp = at.isoformat()
    body = {"old_public_key_hex": old, "new_public_key_hex": new_pub, "recovery_public_key_hex": recovery_pub,
            "compromised_since": since, "at": stamp, "signature": identity.sign_payload(
                recovery, wire.signed_key_recovery_payload(stable_id, old, new_pub, recovery_pub, since, stamp))}
    r = http.post(f"/agents/{agent_id}/key-recovery", json=body)
    if r.status_code != 200:
        if 400 <= r.status_code < 500:          # refused: the new key was never taken
            new_file.unlink(missing_ok=True)
        raise RotationError(f"the relay refused the recovery ({r.status_code}): {_detail(r)}")
    done = r.json()
    return _finish(agent_id, key_dir, new_file, source, old=old, new=new_pub, log=done["key_log"],
                   superseded=done.get("superseded") or [])


def _recovered_from(log: list[dict[str, Any]]) -> str:
    return next((s["old"] for s in reversed(log) if s.get("by") == "recovery"), "")


def _finish(agent_id: str, key_dir: Path, new_file: Path, source: str, *, old: str, new: str,
            log: list[dict[str, Any]], superseded: list[dict[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {"old_public_key": old, "new_public_key": new, "new_key_file": new_file,
                           "installed": False, "kept_old_key_file": None, "key_log": log,
                           "superseded": superseded}
    if not source:
        out.update(install_identity_key(agent_id, key_dir, new_file, kept="recovered"))
    return out
