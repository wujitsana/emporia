"""The signer: agents' wallet keys in a process of their own (P203).

On testnet an agent signs payments in its own process, from keys in its
profile — nothing to set up. That process is the agent's: with a terminal, its
tools can read the keys and change the limits in its .env. For real money the
keys move here, to a service that runs as another user (or in its own
container) and is the only thing that can sign:

* **Its own keys and policy.** Per agent, in `EMPORIA_SIGNER_DIR/agents/<id>/`:
  the wallet keys, and the rules — networks, per-payment and daily caps, the
  approval threshold, whether mainnet is unlocked. The agent cannot read or
  change them; `emporia signer add` moves an agent's keys and rules here and
  `emporia signer policy` changes the rules, both run by a person.
* **Permission to ask, permission to pay.** An agent proves which agent it is
  with a token (`EMPORIA_SIGNER_TOKEN`); that lets it ask. Paying still has to
  clear the rules, checked here by the same code as in-process
  (`agent_wallet`, under a Scope).
* **Approvals the agent cannot give.** Above the approval threshold a payment
  becomes a pending intent; the operator approves it with `emporia signer
  approve ID` (the admin token, readable only by the signer's user), and the
  agent's retry within an hour is paid, once. An answer typed into the agent's
  own chat could have come from the agent, so it does not count here.

Listens on a Unix socket or on 127.0.0.1 — never another address. The socket
(`EMPORIA_SIGNER_SOCKET`) sits outside the signer's own 0700 folder, so agents
running as another user can reach it — and nothing else of the signer's: the
default, beside the folder, suits one user (testing); for real money give it a
folder both can enter (systemd's RuntimeDirectory, a shared Docker volume).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import threading
import time
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from starlette.concurrency import run_in_threadpool

from emporia import agent_wallet, identity

APPROVAL_TTL_S = 3600
# P206: what the signer needs to reach the chains, signer-wide (its signer.env):
# RPC URLs may carry a provider's key, so the file is the signer's alone.
NETWORK_SETTINGS = ("EMPORIA_X402_RPC_URL", "EMPORIA_X402_SOLANA_RPC_URL", "EMPORIA_TEMPO_RPC_URL",
                    "EMPORIA_TEMPO_NETWORK", "EMPORIA_TEMPO_TOKEN")
# The rules a signer keeps per agent — the agent_wallet settings, nothing else.
POLICY_NAMES = ("EMPORIA_X402_PAY_NETWORKS", "EMPORIA_X402_MAX_PAYMENT_CENTS", "EMPORIA_X402_MAX_DAILY_CENTS",
                "EMPORIA_PAYMENT_APPROVAL_CENTS", "EMPORIA_X402_MAINNET",
                # P293 (phase 2a): what its signatures may say — signing.py's rules read these
                *("EMPORIA_SIGN_LIMITS", "EMPORIA_PAYOUT_ADDRESSES", "EMPORIA_RELAY_URL",
                  "EMPORIA_BACKUP_RELAYS", "EMPORIA_VISIT_RELAYS"),
                # P293 (2b): a cap on openings a minute, off unless set
                "EMPORIA_SIGNER_OPEN_PER_MIN")
SIGNING_POLICY = POLICY_NAMES[5:10]
_LOCK = threading.Lock()
_AUDIT_LOCK = threading.Lock()
AUDIT_MAX_BYTES = 5 * 1024 * 1024


def signer_dir() -> Path:
    """EMPORIA_SIGNER_DIR; else, beside Hermes's profiles (its home is often a
    data root, not a place for loose folders); else ~/.emporia-signer."""
    explicit = os.getenv("EMPORIA_SIGNER_DIR", "").strip()
    if explicit:
        return Path(explicit).expanduser()
    hermes = os.getenv("HERMES_HOME", "").strip()
    if hermes and (Path(hermes) / "profiles").is_dir():
        return Path(hermes) / "profiles" / ".signer"
    return Path("~/.emporia-signer").expanduser()


def agent_dir(agent_id: str) -> Path:
    return signer_dir() / "agents" / identity._keyfile_stem(agent_id)


def own(path: Path) -> None:
    """P207: a file root wrote here (an admin container, sudo) belongs to the
    signer's user, or the signer could no longer read its own folder."""
    if os.getuid() != 0 or not signer_dir().exists():
        return
    owner = signer_dir().stat()
    for p in [path, *(path.rglob("*") if path.is_dir() else [])]:
        os.chown(p, owner.st_uid, owner.st_gid)


def _write_json(path: Path, data) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    identity._write_secret_atomically(path, json.dumps(data, indent=1).encode())
    own(path.parent)


def _read_json(path: Path, default):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return default


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


# ─── agents and their rules ───────────────────────────────────────────────────

def policy(agent_id: str) -> dict:
    return _read_json(agent_dir(agent_id) / "policy.json", {})


def save_policy(agent_id: str, env: dict, token_sha256: str | None = None) -> None:
    current = policy(agent_id)
    _write_json(agent_dir(agent_id) / "policy.json", {
        "env": {k: str(v) for k, v in env.items() if k in POLICY_NAMES and str(v) != ""},
        "token_sha256": token_sha256 or current.get("token_sha256", ""),
    })


def new_agent_token(agent_id: str) -> str:
    token = secrets.token_urlsafe(32)
    save_policy(agent_id, policy(agent_id).get("env", {}), _hash(token))
    return token


def admin_token() -> str:
    """The operator's token, made on first use; only the signer's user reads it."""
    path = signer_dir() / "admin.token"
    if not path.exists():
        signer_dir().mkdir(mode=0o700, parents=True, exist_ok=True)
        identity._write_secret_atomically(path, secrets.token_urlsafe(32).encode())
        own(path)
    return path.read_text().strip()


def _scope(agent_id: str) -> agent_wallet.Scope:
    return agent_wallet.Scope(env=dict(policy(agent_id).get("env", {})), key_dir=agent_dir(agent_id) / "keys",
                              approver=lambda message: _decide(agent_id, message),
                              wallet=Wallet(agent_id))      # P293: its keys by their back ends


# ─── signer-wide settings, and what it signed ────────────────────────────────

def settings_path() -> Path:
    return signer_dir() / "signer.env"


_settings_seen: dict = {"mtime": None}


def load_settings() -> None:
    """signer.env into the environment, again whenever it changes."""
    try:
        mtime = settings_path().stat().st_mtime
    except OSError:
        return
    if _settings_seen["mtime"] == mtime:
        return
    for line in settings_path().read_text().splitlines():
        key, sep, value = line.strip().partition("=")
        if sep and key in NETWORK_SETTINGS:
            os.environ[key] = value.strip().strip('"').strip("'")
    _settings_seen["mtime"] = mtime


