"""One deterministic environment for the whole suite, pinned before emporia loads (P60).

`test_emporia.py` already pinned five permissive flags at module scope, with a
comment noting "profile .env may have stricter settings". That was right about
the flags and stopped short of the rest. Measured: on a developer machine the
profile `.env` injects **ten** further variables into every test run, three of
them live credentials —

    STRIPE_SECRET_KEY   NVIDIA_API_KEY   EMPORIA_NOUS_JWT
    OPENROUTER_API_KEY  STRIPE_PROFILE_ID   STRIPE_API_VERSION
    EMPORIA_AGENT_ID    EMPORIA_RELAY_URL
    EMPORIA_GUARDRAILS_MODE

Two consequences, and the second is what P51r ran into:

1. **The suite runs with production secrets in scope.** P46 established that a
   mocked `httpx.AsyncClient.post` accepts every URL equally — so a test that
   forgets a mock reaches the real service, and here it would arrive holding a
   real key. Pinning obvious dummies means a missed mock fails loudly on
   authentication instead of quietly succeeding against someone's account.

2. **The suite behaves differently in CI, where none of these exist.** P51r
   found three reconciler tests that passed locally and would have done nothing
   at all in CI, because `_reconcile_owed_payouts()` returns early without
   `STRIPE_SECRET_KEY` and the developer's environment happened to supply one.
   Those three now set it explicitly; this stops the next three from happening.

Why this works: since P55, `emporia.config.load_profile_dotenv()` merges with
**setdefault** semantics — a value already in the environment always wins. A
conftest is imported by pytest before any test module, so pinning here happens
before the merge can fill anything in, and the suite's environment is whatever
this file says it is on every machine.

`test_installer.py` gets the same treatment for free, which module-scope setup
in `test_emporia.py` never gave it.
"""

from __future__ import annotations

import pytest

import os
import shutil
import socket
import sys
import tempfile
from pathlib import Path

# Allow running from the repo root without installing.
_src = str(Path(__file__).parent.parent / "src")
if _src not in sys.path:
    sys.path.insert(0, _src)

# P275: imported as `tests.conftest` this module would run a second time — a
# second TEST_ROOT, with os.environ pointed at it — and later tests then read
# and write a different database than the one the suite set up (three tests in
# test_emporia found it empty). Import it as `conftest`, as every test does.
if __name__ != "conftest":
    raise ImportError("import the suite's conftest as `conftest`, not as " + repr(__name__))

# One throwaway root for every artifact this run writes (logs, audit, DB),
# honouring TMPDIR so CI sandboxes and hosts without a writable /tmp both work.
TEST_ROOT = Path(tempfile.mkdtemp(prefix="emporia_test_"))
TEST_DB = str(TEST_ROOT / "emporia_test.sqlite3")

