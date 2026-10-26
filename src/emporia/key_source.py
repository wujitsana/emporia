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
        # P173: reading for a given profile (`env`), only that profile's own
        # variables count. Every agent keeping keys in its .env uses the same
        # names, so the caller's environment would hand back another agent's key.
        name = rest.strip()
        value = env.get(name, "") if env is not None else os.environ.get(name, "")
        if not value.strip():
            raise KeySourceError(f"the environment variable {name} is not set for this agent")
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


def read_text(ref: str, env: dict[str, str] | None = None) -> str:
    return read_bytes(ref, env).decode(errors="strict")


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


# P165: keys kept in the agent profile's .env. Hermes treats that file as
# secret: its file tools refuse to read any .env, its terminal and code tools
# start children without variables whose names hold KEY/TOKEN/SECRET, and it
# masks secret-looking assignments in terminal output. That is defence in
# depth around the model, not a boundary (a deliberately disguised read still
# gets through) — better than a key file, which has none of it. The names all
# contain KEY or SECRET so those rules apply.
ENV_KEY_NAMES = {
    "identity": "EMPORIA_IDENTITY_PRIVATE_KEY",
    "encryption": "EMPORIA_ENCRYPTION_PRIVATE_KEY",
    "evm": "EMPORIA_X402_EVM_PRIVATE_KEY",
    "svm": "EMPORIA_X402_SOLANA_PRIVATE_KEY",
    "phrase": "EMPORIA_X402_PHRASE_SECRET",
}
SOURCE_VARS = {
    "identity": "EMPORIA_IDENTITY_KEY_SOURCE",
    "encryption": "EMPORIA_ENCRYPTION_KEY_SOURCE",
    "evm": "EMPORIA_X402_EVM_KEY_SOURCE",
    "svm": "EMPORIA_X402_SOLANA_KEY_SOURCE",
    "phrase": "EMPORIA_X402_PHRASE_SOURCE",
}
STORAGE_VAR = "EMPORIA_KEY_STORAGE"     # "env": new keys go to the profile .env, not files


def store_in_env() -> bool:
    return os.environ.get("EMPORIA_KEY_STORAGE", "").strip().lower() == "env" and bool(dotenv_path())


def dotenv_path():
    from pathlib import Path
    p = os.environ.get("EMPORIA_DOTENV", "").strip()
    return Path(p) if p else None


def set_env_lines(path, updates: dict[str, str]) -> None:
    """Set `updates` in a .env, keeping every other line; 0600; no backup
    beside it (a `.env.bak` is not a name Hermes protects)."""
    lines = path.read_text().splitlines() if path.exists() else []
    seen = set()
    for i, line in enumerate(lines):
        k = line.split("=", 1)[0].strip() if "=" in line and not line.lstrip().startswith("#") else ""
        if k in updates:
            lines[i] = f"{k}={updates[k]}"
            seen.add(k)
    lines += [f"{k}={v}" for k, v in updates.items() if k not in seen]
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write("\n".join(lines) + "\n")
    os.chmod(path, 0o600)


def save_key_to_env(what: str, value: str, path=None) -> None:
    """Keep a new key in the profile .env and point its source at it, here and
    for every later start of the agent."""
    path = path or dotenv_path()
    name, source = ENV_KEY_NAMES[what], SOURCE_VARS[what]
    set_env_lines(path, {name: value, source: f"env:{name}"})
    os.environ[name] = value
    os.environ[source] = f"env:{name}"