def add_settings(values: dict) -> list[str]:
    """Network settings from an agent being added, where signer.env has none. Names added."""
    path = settings_path()
    have = {ln.partition("=")[0] for ln in (path.read_text().splitlines() if path.exists() else [])}
    new = {k: v for k, v in values.items() if k in NETWORK_SETTINGS and v and k not in have}
    if new:
        lines = (path.read_text().splitlines() if path.exists() else []) + [f"{k}={v}" for k, v in new.items()]
        signer_dir().mkdir(mode=0o700, parents=True, exist_ok=True)
        identity._write_secret_atomically(path, ("\n".join(lines) + "\n").encode())
        own(path)
    return sorted(new)


def audit(entry: dict) -> None:
    """One line per request, appended; only the signer writes it."""
    # SI-5: refusals and errors cost the agent nothing and are not rate limited, so they go to their own
    # bounded file; a flood of them can no longer rotate the payment and admin records out of audit.jsonl.
    noisy = entry.get("result") in ("refused", "error", "limited")
    path = signer_dir() / ("audit-refused.jsonl" if noisy else "audit.jsonl")
    with _AUDIT_LOCK:
        try:      # S-8: a bounded file: past AUDIT_MAX_BYTES the old log is kept once (audit.jsonl.1) and a new one starts
            if path.stat().st_size > AUDIT_MAX_BYTES:
                os.replace(path, path.with_name(path.name + ".1"))
        except OSError:
            pass
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a") as f:
            f.write(json.dumps({"at": time.time(), **entry}) + "\n")


# ─── approvals ────────────────────────────────────────────────────────────────

class ApprovalPending(agent_wallet.AgentWalletError):
    def __init__(self, intent_id: str):
        self.intent_id = intent_id
        super().__init__(
            f"this payment needs your operator's approval (intent {intent_id}). Tell them; they approve it on "
            f"the signer's machine with `emporia signer approve {intent_id}`. Then try the same payment again "
            f"within an hour. Not paid yet.")


def _intents_path() -> Path:
    return signer_dir() / "intents.json"


def intents() -> list[dict]:
    now = time.time()
    return [i for i in _read_json(_intents_path(), []) if now - i["at"] < 7 * 24 * 3600]


def _decide(agent_id: str, message: str) -> bool:
    """agent_wallet's approver, here: an approved intent for exactly this payment
    is used once; a denied one refuses; otherwise a pending one is made."""
    with _LOCK:
        items, now = intents(), time.time()
        same = [i for i in items if i["agent"] == agent_id and i["what"] == message and i["status"] != "used"]
        for i in same:
            if i["status"] == "approved" and now - i.get("decided_at", 0) < APPROVAL_TTL_S:
                i["status"], i["used_at"] = "used", now
                _write_json(_intents_path(), items)
                return True
            if i["status"] == "denied" and now - i.get("decided_at", 0) < APPROVAL_TTL_S:
                return False
        pending = next((i for i in same if i["status"] == "pending"), None)
        if pending is None:
            pending = {"id": "int_" + secrets.token_hex(6), "agent": agent_id, "what": message, "at": now,
                       "status": "pending", "terms": agent_wallet.approval_terms()}
            items.append(pending)
            _write_json(_intents_path(), items)
        raise ApprovalPending(pending["id"])


def decide(intent_id: str, approve: bool) -> dict:
    with _LOCK:
        items = intents()
        item = next((i for i in items if i["id"] == intent_id), None)
        if item is None or item["status"] != "pending":
            raise KeyError(intent_id)
        item["status"], item["decided_at"] = ("approved" if approve else "denied"), time.time()
        _write_json(_intents_path(), items)
        return item


# ─── P293 (signer phase 2): back ends — where each agent's keys are kept ─────
# docs/proposals/signer-phase-2.md § 3.1. The rules and limits below are the
# signer's, applied before any back end is asked; a back end only does the key
# work it declares (`signer_backends`). Every role defaults to the local files.

_BACKEND_NAME = r"[a-z0-9][a-z0-9_-]{0,31}"


def _backends_path() -> Path:
    return signer_dir() / "backends.json"


def _backend_secret_path(name: str) -> Path:
    return signer_dir() / "backends" / f"{name}.secret"


def backends() -> dict[str, dict]:
    """{name: {"type", "settings"}} — the back ends its person added (`local` is always there)."""
    return {"local": {"type": "local", "settings": {}}, **_read_json(_backends_path(), {})}


def save_backend(name: str, type_: str, settings: dict, secrets_: dict) -> None:
    import re

    from emporia import signer_backends as sb
    if not re.fullmatch(_BACKEND_NAME, name) or name == "local":
        raise ValueError(f"a back end's name is lowercase letters, digits, - or _ (not 'local'), not {name!r}")
    if type_ not in sb.TYPES or type_ == "local":
        raise ValueError(f"a back end's type is {' or '.join(t for t in sb.TYPES if t != 'local')}")
    unknown = sorted(set(settings) - set(sb.SETTINGS[type_]))
    if unknown:
        raise ValueError(f"{type_} takes {', '.join(sb.SETTINGS[type_])}; not {', '.join(unknown)}")
    with _LOCK:
        items = _read_json(_backends_path(), {})
        items[name] = {"type": type_, "settings": {k: str(v) for k, v in settings.items() if str(v)}}
        _write_json(_backends_path(), items)
        if secrets_:
            _write_json(_backend_secret_path(name), {k: str(v) for k, v in secrets_.items() if str(v)})


def remove_backend(name: str) -> None:
    users = [f"{a} ({r})" for a, rs in all_routes().items() for r, v in rs.items()
             if isinstance(v, dict) and v.get("backend") == name]
    if users:
        raise ValueError(f"{name} still holds keys for {', '.join(users)}; route them elsewhere first")
    with _LOCK:
        items = _read_json(_backends_path(), {})
        items.pop(name, None)
        _write_json(_backends_path(), items)
        _backend_secret_path(name).unlink(missing_ok=True)


def get_backend(name: str = "local"):
    from emporia import signer_backends as sb
    if name in sb.OVERRIDES:
        return sb.OVERRIDES[name]
    item = backends().get(name)
    if item is None:
        raise sb.BackendError(f"no back end named {name!r} in this signer (emporia signer backend)")
    return sb.make_backend(name, item["type"], item.get("settings") or {},
                           _read_json(_backend_secret_path(name), {}) if name != "local" else {})


def routes(agent_id: str) -> dict[str, dict]:
    """{role: {"backend", "ref"}} — where this agent's keys are, when not the local files."""
    return _read_json(agent_dir(agent_id) / "routes.json", {})