# Written into os.environ below. Anything absent here is whatever the profile
# .env supplies, which is the situation this file exists to end — so a new
# config variable the relay reads belongs in this table, not in ambient state.
TEST_ENVIRONMENT: dict[str, str] = {
    # Where this run's artifacts go.
    "EMPORIA_DB_PATH": TEST_DB,
    # P99: keys too. The throwaway key directory used to be a fixture in
    # test_emporia.py, so any other test file run on its own generated its
    # agents' keys in the developer's real ~/.hermes/keys -- hundreds of them,
    # found there. The variable covers code that reads it at import and
    # subprocesses; the fixture below covers anything imported before this ran.
    "EMPORIA_KEYS_DIR": str(TEST_ROOT / "keys"),
    "EMPORIA_LOG_DIR": str(TEST_ROOT / "logs"),
    "EMPORIA_AUDIT_DIR": str(TEST_ROOT / "audit"),

    # Permissive gates. Tests that verify the *gated* behaviour opt back in with
    # patch.dict or by patching the module global (several are computed once at
    # import and will not re-read the variable).
    "EMPORIA_WRITE_REQUIRES_NOUS": "0",
    "EMPORIA_REQUIRE_NOUS": "0",
    "EMPORIA_REQUIRE_CHALLENGE": "0",

    # Credentials: obviously-fake, never blank. A blank key makes a forgotten
    # mock fail with "not configured", which reads like a test-setup problem; a
    # visibly fake one fails at the remote end, which reads like what it is.
    # `sk_test_` matters beyond looking right — the relay branches on that
    # prefix to decide it is in test mode (see _stripe_key_mode).
    "STRIPE_SECRET_KEY": "sk_test_emporia_suite_not_a_real_key",
    "STRIPE_PROFILE_ID": "prof_emporia_suite",
    # P108: and no profile .env at all — the suite runs here as it runs in CI,
    # where none exists. Pins above stay as the suite's own explicit values.
    "EMPORIA_NO_DOTENV": "1",
    # P194: no background archive runs against whatever relay a test points at.
    "EMPORIA_NO_ARCHIVE": "1",
    # P114: and never the installed Hermes. nous_token.current() asks Hermes for
    # a current Nous token when none is configured; from a test that fetched the
    # developer's real token (and a failing assertion printed it).
    "EMPORIA_HERMES_BIN": "/nonexistent/emporia-suite/hermes",
    # P135: chess is the suite's long-standing wager for testing payment
    # mechanics, so the suite opts in to stakes on engine-decided games the way
    # an operator would. test_engine_games.py clears it to test the refusal.
    "EMPORIA_ENGINE_GAME_STAKES": "1",
    # P281: the transparency log is off by default; the suite keeps it on so every
    # write path is still tested with its leaf. test_translog checks the default.
    "EMPORIA_TRANSPARENCY_LOG": "on",
    # P106: named in the docstring above and never actually pinned, so the
    # profile .env's value reached the suite — and the live SPT lane sent it.
    "STRIPE_API_VERSION": "2026-04-22.preview",
    "NVIDIA_API_KEY": "",
    "OPENROUTER_API_KEY": "",
    "EMPORIA_NOUS_JWT": "",

    # Behaviour switches the profile would otherwise choose for us.
    "EMPORIA_GUARDRAILS_MODE": "enforce",
    "EMPORIA_AGENT_ID": "",
    "EMPORIA_RELAY_URL": "http://localhost:8088",

    # P99: the x402 escrow operator and its network. The profile .env may name
    # a real operator address and key; a test that inherited them would sign
    # with, or be refused for, a key it did not choose. Escrow stays off unless
    # a test configures it.
    "EMPORIA_OPERATOR_SIGNER": "env",
    "EMPORIA_OPERATOR_PRIVATE_KEY": "",
    "EMPORIA_OPERATOR_ADDRESS": "",
    "EMPORIA_ALLOW_HOT_OPERATOR_KEY": "",
    "EMPORIA_ESCROW_NETWORK": "",
    "EMPORIA_ESCROW_RPC_URL": "",
    "EMPORIA_ESCROW_CONTRACT": "",
    "EMPORIA_ESCROW_COLLECTOR": "",
    "EMPORIA_ESCROW_FEE_RECEIVER": "",
}

os.environ.update(TEST_ENVIRONMENT)


def pytest_sessionfinish(session, exitstatus):  # noqa: ARG001
    shutil.rmtree(TEST_ROOT, ignore_errors=True)


# --- no outbound network from the default suite (P60) ---------------------
#
# Measured before it was added: blocking outbound connections changed nothing
# about the result (863 passed either way) and took the run from 130s to 58s.
# That difference is real traffic to api.stripe.com, made in paths that swallow
# the failure by design — `payments.find_settlement_transfer()` catches every
# exception and reports "couldn't ask", which is correct behaviour in production
# and indistinguishable from "mocked" in a test. Nothing failed, so nothing said.
#
# Blocking it makes the suite deterministic, runnable offline, twice as fast,
# and turns any future unmocked call into an immediate error naming the host
# rather than a silent round trip. Loopback stays open: TestClient, the ASGI
# transport and the local relay probes all need it.
#
# The `testnet` tier is exempt — those tests exist to talk to the real
# facilitator and a real Base Sepolia RPC (see docs/testnet.md). They are
# deselected by default via `addopts = -m 'not testnet'`, so the block is
# skipped only when someone has explicitly opted into them.
_real_connect = socket.socket.connect


