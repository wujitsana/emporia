"""The relay operator's verdict key: a person's key, never an agent's (security review pass 2, P1).

An idea verdict gives points on the season's board, and a verdict is final.
The relay once took a verdict on the operator agent's own signature — something the operator
agent's process holds — so a takeover of that agent could award points. Now every verdict is signed by a
separate Ed25519 key, the operator's **verdict key**:

* **Made by the person**, with `emporia operator NAME verdict-key new` in their own terminal
  (the CLI refuses it from an agent's tools, and the key guard refuses the words), and kept
  in the signer's folder — `signer_dir()/operator/verdict.priv`, 0600 in a 0700 folder — the
  person's side, beside the signer's admin token, never in an agent profile. Where the signer
  runs as its own user (its set-up for real money) no agent can read the folder at all; on a
  one-user testnet set-up the key guard refuses a tool that names it.
* **Registered with the relay** by the person who runs it:
  `EMPORIA_OPERATOR_VERDICT_KEY=<public key>` in the relay's .env, beside `EMPORIA_AGENT_ID`
  (which names the operator), then a restart. The relay takes a verdict only with that key's
  signature, and refuses every verdict, saying how to set one, while it is unset.
* **Rotation and recovery:** `emporia operator NAME verdict-key rotate` makes a new key (the old
  file is kept beside it, renamed `.retired-<UTC stamp>`); the person puts the new public key in
  the relay's .env and restarts it. A lost or stolen key takes the same step: from that restart
  the relay no longer takes the old one. A verdict already given keeps the key that signed it
  (`verdict_key` beside its signature), so anyone can still check it.

The payloads are `wire.signed_idea_verdict_payload`; it is not an agent kind (`signing.KINDS` does not
have it), so no agent's signer or process signs one.
"""

from __future__ import annotations

import os
import re
import stat
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives.asymmetric import ed25519

ENV = "EMPORIA_OPERATOR_VERDICT_KEY"
KEY_FILE = "verdict.priv"
_HEX64 = re.compile(r"^[0-9a-f]{64}$")

UNSET = (f"verdicts need the operator's verdict key, and this relay has none: the person running it makes one "
         f"with `emporia operator NAME verdict-key new` (in their own terminal), sets {ENV}=<its public key> in "
         "the relay's .env, and restarts the relay")


class VerdictKeyError(Exception):
    """Said to the person as it is; nothing was written unless the message says so."""


def folder() -> Path:
    """The person's side of the signer's folder (`EMPORIA_SIGNER_DIR`, or its default)."""
    from emporia.signer import signer_dir
    return signer_dir() / "operator"


def key_path() -> Path:
    return folder() / KEY_FILE


def _private_folder(path: Path, create: bool) -> None:
    """The folder is this user's own, not a link and not open to anyone else."""
    if create:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
        raise VerdictKeyError(f"{path} is not a plain folder (a link?) — refusing to keep a verdict key there")
    if st.st_uid != os.geteuid():
        raise VerdictKeyError(f"{path} belongs to another user — run this as the signer's user")
    if st.st_mode & 0o077:
        raise VerdictKeyError(f"{path} is open to other users (mode {oct(st.st_mode & 0o777)}) — chmod 700 it first")


def _load() -> ed25519.Ed25519PrivateKey | None:
    path = key_path()
    _private_folder(path.parent, create=False)
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return None
    if len(raw) != 32:
        raise VerdictKeyError(f"{path} is not a verdict key (32 bytes) — move it aside and make a new one")
    return ed25519.Ed25519PrivateKey.from_private_bytes(raw)


def _public_of(priv: ed25519.Ed25519PrivateKey) -> str:
    return priv.public_key().public_bytes_raw().hex()


def public_hex() -> str:
    """This person's verdict key, public half; "" when none was made here."""
    priv = _load()
    return _public_of(priv) if priv else ""


def new(rotate: bool = False) -> tuple[str, Path | None]:
    """Make the verdict key: (its public key, where the old one was kept, if rotating).
    Without `rotate`, an existing key is never replaced."""
    from emporia import identity
    path = key_path()
    _private_folder(path.parent, create=True)
    retired = None
    if path.exists():
        if not rotate:
            raise VerdictKeyError(f"a verdict key is already here ({public_hex()[:16]}…, {path}) — "
                                  "`verdict-key rotate` replaces it")
        retired = path.with_name(f"{KEY_FILE}.retired-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}")
        os.replace(path, retired)
    priv = ed25519.Ed25519PrivateKey.generate()
    identity._write_secret_atomically(path, priv.private_bytes_raw())
    try:
        from emporia import signer
        signer.own(path.parent)
    except Exception:  # noqa: BLE001 — only root needs to hand the files back
        pass
    return _public_of(priv), retired


def sign(payload: dict[str, Any]) -> str:
    """This person's signature on a verdict payload that names their verdict key."""
    from emporia import identity
    priv = _load()
    if priv is None:
        raise VerdictKeyError(f"no verdict key here ({key_path()}) — make one with "
                              "`emporia operator NAME verdict-key new` and register it with the relay")
    if payload.get("verdict_key") != _public_of(priv):
        raise VerdictKeyError("the verdict names another verdict key than the one kept here — not signed")
    return identity.sign_payload(priv, payload)


def registered(env: dict[str, str] | None = None) -> tuple[str, str]:
    """The relay's side: (the registered verdict key, "") or ("", why there is none)."""
    raw = str((os.environ if env is None else env).get(ENV, "")).strip().lower()
    if not raw:
        return "", UNSET
    if not _HEX64.match(raw):
        return "", (f"{ENV} on this relay is not an Ed25519 public key (64 hex) — the person running it sets "
                    "the public key `emporia operator NAME verdict-key` shows, and restarts the relay")
    return raw, ""