def all_routes() -> dict[str, dict]:
    base = signer_dir() / "agents"
    return {p.name: _read_json(p / "routes.json", {}) for p in (base.glob("*") if base.is_dir() else [])
            if (p / "routes.json").exists()}


def set_route(agent_id: str, role: str, backend: str, ref: str = "") -> None:
    from emporia import signer_backends as sb
    if role not in sb.ROLES:
        raise ValueError(f"a role is one of {', '.join(sb.ROLES)}")
    b = get_backend(backend)
    if not b.can_hold(role):
        raise sb.Unsupported(f"the {backend} back end ({b.type}) cannot hold the {role} key: it does "
                             f"{', '.join(sorted(b.capabilities))}")
    with _LOCK:
        items = routes(agent_id)
        if backend == "local":
            items.pop(role, None)
        else:
            items[role] = {"backend": backend, "ref": ref}
        _write_json(agent_dir(agent_id) / "routes.json", items)


def route(agent_id: str, role: str):
    """(back end, ref) for one of the agent's keys."""
    r = routes(agent_id).get(role)
    return (get_backend(r["backend"]), r.get("ref", "")) if r else (get_backend("local"), "")


def holds(agent_id: str, role: str) -> bool:
    if role in routes(agent_id):
        return True
    from emporia import signer_backends as sb
    return sb.LocalBackend("local").holds(agent_id, role)


def public(agent_id: str, role: str) -> str | None:
    if not holds(agent_id, role):
        return None
    b, ref = route(agent_id, role)
    return b.public(agent_id, role, ref)


class _EvmSigner:
    """agent_wallet's EVM signer, in the signer: the key work by its back end."""

    def __init__(self, agent_id: str):
        self.agent_id = agent_id
        self.backend, self.ref = route(agent_id, "evm")
        self.address = self.backend.public(agent_id, "evm", self.ref)

    def sign_typed_data(self, full_message: dict) -> str:
        return "0x" + self.backend.sign_typed_data(self.agent_id, full_message, self.ref).hex()

    def sign_transaction(self, tx: dict) -> bytes:
        return self.backend.sign_transaction(self.agent_id, tx, self.ref)


class _SolanaSigner:
    """agent_wallet's Solana signer, in the signer (solders' pubkey/sign_message shape)."""

    def __init__(self, agent_id: str):
        self.agent_id = agent_id
        self.backend, self.ref = route(agent_id, "svm")
        self.address = self.backend.public(agent_id, "svm", self.ref)

    def pubkey(self):
        from solders.pubkey import Pubkey
        return Pubkey.from_string(self.address)

    def sign_message(self, message: bytes):
        from solders.signature import Signature
        return Signature.from_bytes(self.backend.sign_ed25519(self.agent_id, "svm", bytes(message), self.ref))


class Wallet:
    """What `agent_wallet` asks of the keys inside a Scope: which families, the
    addresses, and signers — every one routed to the agent's back end."""

    def __init__(self, agent_id: str):
        self.agent_id = agent_id

    def families(self) -> tuple[str, ...]:
        return tuple(f for f in ("evm", "svm") if holds(self.agent_id, f))

    def address(self) -> str:
        return public(self.agent_id, "evm") or ""

    def solana_address(self) -> str:
        return public(self.agent_id, "svm") or ""

    def evm(self) -> _EvmSigner:
        if not holds(self.agent_id, "evm"):
            raise agent_wallet.AgentWalletError(f"this signer holds no EVM wallet for {self.agent_id}")
        return _EvmSigner(self.agent_id)

    def svm(self) -> _SolanaSigner:
        if not holds(self.agent_id, "svm"):
            raise agent_wallet.AgentWalletError(f"this signer holds no Solana wallet for {self.agent_id}")
        return _SolanaSigner(self.agent_id)


# ─── P293 (signer phase 2a): the identity key, and signatures by kind ────────

def identity_path(agent_id: str) -> Path:
    return agent_dir(agent_id) / "keys" / "identity.ed25519"


def identity_public(agent_id: str) -> str | None:
    """The agent's identity public key (hex), if the signer holds its key (here or in a back end)."""
    return public(agent_id, "identity")


class _HeldIdentity:
    """The identity key as the signer's back end holds it: `.sign(bytes)` for
    payloads the signer built itself, never bytes an agent handed it."""

    def __init__(self, agent_id: str):
        self.agent_id = agent_id
        self.backend, self.ref = route(agent_id, "identity")

    def sign(self, data: bytes) -> bytes:
        return self.backend.sign_ed25519(self.agent_id, "identity", data, self.ref)


def _identity(agent_id: str) -> _HeldIdentity:
    from emporia import signing
    if not holds(agent_id, "identity"):
        raise signing.Refused(f"this signer does not hold {agent_id}'s identity key — "
                              f"emporia signer add {agent_id} --identity moves it here")
    return _HeldIdentity(agent_id)


def _sign_with(agent_id: str, held: _HeldIdentity, data_fn) -> bytes:
    from emporia import signer_backends as sb
    from emporia import signing
    try:
        return data_fn(held)
    except sb.BackendError as e:
        raise signing.Refused(f"not signed: {e}") from None


class FileHistory:
    """signing.History, kept on disk per agent: a restart must not reset a limit."""

    def __init__(self, agent_id: str):
        self.path = agent_dir(agent_id) / "sign_history.json"

    def take(self, agent_id: str, group: str, limit: tuple[int, float], now: float) -> float:
        count, window = limit
        with _LOCK:
            seen = _read_json(self.path, {})
            times = [t for t in seen.get(group, []) if t > now - window]
            if len(times) >= count:
                return times[0] + window - now
            seen[group] = [*times, now]
            _write_json(self.path, seen)
            return 0.0


def _alert(agent_id: str, event: str, detail: str) -> None:
    try:
        from emporia import signer_alerts
        signer_alerts.notify(agent_id, event, detail)
    except Exception:  # noqa: BLE001 — an alert never stands between an agent and its signature
        pass


def _changes(agent_id: str, kind: str, payload: dict) -> tuple[str, str] | None:
    """(event, detail) when a signature changes who the agent is or where it is paid."""
    if kind == "payout-wallet":
        return "payout-wallet", f"paid at {payload.get('address')} ({payload.get('network_family')}) from now on"
    if kind == "encryption-key":
        path = agent_dir(agent_id) / "last_encryption_binding.json"
        last = _read_json(path, {}).get("key")
        now = payload.get("encryption_public_key_hex")
        _write_json(path, {"key": now})
        if last != now:
            return "encryption-key", "a new encryption key binding — what it is sent is sealed to it"
        return None
    if kind == "relay-list":
        path = agent_dir(agent_id) / "last_relay_list.json"
        last = _read_json(path, {})
        now = {"home": payload.get("home"), "backups": sorted(payload.get("backups") or [])}
        _write_json(path, now)
        if last and last != now:
            return "home-or-backups", f"home {now['home']}, backups {', '.join(now['backups']) or 'none'}"
    return None


