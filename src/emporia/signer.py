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
                  "EMPORIA_BACKUP_RELAYS", "EMPORIA_VISIT_RELAYS"))
SIGNING_POLICY = POLICY_NAMES[5:]
_LOCK = threading.Lock()


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
                              approver=lambda message: _decide(agent_id, message))


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
    path = signer_dir() / "audit.jsonl"
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
                       "status": "pending"}
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


# ─── P293 (signer phase 2a): the identity key, and signatures by kind ────────

def identity_path(agent_id: str) -> Path:
    return agent_dir(agent_id) / "keys" / "identity.ed25519"


def identity_key(agent_id: str):
    """The agent's Ed25519 identity key, if the signer holds it (`signer add NAME --identity`)."""
    from cryptography.hazmat.primitives.asymmetric import ed25519
    path = identity_path(agent_id)
    if not path.exists():
        return None
    return ed25519.Ed25519PrivateKey.from_private_bytes(path.read_bytes())


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
        return "encryption-key", "a new encryption key binding — what it is sent is sealed to it"
    if kind == "relay-list":
        path = agent_dir(agent_id) / "last_relay_list.json"
        last = _read_json(path, {})
        now = {"home": payload.get("home"), "backups": sorted(payload.get("backups") or [])}
        _write_json(path, now)
        if last and last != now:
            return "home-or-backups", f"home {now['home']}, backups {', '.join(now['backups']) or 'none'}"
    return None


def sign_kind(agent_id: str, kind: str, args: list, kwargs: dict) -> str:
    """Build the payload from the kind and arguments, apply its rule with this
    signer's history and the person's policy, sign with the key held here."""
    from emporia import signing
    priv = identity_key(agent_id)
    if priv is None:
        raise signing.Refused(f"this signer does not hold {agent_id}'s identity key — "
                              f"emporia signer add {agent_id} --identity moves it here")
    p = signing.payload(kind, *args, **kwargs)
    env = dict(policy(agent_id).get("env", {}))
    with agent_wallet.scope(_scope(agent_id)):        # its own wallets, as the signer holds them
        signing.check(kind, p, signing.Context(agent_id, env=env), history=FileHistory(agent_id))
    sig = identity.sign_payload(priv, p)
    if change := _changes(agent_id, kind, p):
        _alert(agent_id, *change)
    return sig


def sign_in(agent_id: str, nonce: str) -> str:
    from emporia import signing
    priv = identity_key(agent_id)
    if priv is None:
        raise signing.Refused(f"this signer does not hold {agent_id}'s identity key")
    signing.check("sign-in", {"nonce": nonce}, signing.Context(agent_id, env=dict(policy(agent_id).get("env", {}))),
                  history=FileHistory(agent_id))
    return priv.sign(identity.challenge_bytes(nonce)).hex()


def log(agent_id: str = "", limit: int = 50) -> list[dict]:
    """The audit's last entries, for one agent or all: kinds, results, reasons — never content."""
    path = signer_dir() / "audit.jsonl"
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return []
    out = []
    for line in reversed(lines):
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if not agent_id or e.get("agent") == agent_id:
            out.append(e)
            if len(out) >= limit:
                break
    return list(reversed(out))


# ─── the service ──────────────────────────────────────────────────────────────

app = FastAPI(title="Emporia signer", docs_url=None, redoc_url=None, openapi_url=None)


def _agent(request: Request) -> str:
    agent_id = request.headers.get("x-emporia-agent", "")
    token = request.headers.get("authorization", "").removeprefix("Bearer ").strip()
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
        except Exception as e:   # a malformed offer, a refused requirement: say so, never a traceback
            audit({"agent": aid, "op": op, "result": "error", "why": f"{type(e).__name__}: {e}"[:300]})
            raise HTTPException(400, {"error": f"{type(e).__name__}: {e}"}) from None
        audit({"agent": aid, "op": op, "result": "signed",
               "amount_atomic": agent_wallet.spent_today_atomic(aid) - before})
        return out


@app.post("/v1/x402")
async def sign_x402(request: Request) -> dict:
    body = await request.json()
    return {"header": _run(request, lambda aid: agent_wallet.sign_exact(aid, body["body"], body.get("network", "")))}


@app.post("/v1/tempo")
async def pay_tempo(request: Request) -> dict:
    body = await request.json()
    return {"header": _run(request, lambda aid: agent_wallet.pay_tempo(aid, body["offer"]))}


@app.post("/v1/escrow")
async def sign_escrow(request: Request) -> dict:
    body = await request.json()
    return {"header": _run(request, lambda aid: agent_wallet.sign_escrow(
        aid, body["offer"], expected_amount=int(body["expected_amount"])))}


@app.post("/v1/reclaim")
async def reclaim(request: Request) -> dict:
    body = await request.json()
    return _run(request, lambda aid: agent_wallet.reclaim_escrow(aid, body["hold"]))


def _signing_call(request: Request, kind: str, fn):
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
        _alert(aid, "refused", f"{kind}: {e}")
        raise HTTPException(403, {"error": str(e)}) from None
    except Exception as e:   # wrong arguments for the kind: say so, never a traceback
        audit({"agent": aid, "op": "sign", "kind": kind, "result": "error", "why": f"{type(e).__name__}: {e}"[:300]})
        raise HTTPException(400, {"error": f"{type(e).__name__}: {e}"}) from None
    audit({"agent": aid, "op": "sign", "kind": kind, "result": "signed"})
    return {"signature": out}


@app.get("/v1/identity")
def get_identity(request: Request) -> dict:
    aid = _agent(request)
    priv = identity_key(aid)
    if priv is None:
        raise HTTPException(404, "this signer does not hold its identity key")
    return {"agent_id": aid, "public_key": priv.public_key().public_bytes_raw().hex()}


@app.post("/v1/sign")
async def sign(request: Request) -> dict:
    body = await request.json()
    kind = str(body.get("kind", ""))
    args, kwargs = body.get("args") or [], body.get("kwargs") or {}
    if not isinstance(args, list) or not isinstance(kwargs, dict):
        raise HTTPException(400, {"error": "args is a list and kwargs an object"})
    return _signing_call(request, kind, lambda aid: sign_kind(aid, kind, args, kwargs))


@app.post("/v1/sign-in")
async def post_sign_in(request: Request) -> dict:
    body = await request.json()
    return _signing_call(request, "sign-in", lambda aid: sign_in(aid, str(body.get("nonce", ""))))


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


def serve(socket: str = "", port: int = 0) -> None:
    import uvicorn

    from emporia.relay_host import in_container
    signer_dir().mkdir(mode=0o700, parents=True, exist_ok=True)
    admin_token()
    load_settings()
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
