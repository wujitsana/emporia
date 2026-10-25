"""Keys read from where the operator keeps them (P162, P164).

A key setting may hold a *reference* instead of a key file:

    env:NAME                             an environment variable of the agent's process
    cmd:op read op://Vault/Item/field    1Password CLI (OP_SERVICE_ACCOUNT_TOKEN, unattended)
    cmd:op document get "emporia identity" --vault Agents
    cmd:bw get password ITEM             Bitwarden CLI (BW_SESSION)

The secret is read when first needed, kept in memory for the life of the
process, and never written to disk. Errors name the source, never the value.
Hermes's own credential vault is not used: its password-manager unlocks are
per chat session and stay locked unattended, by design, and an agent signs
unattended.
"""

from __future__ import annotations

import base64
import binascii
import os
import shlex
import subprocess


class KeySourceError(RuntimeError):
    """A key source could not be read; the message is safe to show."""


def read_bytes(ref: str, env: dict[str, str] | None = None) -> bytes:
    """What the reference points at, as the bytes the command printed. `env`
    adds variables (a profile's .env, for a process that is not the agent)."""
    kind, _, rest = ref.strip().partition(":")
    if kind == "env":
        value = os.environ.get(rest.strip(), "") or (env or {}).get(rest.strip(), "")
        if not value.strip():
            raise KeySourceError(f"the environment variable {rest.strip()} is not set for this agent")
        return value.encode()
    if kind == "cmd":
        argv = shlex.split(rest)
        if not argv:
            raise KeySourceError("cmd: needs a command, e.g. cmd:op read op://Vault/Item/key")
        try:
            r = subprocess.run(argv, capture_output=True, timeout=60,
                               env={**os.environ, **(env or {})} if env else None)
        except OSError as e:          # missing, or a PATH entry it may not run
            raise KeySourceError(f"{argv[0]} is not installed or not on PATH") from e
        except subprocess.TimeoutExpired as e:
            raise KeySourceError(f"{argv[0]} did not answer within 60s (locked or waiting for sign-in?)") from e
        if r.returncode != 0 or not r.stdout.strip():
            err = r.stderr.decode(errors="replace").strip()[-200:]
            raise KeySourceError(f"{argv[0]} failed (exit {r.returncode}): {err}")
        return r.stdout
    raise KeySourceError("a key source is env:NAME or cmd:COMMAND")


def read_text(ref: str) -> str:
    return read_bytes(ref).decode(errors="strict")


def key32(ref: str, env: dict[str, str] | None = None) -> bytes:
    """A 32-byte key from a source: the raw bytes (a key file stored as a
    document), 64 hex characters, or base64."""
    data = read_bytes(ref, env)
    if len(data) == 32:
        return data
    text = data.decode(errors="replace").strip()
    try:
        raw = bytes.fromhex(text.removeprefix("0x"))
        if len(raw) == 32:
            return raw
    except ValueError:
        pass
    try:
        raw = base64.b64decode(text, validate=True)
        if len(raw) == 32:
            return raw
    except (binascii.Error, ValueError):
        pass
    raise KeySourceError("the source did not give a 32-byte key (raw, 64 hex characters, or base64)")