def _encryption_binding_rule(agent_id: str, payload: dict) -> bool:
    """P293 (2c): behind a signer holding the encryption keys, a binding names the
    key it holds, or none — a takeover cannot point peers at a key of its own. A
    binding of the key already bound (each registration signs one), or the first
    binding of a key, is no change and counts against no limit. True when it is a
    change (a replacement in the signer counts its own: `replace_encryption_key`)."""
    from emporia import signing
    held = public(agent_id, "encryption")
    named = str(payload.get("encryption_public_key_hex") or "")
    if held and named != held:
        raise signing.Refused("not signed: an encryption key binding behind this signer names the key it holds "
                              f"({held[:12]}…), not {named[:12]}… — a new one is made here "
                              "(emporia agent rotate-encryption-key)")
    last = _read_json(agent_dir(agent_id) / "last_encryption_binding.json", {}).get("key")
    return bool(last) and last != named


def sign_kind(agent_id: str, kind: str, args: list, kwargs: dict) -> str:
    """Build the payload from the kind and arguments, apply its rule with this
    signer's history and the person's policy, sign with the key held here."""
    from emporia import signing
    held = _identity(agent_id)
    p = signing.payload(kind, *args, **kwargs)
    env = dict(policy(agent_id).get("env", {}))
    counted = _encryption_binding_rule(agent_id, p) if kind == "encryption-key" else True
    with agent_wallet.scope(_scope(agent_id)):        # its own wallets, as the signer holds them
        signing.check(kind, p, signing.Context(agent_id, env=env),
                      history=FileHistory(agent_id) if counted else None)
    sig = _sign_with(agent_id, held, lambda h: identity.sign_payload(h, p))
    if change := _changes(agent_id, kind, p):
        _alert(agent_id, *change)
    return sig


# Kinds only a person signs (signing.KINDS' `person`), and the sign-in their
# commands need first. Never reachable with an agent's token: these run in the
# person's own terminal, as the signer's user (they read its folder).
PERSON_KINDS = ("key-rotation", "recovery-commitment", "dashboard-sign-in")


def person_sign(agent_id: str, kind: str, *args, **kwargs) -> str:
    """P293 (2c): a signature its person asked for (`emporia agent rotate-key`,
    `recovery-key`), with the identity key the signer holds. Built by kind, as
    every signature here; logged and, when it changes who the agent is, alerted."""
    from emporia import signing
    if kind not in PERSON_KINDS:
        raise signing.Refused(f"{kind} is not signed for a person here; it is the agent's own (its rules apply)")
    held = _identity(agent_id)
    p = signing.payload(kind, *args, **kwargs)
    if kind == "dashboard-sign-in" and (why := signing.KINDS[kind].check(p, signing.Context(agent_id))):
        raise signing.Refused(f"not signed: {why}")
    sig = _sign_with(agent_id, held, lambda h: identity.sign_payload(h, p))
    audit({"agent": agent_id, "op": "sign", "kind": kind, "result": "signed", "by": "person"})
    if kind != "dashboard-sign-in":
        _alert(agent_id, "identity-change" if kind == "key-rotation" else "recovery-key",
               "its person rotated its identity key" if kind == "key-rotation" else "its person made a recovery key")
    return sig


# ─── P293 (2c): keys made inside the signer ───────────────────────────────────

def make_agent_keys(agent_id: str, where: dict[str, str] | None = None) -> dict[str, str]:
    """New keys for a new agent, made inside the signer (or its back ends), so they
    never exist in the agent's profile. `where`: {role: back end name}, local by
    default; identity and encryption always, evm/svm when named or by default.
    Returns {role: public key or address}. Nothing is replaced."""
    from emporia import signer_backends as sb
    where = {"identity": "local", "encryption": "local", "evm": "local", "svm": "local", **(where or {})}
    for role, name in where.items():
        if role not in sb.ROLES:
            raise ValueError(f"a role is one of {', '.join(sb.ROLES)}")
        if holds(agent_id, role):
            raise sb.BackendError(f"the signer already holds a {role} key for {agent_id}; nothing was made")
        b = get_backend(name)
        if not b.can_hold(role):
            raise sb.Unsupported(f"the {name} back end ({b.type}) cannot hold the {role} key")
        b.require("make-keys", f"make a {role} key")
    agent_dir(agent_id).mkdir(mode=0o700, parents=True, exist_ok=True)
    out = {}
    for role, name in where.items():
        b = get_backend(name)
        ref = b.make(agent_id, role)
        if name != "local":
            set_route(agent_id, role, name, ref)
        out[role] = b.public(agent_id, role, ref)
    own(agent_dir(agent_id))
    audit({"agent": agent_id, "op": "make-keys", "result": "made",
           "roles": {r: where[r] for r in out}})
    return out


def replace_encryption_key(agent_id: str) -> dict:
    """A new encryption key made in the signer, the old one kept (retired) to open
    what was sealed to it; the binding of the new key signed at once. Counted
    against the encryption-key limit (1 a day by default), and alerted."""
    from datetime import UTC, datetime

    from emporia import signing
    b, _ = route(agent_id, "encryption")
    if b.type != "local" or not b.holds(agent_id, "encryption"):
        raise signing.Refused(f"this signer does not hold {agent_id}'s encryption keys")
    limit = signing.limits(policy(agent_id).get("env", {}))["encryption-key"]
    wait = FileHistory(agent_id).take(agent_id, "encryption-key", limit, time.time())
    if wait > 0:
        raise signing.Limited(f"not replaced: an encryption key is replaced at most {limit[0]} times a "
                              f"{'day' if limit[1] == 86400 else f'{int(limit[1])}s'}; again in {int(wait) + 1}s",
                              wait)
    old, new = b.replace_encryption_key(agent_id)
    at = datetime.now(UTC).isoformat()
    p = signing.payload("encryption-key", agent_id, new, at)
    sig = _sign_with(agent_id, _identity(agent_id), lambda h: identity.sign_payload(h, p))
    _write_json(agent_dir(agent_id) / "last_encryption_binding.json", {"key": new})
    audit({"agent": agent_id, "op": "replace-encryption-key", "result": "made"})
    _alert(agent_id, "encryption-key", "a new encryption key, made in the signer — what it is sent is sealed to it")
    return {"old_public_key": old, "encryption_public_key": new, "at": at, "signature": sig}


def _pending_path(agent_id: str) -> Path:
    return agent_dir(agent_id) / "keys" / "identity.ed25519.pending"