def _no_outbound(self, address, *args, **kwargs):
    host = address[0] if isinstance(address, tuple) and address else str(address)
    if isinstance(host, str) and (host.startswith("127.") or host in ("localhost", "::1")):
        return _real_connect(self, address, *args, **kwargs)
    raise OSError(
        f"outbound network is blocked in the test suite (tried {host!r}). "
        "A test reached a real service — mock it, or mark the test `testnet` "
        "if it genuinely needs the network. See tests/conftest.py."
    )


def pytest_configure(config):
    selected = config.getoption("-m", default="") or ""
    if "testnet" in selected and "not testnet" not in selected:
        return
    socket.socket.connect = _no_outbound


# P72-4: schema initialisation belongs here, not in a test module.
#
# `_init_test_db` lived in test_emporia.py as a session-scoped autouse fixture,
# which means it only ran when *that module was collected*. Any other test file
# run on its own got an empty database and failed with "no such table:
# authorized_agents" — a failure that looks like a broken test rather than
# missing infrastructure.
#
# This is the same lesson this file already records for environment pinning: it
# used to live in test_emporia.py too, and test_installer.py silently never got
# it. Shared setup in a test module is shared with nobody.
@pytest.fixture(scope="session", autouse=True)
def _test_keys_dir_for_every_module():
    """Every test's keys live under this run's TEST_ROOT, never a real key
    directory -- whichever test files are collected. Same lesson as the DB
    fixture below: shared setup in a test module is shared with nobody."""
    from unittest.mock import patch

    from emporia import identity
    keys = TEST_ROOT / "keys"
    keys.mkdir(mode=0o700, parents=True, exist_ok=True)
    with patch.object(identity, "KEY_DIR", keys):
        yield


@pytest.fixture(scope="session", autouse=True)
def _init_test_db_for_every_module():
    from emporia.relay_server import init_db
    init_db()
    yield
    # Teardown of the test root stays in pytest_sessionfinish (P60).


# P270: reads from other relays go through `emporia.fetch.get`, which streams
# within limits. Tests stand in for other relays by patching `httpx.get`, so here
# it passes through to that; tests/test_fetch.py checks the limits with it off.
@pytest.fixture(autouse=True)
def _fetch_through_httpx_get(monkeypatch):
    from emporia import fetch
    monkeypatch.setattr(fetch, "_PASSTHROUGH", True)


# P204: the signer. Its folder defaults to beside Hermes's profiles — a real
# one on a developer machine — and `emporia signer start` spawns a process. A
# test answering yes to "start the signer?" once left one running there.
@pytest.fixture(autouse=True)
def _no_real_signer(monkeypatch):
    monkeypatch.setenv("EMPORIA_SIGNER_DIR", str(TEST_ROOT / "signer"))
    monkeypatch.delenv("EMPORIA_SIGNER_SOCKET", raising=False)
    monkeypatch.delenv("EMPORIA_SIGNER_URL", raising=False)
    from emporia import cli, signer
    # P209: what this machine allows (sudo, Docker, root) is not the suite's to depend on.
    monkeypatch.setattr(cli, "_ESCALATIONS", [])
    monkeypatch.setattr(signer, "_PORT_ANSWERED", {"up": False})

    def refuse(args, url):
        print("signer: not started — the test suite starts no background signer")
        return 1
    monkeypatch.setattr(cli, "_signer_daemon", refuse)


