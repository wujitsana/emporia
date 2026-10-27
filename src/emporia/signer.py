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
                "EMPORIA_PAYMENT_APPROVAL_CENTS", "EMPORIA_X402_MAINNET")
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


def _write_json(path: Path, data) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    identity._write_secret_atomically(path, json.dumps(data, indent=1).encode())


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
    """Its user, so a client can tell whether it is apart from the agents."""
    return {"service": "emporia-signer", "uid": os.getuid()}


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


def socket_path() -> Path:
    raw = os.getenv("EMPORIA_SIGNER_SOCKET", "").strip()
    return Path(raw).expanduser() if raw else signer_dir().with_name(signer_dir().name + ".sock")


# Linux keeps a Unix socket's path in 108 bytes, its terminating zero included.
_MAX_SOCKET_PATH = 107


def serve(socket: str = "", port: int = 0) -> None:
    import uvicorn
    signer_dir().mkdir(mode=0o700, parents=True, exist_ok=True)
    admin_token()
    load_settings()
    if port:
        uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")
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