def identity_pending(agent_id: str) -> str:
    """P293 (2c): the next identity key, made inside the signer by the back end
    that holds the current one — for a rotation or a recovery its person runs.
    Kept apart until `identity_commit` (after the relay took it); an earlier
    run's pending key is given again, so a rotation can be finished."""
    from emporia import signer_backends as sb
    if not holds(agent_id, "identity"):
        raise sb.BackendError(f"this signer does not hold {agent_id}'s identity key")
    b, _ = route(agent_id, "identity")
    pending = routes(agent_id).get("identity-pending")
    if b.type == "local":
        path = _pending_path(agent_id)
        if not path.exists():
            import secrets as _secrets
            identity._write_secret_atomically(path, _secrets.token_bytes(32))
            own(path)
        from cryptography.hazmat.primitives.asymmetric import ed25519
        return ed25519.Ed25519PrivateKey.from_private_bytes(path.read_bytes()).public_key().public_bytes_raw().hex()
    if not pending:
        ref = b.make(agent_id, "identity")
        with _LOCK:
            items = routes(agent_id)
            items["identity-pending"] = {"backend": b.name, "ref": ref}
            _write_json(agent_dir(agent_id) / "routes.json", items)
        pending = items["identity-pending"]
    return b.public(agent_id, "identity", pending["ref"])


def identity_commit(agent_id: str) -> str:
    """The pending identity key becomes the agent's; the old one is kept in the
    signer's folder (local) or named in its routes (a back end), never deleted."""
    from emporia import signer_backends as sb
    b, _ = route(agent_id, "identity")
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    with _LOCK:
        if b.type == "local":
            path, pending = identity_path(agent_id), _pending_path(agent_id)
            if not pending.exists():
                raise sb.BackendError("no pending identity key to commit")
            identity._write_secret_atomically(path.with_name(f"identity.ed25519.rotated-{stamp}"), path.read_bytes())
            os.replace(pending, path)
        else:
            items = routes(agent_id)
            if "identity-pending" not in items:
                raise sb.BackendError("no pending identity key to commit")
            items.setdefault("identity-previous", []).append({**items["identity"], "until": stamp})
            items["identity"] = items.pop("identity-pending")
            _write_json(agent_dir(agent_id) / "routes.json", items)
    audit({"agent": agent_id, "op": "identity-commit", "result": "made"})
    return identity_public(agent_id) or ""


def identity_abort(agent_id: str) -> None:
    _pending_path(agent_id).unlink(missing_ok=True)
    with _LOCK:
        items = routes(agent_id)
        if items.pop("identity-pending", None) is not None:
            _write_json(agent_dir(agent_id) / "routes.json", items)


# ─── P293 (2c): a backup, from the signer's side ──────────────────────────────

BACKUP_PREFIX = "emporia-signer-backup-v1"


def _backup_key(passphrase: str, salt: bytes) -> bytes:
    from cryptography.hazmat.primitives.kdf.scrypt import Scrypt
    return Scrypt(salt=salt, length=32, n=2 ** 15, r=8, p=1).derive(passphrase.encode())


def backup(agent_id: str, passphrase: str) -> tuple[bytes, list[str]]:
    """The agent's keys the signer holds in its own files, sealed under a
    passphrase (scrypt, AES-256-GCM), for its person to keep off this machine.
    Keys a back end holds (KMS, a provider) never leave it: they are named in the
    backup, and backed up there. (backup bytes, notes on what is elsewhere)."""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    from emporia import signer_backends as sb
    if len(passphrase) < 12:
        raise ValueError("a backup's passphrase is at least 12 characters")
    local = sb.LocalBackend("local")
    keys, notes = {}, []
    for role in sb.ROLES:
        if role in routes(agent_id):
            r = routes(agent_id)[role]
            notes.append(f"{role}: held by the {r['backend']} back end ({r.get('ref', '')}); back it up there")
        elif local.holds(agent_id, role):
            keys[role] = local._raw(agent_id, role).hex()
    retired = agent_dir(agent_id) / "keys" / "encryption.retired"
    if retired.exists():
        keys["encryption-retired"] = identity._parse_retired(retired.read_text())
    if not keys:
        raise ValueError(f"the signer holds no key files for {agent_id}")
    body = json.dumps({"agent": agent_id, "keys": keys, "routes": routes(agent_id),
                       "made": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}).encode()
    salt, nonce = secrets.token_bytes(16), secrets.token_bytes(12)
    ct = AESGCM(_backup_key(passphrase, salt)).encrypt(nonce, body, BACKUP_PREFIX.encode())
    import base64
    out = json.dumps({"format": BACKUP_PREFIX, "kdf": "scrypt-n32768-r8-p1", "salt": salt.hex(),
                      "nonce": nonce.hex(), "ct": base64.b64encode(ct).decode()}).encode()
    audit({"agent": agent_id, "op": "backup", "result": "made", "roles": sorted(keys)})
    return out, notes


def restore(agent_id: str, data: bytes, passphrase: str) -> list[str]:
    """A backup back into this signer, for the same agent; never over a key it holds."""
    import base64

    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    from emporia import signer_backends as sb
    try:
        outer = json.loads(data)
        if outer.get("format") != BACKUP_PREFIX:
            raise ValueError
        body = json.loads(AESGCM(_backup_key(passphrase, bytes.fromhex(outer["salt"]))).decrypt(
            bytes.fromhex(outer["nonce"]), base64.b64decode(outer["ct"]), BACKUP_PREFIX.encode()))
    except InvalidTag:
        raise ValueError("that passphrase does not open this backup") from None
    except (ValueError, KeyError, TypeError):
        raise ValueError("that is not a signer backup") from None
    if body.get("agent") != agent_id:
        raise ValueError(f"that backup is {body.get('agent')!r}'s, not {agent_id!r}'s")
    local = sb.LocalBackend("local")
    roles = [r for r in sb.ROLES if r in body["keys"]]
    if held := [r for r in roles if holds(agent_id, r)]:
        raise ValueError(f"the signer already holds {', '.join(held)} for {agent_id}; nothing restored")
    for role in roles:
        path = local._path(agent_id, role)
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        identity._write_secret_atomically(path, bytes.fromhex(body["keys"][role]))
    if body["keys"].get("encryption-retired"):
        identity._write_secret_atomically(agent_dir(agent_id) / "keys" / "encryption.retired",
                                          ("\n".join(body["keys"]["encryption-retired"]) + "\n").encode())
    own(agent_dir(agent_id))
    audit({"agent": agent_id, "op": "restore", "result": "made", "roles": roles})
    return roles


# ─── P293 (signer phase 2b): the encryption keys, and opening ─────────────────

def encryption_path(agent_id: str) -> Path:
    return agent_dir(agent_id) / "keys" / "encryption.x25519"