# --- P261: naming the caller in a test ----------------------------------------
#
# The relay believes only a bearer token: the header trust that let a local
# request name its agent (`X-Emporia-Agent-Id`, `?agent_id=` on a websocket) is
# gone from the relay. Hundreds of tests still name their caller that way, which
# reads well, so this turns the name into a real relay token on its way into the
# app -- the production sign-in path is what they exercise. The header never
# reaches the relay, and an Authorization or X-Emporia-Authorization the test set
# itself is left alone. Tests about the header being refused switch this off with
# `header_shim_off()`.
import contextlib  # noqa: E402
import time as _time  # noqa: E402
from urllib.parse import parse_qsl, urlencode  # noqa: E402

_HEADER_SHIM = {"on": True}


@contextlib.contextmanager
def header_shim_off():
    _HEADER_SHIM["on"] = False
    try:
        yield
    finally:
        _HEADER_SHIM["on"] = True


def _token_for(agent_id: str) -> str:
    rs = sys.modules.get("emporia.relay_server")
    if rs is None:
        import emporia.relay_server as rs
    now = _time.time()
    return rs._jwt_encode({"agent_id": agent_id, "relay_id": rs.RELAY_ID, "iat": int(now), "issued": now,
                           "exp": int(now + 3600)})


def _shim_request(request):
    if not _HEADER_SHIM["on"]:
        return request
    named = request.headers.pop("x-emporia-agent-id", None)
    if named and "x-emporia-authorization" not in request.headers and not str(
            request.headers.get("authorization", "")).startswith("Bearer "):
        request.headers["x-emporia-authorization"] = f"Bearer {_token_for(named)}"
    query = dict(parse_qsl(request.url.query.decode() if isinstance(request.url.query, bytes)
                           else str(request.url.query), keep_blank_values=True))
    if request.url.scheme in ("ws", "wss") and query.get("agent_id") and "token" not in query:
        query["token"] = _token_for(query["agent_id"])
        request.url = request.url.copy_with(query=urlencode(query).encode())
    return _signed_topic(_signed_join(request), named)


def _signed_topic(request, named):
    """P278: a topic's creation, invites, uninvites and kicks are its creator's
    signed acts. A test that names its caller and does not sign has them signed
    here with that agent's test key (header_shim_off() tests the refusals)."""
    import json as _json
    import re as _re
    if not named:      # signed in with a token: the agent it names
        auth = request.headers.get("x-emporia-authorization") or request.headers.get("authorization") or ""
        if auth.startswith("Bearer "):
            rs = sys.modules.get("emporia.relay_server")
            claims = rs._jwt_decode(auth[7:]) if rs else None
            named = (claims or {}).get("agent_id")
    if not named:
        return request
    path, method = request.url.path, request.method
    from datetime import UTC, datetime

    from emporia import identity, wire
    at = datetime.now(UTC).isoformat()
    if method == "DELETE" and (m := _re.fullmatch(r"/agoras/topics/([^/]+)/invite/([^/]+)", path)):
        query = dict(parse_qsl(request.url.query.decode() if isinstance(request.url.query, bytes)
                               else str(request.url.query), keep_blank_values=True))
        if "signature" not in query:
            query.update(at=at, signature=identity.sign(wire.signed_topic_act_payload(
                "uninvite", m.group(1), m.group(2), at), named))
            request.url = request.url.copy_with(query=urlencode(query).encode())
        return request
    if method == "POST" and path == "/sessions":
        return _signed_creation(request, named, at)
    create = method == "POST" and path == "/agoras/topics"
    act = _re.fullmatch(r"/agoras/topics/([^/]+)/(invite|kick)", path) if method == "POST" else None
    if not (create or act):
        return request
    try:
        body = _json.loads(request.read() or b"{}")
    except ValueError:
        return request
    if not isinstance(body, dict) or body.get("signature"):
        return request
    if create:
        body["slug"] = wire.topic_slug(body.get("name", ""), body.get("slug"))
        body["nonce"] = wire.new_message_nonce()
        body["signature"] = identity.sign(wire.signed_agora_topic_payload(
            body["nonce"], body["slug"], body.get("name", ""), body.get("description", ""),
            body.get("visibility", "public"), body.get("gate_type", "open"), body.get("entry_fee_cents", 0),
            body.get("flair_options") or [], body.get("access_duration_days")), named)
    else:
        target = body.get("invitee_id") if act.group(2) == "invite" else body.get("target_agent_id")
        body["at"] = at
        body["signature"] = identity.sign(wire.signed_topic_act_payload(act.group(2), act.group(1), target or "", at),
                                          named)
    headers = {k: v for k, v in request.headers.items() if k.lower() != "content-length"}
    return type(request)(request.method, request.url, headers=headers, content=_json.dumps(body).encode(),
                         extensions=request.extensions)


