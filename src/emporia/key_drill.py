"""The recovery drill: prove a key backup brings the same agent back, before it is needed.

`emporia keys drill NAME --from BACKUP` opens the agent's backup (the sealed file `emporia signer backup` makes)
in a fresh throwaway folder, loads the keys it holds, signs a local test message and verifies it, and checks that
the public keys are the agent's own: the identity key (so the same agent id) and the encryption key (so the same
fingerprint). Nothing is sent anywhere and nothing is registered. The folder is deleted at the end, whatever
happened. The receipt shows only public values (fingerprints), never key material.

The last pass is recorded in the agent's plugin data (`plugin-data/emporia/key_drill.json`); `doctor` notes when
none has passed in `DUE_DAYS`. Pure steps over bytes and paths; the command (cli.cmd_keys) asks for the passphrase.
"""

from __future__ import annotations

import json
import os
import secrets
import shutil
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric import ed25519, x25519

from emporia import identity, signer

DUE_DAYS = 90
RECORD = "key_drill.json"


class DrillError(ValueError):
    """Why the drill failed, in a sentence that never holds key material."""


def record_path(profile: Path) -> Path:
    return Path(profile) / "plugin-data" / "emporia" / RECORD


def last_pass(profile: Path) -> datetime | None:
    try:
        return datetime.fromisoformat(json.loads(record_path(profile).read_text(encoding="utf-8"))["last_pass"])
    except (OSError, ValueError, KeyError, TypeError):
        return None


def record_pass(profile: Path, agent_id: str, now: datetime | None = None) -> None:
    path = record_path(profile)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"last_pass": (now or datetime.now(UTC)).isoformat(), "agent": agent_id}) + "\n",
                    encoding="utf-8")


def due(profile: Path, now: datetime | None = None) -> tuple[bool, str]:
    """(is a drill due, one line for doctor)."""
    at = last_pass(profile)
    now = now or datetime.now(UTC)
    if at is None:
        return True, f"no recovery drill has passed yet (a drill is due every {DUE_DAYS} days)"
    age = (now - at).days
    if now - at > timedelta(days=DUE_DAYS):
        return True, f"no recovery drill has passed in {DUE_DAYS} days (the last was {age} days ago)"
    return False, f"recovery drill passed {age} day{'s' if age != 1 else ''} ago"


def _public(role: str, raw: bytes) -> str:
    if role == "identity":
        return ed25519.Ed25519PrivateKey.from_private_bytes(raw).public_key().public_bytes_raw().hex()
    return x25519.X25519PrivateKey.from_private_bytes(raw).public_key().public_bytes_raw().hex()


def drill(names: list[str], backup: bytes, passphrase: str, expected: dict[str, str], workroot: Path) -> dict[str, Any]:
    """Restore `backup` into a new folder under `workroot`, check it, delete the folder. `names` are the ids the
    backup may be filed under (the signer's name for the agent, its agent id). `expected` maps `identity` (needed)
    and `encryption` (checked when the backup holds one) to the public key hex the agent has now. Returns the
    receipt; raises DrillError. The folder is gone in every case."""
    body, last = None, ""
    for n in dict.fromkeys(x for x in names if x):
        try:
            body = signer.open_backup(n, backup, passphrase)
            break
        except ValueError as e:
            last = str(e)
    if body is None:
        raise DrillError(last or "that is not a backup for this agent")
    keys = body.get("keys") or {}
    if "identity" not in keys:
        raise DrillError("that backup holds no identity key (a key a back end holds is backed up there)")
    workroot.mkdir(parents=True, exist_ok=True)
    folder = Path(tempfile.mkdtemp(prefix="drill-", dir=workroot))
    os.chmod(folder, 0o700)
    try:
        receipt: dict[str, Any] = {"agent": body.get("agent"), "made": body.get("made", "")}
        for role in ("identity", "encryption"):
            if role not in keys:
                continue
            raw = bytes.fromhex(keys[role])
            identity._write_secret_atomically(folder / f"{role}.key", raw)       # the restore, in the throwaway folder
            got = _public(role, (folder / f"{role}.key").read_bytes())
            want = (expected.get(role) or "").strip().lower()
            if not want:
                if role == "identity":
                    raise DrillError("there is no public identity key to compare with (no key file, signer marker or "
                                     "registered key), so nothing was proved")
                continue
            if got != want:
                raise DrillError(f"the {role} key in that backup is not this agent's: its public key differs")
            receipt[f"{role}_fingerprint"] = identity.encryption_key_fingerprint(got)
            if role == "identity":
                priv = ed25519.Ed25519PrivateKey.from_private_bytes((folder / "identity.key").read_bytes())
                message = b"emporia recovery drill " + secrets.token_bytes(16)       # local only: never sent
                signature = priv.sign(message)
                try:
                    ed25519.Ed25519PublicKey.from_public_bytes(bytes.fromhex(want)).verify(signature, message)
                except InvalidSignature as e:
                    raise DrillError("a test message signed with the restored key did not verify") from e
                receipt["signed_and_verified"] = True
        receipt["checked"] = [r for r in ("identity", "encryption") if f"{r}_fingerprint" in receipt]
        return receipt
    finally:
        shutil.rmtree(folder, ignore_errors=True)
        if folder.exists():      # pragma: no cover - a folder that would not go is said loudly
            raise DrillError(f"the throwaway folder {folder} could not be deleted: remove it yourself")


def now_stamp() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")