def encryption_keys(agent_id: str) -> list:
    """The agent's X25519 key and the ones it replaced (newest first), as the signer holds them."""
    from emporia import signer_backends as sb
    return sb.LocalBackend("local").encryption_keys(agent_id)


# A burst of openings is how a takeover reading the backlog shows: alerted, and
# optionally capped (EMPORIA_SIGNER_OPEN_PER_MIN in its policy; off by default).
OPEN_ALERT_PER_10_MIN = int(os.getenv("EMPORIA_SIGNER_OPEN_ALERT", "300"))
_OPENS: dict[str, list[float]] = {}
_OPEN_COUNTS: dict[str, dict] = {}
# The contexts a sealed message is opened in (crypto_box.message_aad's `kind`).
OPEN_KINDS = ("dm", "room", "session", "topic")   # topic: sealed Agora posts (sealed_posts.py)


def _count_open(agent_id: str, ok: bool, now: float, kind: str = "") -> None:
    """One audit line per agent per minute — how many it opened, of which kind, never what."""
    c = _OPEN_COUNTS.setdefault(agent_id, {"minute": int(now // 60), "opened": 0, "failed": 0, "kinds": {}})
    if c["minute"] != int(now // 60):
        if c["opened"] or c["failed"]:
            audit({"agent": agent_id, "op": "open", "result": "counted", "opened": c["opened"],
                   "failed": c["failed"], "kinds": dict(c.get("kinds") or {}), "minute": c["minute"] * 60})
        c.update(minute=int(now // 60), opened=0, failed=0, kinds={})
    c["opened" if ok else "failed"] += 1
    if kind:
        c.setdefault("kinds", {})[kind] = c.get("kinds", {}).get(kind, 0) + 1


def open_rule(agent_id: str, envelope, label: str, aad: bytes) -> tuple[str, str]:
    """P293 (2b): what the signer opens — a sealed message addressed to this agent
    (by its id, or its address `id@host` in a thread with another relay), in a DM,
    room or session context. ("", kind) to open, or (why not, "")."""
    from emporia import crypto_box
    from emporia.addresses import is_address, split_address
    me = agent_id.strip().lower()
    name = (label or "").strip().lower()
    if not (name == me or (is_address(name) and split_address(name)[0] == me)):
        return f"{label!r} is not {agent_id}: the signer opens only what is sealed to its own agent", ""
    if not isinstance(envelope, dict) or not isinstance(envelope.get("keys"), dict):
        return "not a sealed message", ""
    if label not in envelope["keys"]:
        return f"not addressed to {agent_id}: no key in it is wrapped for {label}", ""
    try:
        ctx = json.loads(aad)
    except (ValueError, UnicodeDecodeError):
        ctx = None
    if (not isinstance(ctx, dict) or set(ctx) != {"v", "kind", "scope_id", "sender_id", "msg_type", "nonce"}
            or ctx.get("v") != crypto_box.ENVELOPE_VERSION or ctx.get("kind") not in OPEN_KINDS
            or not isinstance(ctx.get("scope_id"), str) or not ctx["scope_id"]):
        return "the context it is to be opened in is not a DM's, a room's or a session's", ""
    return "", ctx["kind"]


def open_for(agent_id: str, envelope: dict, label: str, aad: bytes) -> str:
    from emporia import crypto_box, signing
    if not holds(agent_id, "encryption"):
        raise signing.Refused(f"this signer does not hold {agent_id}'s encryption keys")
    why, kind = open_rule(agent_id, envelope, label, aad)
    if why:
        audit({"agent": agent_id, "op": "open", "result": "refused", "why": why[:300]})
        _alert(agent_id, "refused", f"open: {why}")
        raise signing.Refused(f"not opened: {why}")
    now = time.time()
    with _LOCK:
        recent = [t for t in _OPENS.get(agent_id, []) if t > now - 600]
        cap = policy(agent_id).get("env", {}).get("EMPORIA_SIGNER_OPEN_PER_MIN", "")
        if cap.isdigit() and int(cap) and sum(1 for t in recent if t > now - 60) >= int(cap):
            raise signing.Limited(f"not opened: at most {cap} messages a minute", 60)
        recent.append(now)
        _OPENS[agent_id] = recent
        if len(recent) == OPEN_ALERT_PER_10_MIN:
            _alert(agent_id, "open-burst", f"{len(recent)} messages opened in 10 minutes — if that was not you "
                                           f"reading old threads, its process may be reading the backlog")
    b, ref = route(agent_id, "encryption")
    try:
        text = b.open_sealed(agent_id, envelope, label, aad, ref)
    except crypto_box.DecryptionError:
        _count_open(agent_id, False, now, kind)
        raise
    _count_open(agent_id, True, now, kind)
    return text


def _safe_error(e: BaseException) -> str:
    """An exception as told to the agent: never an RPC URL (it may carry a provider's key)."""
    from emporia.rpc_safe import describe
    return describe(e)


def audit_rows() -> list[dict]:
    """Every audit entry, oldest first: the main file and the refusals' file merged by time."""
    rows = []
    for fname in ("audit.jsonl", "audit-refused.jsonl"):
        try:
            lines = (signer_dir() / fname).read_text().splitlines()
        except OSError:
            continue
        for line in lines:
            try:
                e = json.loads(line)
            except ValueError:
                continue
            if isinstance(e, dict):
                rows.append(e)
    return sorted(rows, key=lambda e: e.get("at", 0))


def log(agent_id: str = "", limit: int = 50) -> list[dict]:
    """The audit's last entries, for one agent or all: kinds, results, reasons — never content."""
    out = [e for e in audit_rows() if not agent_id or e.get("agent") == agent_id]
    return out[-limit:] if limit else []


# ─── the service ──────────────────────────────────────────────────────────────

app = FastAPI(title="Emporia signer", docs_url=None, redoc_url=None, openapi_url=None)


def _agent(request: Request) -> str:
    agent_id = request.headers.get("x-emporia-agent", "")
    token = request.headers.get("authorization", "").removeprefix("Bearer ").strip()
    # Review pass 2: the id exactly as the signer files it. `agent_dir` folds case and odd
    # characters, so `ALICE` or ` alice` found alice's policy and token, while the
    # per-process counters (`_OPENS`, the open cap and its burst alert) and approvals keyed
    # the spelling: each new spelling started a fresh count.
    if agent_id != identity._keyfile_stem(agent_id):
        agent_id = ""
    want = policy(agent_id).get("token_sha256", "") if agent_id else ""
    if not (want and token and hmac.compare_digest(want, _hash(token))):
        raise HTTPException(401, "not an agent this signer holds keys for, or the wrong token")
    return agent_id


def _admin(request: Request) -> None:
    token = request.headers.get("authorization", "").removeprefix("Bearer ").strip()
    if not (token and hmac.compare_digest(_hash(admin_token()), _hash(token))):
        raise HTTPException(401, "admin token required")


@app.get("/health")
def health() -> dict:
    """Its user and whether it runs in a container, so a client can tell
    whether it is apart from the agents, and what could get round that."""
    from emporia.relay_host import in_container
    return {"service": "emporia-signer", "uid": os.getuid(), "in_container": in_container()}


@app.get("/v1/wallet")
def wallet(request: Request) -> dict:
    aid = _agent(request)
    with agent_wallet.scope(_scope(aid)):
        families = agent_wallet._families(aid)
        try:
            networks = agent_wallet.allowed_networks()
        except agent_wallet.AgentWalletError:
            networks = []
        return {"agent_id": aid, "families": list(families),
                "address": agent_wallet.address(aid) if "evm" in families else "",
                "solana_address": agent_wallet.solana_address(aid) if "svm" in families else "",
                "pay_networks": networks, "max_payment_cents": agent_wallet.max_payment_cents(),
                "max_daily_cents": agent_wallet.max_daily_cents(),
                "spent_today_atomic": agent_wallet.spent_today_atomic(aid)}


def _run(request: Request, fn):
    from emporia import signer_backends as sb
    aid = _agent(request)
    op = request.url.path.rsplit("/", 1)[-1]
    load_settings()
    with agent_wallet.scope(_scope(aid)):
        before = agent_wallet.spent_today_atomic(aid)
        try:
            out = fn(aid)
        except ApprovalPending as e:
            audit({"agent": aid, "op": op, "result": "waiting", "intent": e.intent_id})
            raise HTTPException(409, {"error": str(e), "intent": e.intent_id}) from None
        except agent_wallet.AgentWalletError as e:
            audit({"agent": aid, "op": op, "result": "refused", "why": str(e)[:300]})
            raise HTTPException(403, {"error": str(e)}) from None
        except sb.BackendError as e:   # P293: its back end cannot, or would not
            audit({"agent": aid, "op": op, "result": "refused", "why": str(e)[:300]})
            raise HTTPException(403, {"error": f"not paid: {e}"}) from None
        except Exception as e:   # a malformed offer, a refused requirement: say so, never a traceback
            audit({"agent": aid, "op": op, "result": "error", "why": _safe_error(e)[:300]})
            raise HTTPException(400, {"error": _safe_error(e)}) from None
        audit({"agent": aid, "op": op, "result": "signed",
               "amount_atomic": agent_wallet.spent_today_atomic(aid) - before})
        return out


# S-6: a fee leg is bound to the creator payment this signer itself signed for the agent a moment before, never to
# a creator_amount the caller says: a compromised agent cannot make any fee look small by naming a huge price.
CREATOR_LEG_TTL_S = 120
_CREATOR_LEGS: dict[str, list[tuple[float, int, str]]] = {}


def _note_creator_leg(agent_id: str, header: str) -> None:
    from emporia.payments import extract_x402_payload
    accepted = (extract_x402_payload(header) or {}).get("accepted") or {}
    try:
        amount = int(accepted.get("amount") or 0)
    except (TypeError, ValueError):
        return
    if amount > 0:
        with _LOCK:
            legs = [x for x in _CREATOR_LEGS.get(agent_id, []) if time.time() - x[0] < CREATOR_LEG_TTL_S]
            legs.append((time.time(), amount, str(accepted.get("network") or "")))
            _CREATOR_LEGS[agent_id] = legs[-8:]


def _creator_leg_for_fee(agent_id: str, network: str) -> tuple[int, tuple]:
    with _LOCK:
        for leg in reversed(_CREATOR_LEGS.get(agent_id, [])):
            if time.time() - leg[0] < CREATOR_LEG_TTL_S and (not network or leg[2] == network):
                return leg[1], leg
    raise agent_wallet.AgentWalletError(
        "not paid: a relay fee is paid only together with the creator's share, which this signer signs first; "
        "none was signed for this agent in the last two minutes")


def _x402(body: dict):
    fee_leg = bool(body.get("fee_leg"))

    def go(aid: str) -> str:
        creator, used = 0, None
        if fee_leg:
            creator, used = _creator_leg_for_fee(aid, body.get("network", ""))
        header = agent_wallet.sign_exact(aid, body["body"], body.get("network", ""), fee_leg=fee_leg,
                                         creator_amount=creator)
        if fee_leg:
            with _LOCK:
                if used in _CREATOR_LEGS.get(aid, []):
                    _CREATOR_LEGS[aid].remove(used)
        else:
            _note_creator_leg(aid, header)
        return header
    return go


# The paying handlers do blocking work (a Tempo transfer waits on the chain for up to a minute; a reclaim runs its
# own event loop), so they run in a worker thread and never hold up other agents' signing on the loop.
@app.post("/v1/x402")
async def sign_x402(request: Request) -> dict:
    body = await request.json()
    return {"header": await run_in_threadpool(_run, request, _x402(body))}


@app.post("/v1/tempo")
async def pay_tempo(request: Request) -> dict:
    body = await request.json()
    return {"header": await run_in_threadpool(
        _run, request, lambda aid: agent_wallet.pay_tempo(aid, body["offer"]))}


@app.post("/v1/escrow")
async def sign_escrow(request: Request) -> dict:
    body = await request.json()
    return {"header": await run_in_threadpool(_run, request, lambda aid: agent_wallet.sign_escrow(
        aid, body["offer"], expected_amount=int(body["expected_amount"])))}


@app.post("/v1/reclaim")
async def reclaim(request: Request) -> dict:
    body = await request.json()
    return await run_in_threadpool(_run, request, lambda aid: agent_wallet.reclaim_escrow(aid, body["hold"]))


def _signing_call(request: Request, kind: str, fn, key: str = "signature"):
    from emporia import signing
    aid = _agent(request)
    try:
        out = fn(aid)
    except signing.Limited as e:
        audit({"agent": aid, "op": "sign", "kind": kind, "result": "limited", "why": str(e)[:300]})
        _alert(aid, "refused", str(e))
        raise HTTPException(429, {"error": str(e), "retry_after": e.retry_after}) from None
    except signing.Refused as e:
        audit({"agent": aid, "op": "sign", "kind": kind, "result": "refused", "why": str(e)[:300]})
        _alert(aid, "refused", f"{kind if kind in signing.KINDS else 'unknown kind'}: {e}")
        raise HTTPException(403, {"error": str(e)}) from None
    except Exception as e:   # wrong arguments for the kind: say so, never a traceback
        audit({"agent": aid, "op": "sign", "kind": kind, "result": "error", "why": f"{type(e).__name__}: {e}"[:300]})
        raise HTTPException(400, {"error": f"{type(e).__name__}: {e}"}) from None
    audit({"agent": aid, "op": "sign", "kind": kind, "result": "signed"})
    return out if key == "result" else {key: out}


@app.get("/v1/identity")
def get_identity(request: Request) -> dict:
    from emporia import signer_backends as sb
    aid = _agent(request)
    try:
        pub = identity_public(aid)
        enc = public(aid, "encryption")
    except sb.BackendError as e:
        raise HTTPException(502, {"error": str(e)}) from None
    if pub is None:
        raise HTTPException(404, "this signer does not hold its identity key")
    return {"agent_id": aid, "public_key": pub, "encryption_public_key": enc}


@app.post("/v1/sign")
async def sign(request: Request) -> dict:
    body = await request.json()
    kind = str(body.get("kind", ""))
    args, kwargs = body.get("args") or [], body.get("kwargs") or {}
    if not isinstance(args, list) or not isinstance(kwargs, dict):
        raise HTTPException(400, {"error": "args is a list and kwargs an object"})
    return _signing_call(request, kind, lambda aid: sign_kind(aid, kind, args, kwargs))


@app.post("/v1/open")
async def post_open(request: Request) -> dict:
    import base64

    from emporia import crypto_box, signing
    aid = _agent(request)
    body = await request.json()
    envelope, label = body.get("envelope"), str(body.get("label") or aid)
    try:
        aad = base64.b64decode(str(body.get("aad", "")), validate=True)
    except ValueError:
        raise HTTPException(400, {"error": "aad is base64"}) from None
    try:
        return {"plaintext": open_for(aid, envelope, label, aad)}
    except signing.Limited as e:
        raise HTTPException(429, {"error": str(e), "retry_after": e.retry_after}) from None
    except signing.Refused as e:
        raise HTTPException(403, {"error": str(e)}) from None
    except crypto_box.DecryptionError as e:
        raise HTTPException(422, {"error": str(e)}) from None


@app.post("/v1/replace-encryption-key")
def post_replace_encryption_key(request: Request) -> dict:
    """P293 (2c): a new encryption key, made here; its binding signed."""
    return _signing_call(request, "encryption-key", lambda aid: replace_encryption_key(aid), key="result")


@app.get("/admin/log")
def admin_log(request: Request, agent: str = "", limit: int = 50) -> dict:
    _admin(request)
    return {"entries": log(agent, max(1, min(limit, 500)))}


@app.get("/admin/intents")
def list_intents(request: Request) -> dict:
    _admin(request)
    return {"intents": intents()}


@app.post("/admin/intents/{intent_id}/{verb}")
def decide_intent(intent_id: str, verb: str, request: Request) -> dict:
    _admin(request)
    if verb not in ("approve", "deny"):
        raise HTTPException(404, "approve or deny")
    try:
        return decide(intent_id, verb == "approve")
    except KeyError:
        raise HTTPException(404, f"no pending intent {intent_id}") from None


# Where `emporia signer host` puts it (P207): a socket shared with Hermes's
# container, or — for Hermes on the host, where a container's socket cannot be
# reached (Docker Desktop passes no Unix sockets) — a port on 127.0.0.1.
HOSTED_SOCKET = Path("/run/emporia-signer/signer.sock")
HOSTED_PORT = 8790


def socket_path() -> Path:
    """EMPORIA_SIGNER_SOCKET; else a signer running apart, if one answers there;
    else practice mode's, beside its folder."""
    raw = os.getenv("EMPORIA_SIGNER_SOCKET", "").strip()
    if raw:
        return Path(raw).expanduser()
    if HOSTED_SOCKET.is_socket():
        return HOSTED_SOCKET
    return signer_dir().with_name(signer_dir().name + ".sock")


# Linux keeps a Unix socket's path in 108 bytes, its terminating zero included.
_MAX_SOCKET_PATH = 107


_PORT_ANSWERED: dict = {}


def default_url() -> str:
    """Where to find the signer when nothing says: a hosted one that answers,
    else practice mode's socket. The port is probed once per process."""
    if not os.getenv("EMPORIA_SIGNER_SOCKET", "").strip():
        if HOSTED_SOCKET.is_socket():
            return f"unix://{HOSTED_SOCKET}"
        if "up" not in _PORT_ANSWERED:
            import httpx
            try:
                _PORT_ANSWERED["up"] = httpx.get(f"http://127.0.0.1:{HOSTED_PORT}/health",
                                                 timeout=1).json().get("service") == "emporia-signer"
            except (httpx.HTTPError, ValueError):
                _PORT_ANSWERED["up"] = False
        if _PORT_ANSWERED["up"]:
            return f"http://127.0.0.1:{HOSTED_PORT}"
    return f"unix://{socket_path()}"


def unlock() -> list[str]:
    """P293: every key a password manager holds for an agent here, read into this
    process now (at start), so a locked vault shows at once and not at an agent's
    first signature. Problems, said without a value."""
    problems = []
    for agent, rs in all_routes().items():
        for role, r in rs.items():
            try:
                if role in ("identity", "encryption", "evm", "svm") and get_backend(r["backend"]).type == "vault":
                    public(agent, role)
            except Exception as e:  # noqa: BLE001 — reported, and the signer still starts
                problems.append(f"{agent} {role}: {e}")
    return problems


def serve(socket: str = "", port: int = 0) -> None:
    import sys

    import uvicorn

    from emporia.relay_host import in_container
    signer_dir().mkdir(mode=0o700, parents=True, exist_ok=True)
    admin_token()
    load_settings()
    for problem in unlock():
        print(f"emporia signer: {problem}", file=sys.stderr)
    if port:
        # In its own container the port is published on the host's 127.0.0.1
        # only (compose); the container's own interface is not the host's.
        uvicorn.run(app, host="0.0.0.0" if in_container() else "127.0.0.1", port=port, log_level="warning")
        return
    sock = Path(socket) if socket else socket_path()
    if len(str(sock).encode()) > _MAX_SOCKET_PATH:
        raise SystemExit(f"emporia signer: the socket path is {len(str(sock).encode())} bytes; a Unix socket's "
                         f"must be at most {_MAX_SOCKET_PATH}. Give a shorter one: --socket /run/emporia-signer/"
                         f"signer.sock (or EMPORIA_SIGNER_SOCKET)")
    sock.parent.mkdir(parents=True, exist_ok=True)
    sock.unlink(missing_ok=True)
    # Agents run as another user: they may connect (the token decides the
    # rest); the folder around it stays the signer's.
    old = os.umask(0o111)
    try:
        uvicorn.run(app, uds=str(sock), log_level="warning")
    finally:
        os.umask(old)