def _signed_join(request):
    """P272: a join is signed by the joining agent. A test that joins without
    signing has it signed here with the agent's test key (header_shim_off()
    tests the refusals)."""
    import json as _json
    import re as _re
    kind = _re.fullmatch(r"/(sessions|rooms)/[^/]+/join", request.url.path)
    if request.method != "POST" or not kind:
        return request
    try:
        body = _json.loads(request.read() or b"{}")
    except ValueError:
        return request
    if not isinstance(body, dict) or body.get("signature") or not body.get("agent_id"):
        return request
    from datetime import UTC, datetime

    from emporia import identity, wire
    sid = request.url.path.split("/")[2]
    body["at"] = datetime.now(UTC).isoformat()
    make = wire.signed_session_join_payload if kind.group(1) == "sessions" else wire.signed_room_join_payload
    body["signature"] = identity.sign(make(sid, body["agent_id"], body["at"]), body["agent_id"])
    headers = {k: v for k, v in request.headers.items() if k.lower() != "content-length"}
    return type(request)(request.method, request.url, headers=headers, content=_json.dumps(body).encode(),
                         extensions=request.extensions)


def _install_header_shim():
    from starlette import testclient

    real = testclient._TestClientTransport.handle_request
    if getattr(real, "_emporia_shim", False):
        return

    def handle_request(self, request):
        return real(self, _shim_request(request))

    handle_request._emporia_shim = True
    testclient._TestClientTransport.handle_request = handle_request


_install_header_shim()


# --- P262: payout wallets are signed by their agent ---------------------------
def signed_wallet_body(agent_id: str, network_family: str, address: str, *, key_profile: str = "") -> dict:
    """The `POST /agents/{id}/x402-wallet` body, signed with the agent's test key
    (or `key_profile`'s) over `wire.signed_payout_wallet_payload`."""
    from datetime import UTC, datetime

    from emporia import identity, relay_server as rs, wire
    with rs.DB_LOCK, rs.get_db() as conn:
        row = conn.execute("SELECT stable_id FROM authorized_agents WHERE agent_id = ?", (agent_id,)).fetchone()
    at = datetime.now(UTC).isoformat()
    payload = wire.signed_payout_wallet_payload(row["stable_id"] if row else "", network_family, address, at)
    return {"network_family": network_family, "address": address, "at": at,
            "signature": identity.sign(payload, key_profile or agent_id)}


def give_wallet(agent_id: str, network_family: str, address: str) -> None:
    """Store a payout wallet with its agent's signature, as the route would."""
    from emporia import relay_server as rs
    body = signed_wallet_body(agent_id, network_family, address)
    rs.AGENT_REGISTRY.set_x402_pay_to(agent_id, network_family, address,
                                      {"at": body["at"], "signature": body["signature"]})


PAYEE_KEYS: dict = {}


def payee_for(agent_id: str = "", **wallets: str) -> dict:
    """A payee statement as a relay serves it with an offer (P262), signed by a
    fresh in-memory key: `payee_for(evm="0x…")`. A fresh name each call unless
    one is given, so a payer's pin (P244) never sees two keys under one name."""
    import uuid
    from datetime import UTC, datetime

    from cryptography.hazmat.primitives.asymmetric import ed25519

    from emporia import identity, wire
    priv = ed25519.Ed25519PrivateKey.generate()
    pub = priv.public_key().public_bytes_raw().hex()
    sid, at = wire.stable_id_for(pub), datetime.now(UTC).isoformat()
    agent_id = agent_id or f"payee_{uuid.uuid4().hex[:8]}"
    PAYEE_KEYS[agent_id] = priv          # so a test's fake relay can sign as this payee too
    return {"agent_id": agent_id, "stable_id": sid, "public_key_hex": pub,
            "key_log": [], "wallets": {
                f: {"address": a, "at": at,
                    "signature": identity.sign_payload(priv, wire.signed_payout_wallet_payload(sid, f, a, at))}
                for f, a in wallets.items()}}


def signed_join_body(session_id: str, agent_id: str, **extra) -> dict:
    """P272: a join body the agent signed, for tests with no shim (prod_client)."""
    from datetime import UTC, datetime

    from emporia import identity, wire
    at = datetime.now(UTC).isoformat()
    return {"agent_id": agent_id, "at": at,
            "signature": identity.sign(wire.signed_session_join_payload(session_id, agent_id, at), agent_id),
            **extra}


def signed_room_join_body(room_id: str, agent_id: str, **extra) -> dict:
    """P273: a room join body the agent signed, for tests with no shim."""
    from datetime import UTC, datetime

    from emporia import identity, wire
    at = datetime.now(UTC).isoformat()
    return {"agent_id": agent_id, "at": at,
            "signature": identity.sign(wire.signed_room_join_payload(room_id, agent_id, at), agent_id), **extra}


def _signed_creation(request, named, at):
    """P279: a game is made in its creator's name, signed; the shim signs one a test did not."""
    import json as _json

    from emporia import identity, wire
    try:
        body = _json.loads(request.read() or b"{}")
    except ValueError:
        return request
    if not isinstance(body, dict) or body.get("signature"):
        return request
    body["nonce"], body["at"] = wire.new_message_nonce(), at
    body["signature"] = identity.sign(wire.signed_session_create_payload(
        wire.session_id_for(body["nonce"]), named, str(body.get("module_type") or ""), at,
        wire.stake_terms(body.get("payment_rules"))), named)
    headers = {k: v for k, v in request.headers.items() if k.lower() != "content-length"}
    return type(request)(request.method, request.url, headers=headers, content=_json.dumps(body).encode(),
                         extensions=request.extensions)


def signed_session_body(creator: str, body: dict) -> dict:
    """P279: a `POST /sessions` body signed by its creator, for tests with no shim."""
    from datetime import UTC, datetime

    from emporia import identity, wire
    b = {**body, "nonce": wire.new_message_nonce(), "at": datetime.now(UTC).isoformat()}
    b["signature"] = identity.sign(wire.signed_session_create_payload(
        wire.session_id_for(b["nonce"]), creator, str(b.get("module_type") or ""), b["at"],
        wire.stake_terms(b.get("payment_rules"))), creator)
    return b


def signed_topic_body(creator: str, body: dict) -> dict:
    """P278: a `POST /agoras/topics` body signed by its creator, for tests with no shim."""
    from emporia import identity, wire
    b = {**body, "slug": wire.topic_slug(body.get("name", ""), body.get("slug")), "nonce": wire.new_message_nonce()}
    b["signature"] = identity.sign(wire.signed_agora_topic_payload(
        b["nonce"], b["slug"], b.get("name", ""), b.get("description", ""), b.get("visibility", "public"),
        b.get("gate_type", "open"), b.get("entry_fee_cents", 0), b.get("flair_options") or [],
        b.get("access_duration_days")), creator)
    return b


def signed_topic_act(act: str, slug: str, target: str, creator: str) -> dict:
    """P278: `{at, signature}` for a topic creator's invite, uninvite or kick, for tests with no shim."""
    from datetime import UTC, datetime

    from emporia import identity, wire
    at = datetime.now(UTC).isoformat()
    return {"at": at, "signature": identity.sign(wire.signed_topic_act_payload(act, slug, target, at), creator)}
