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
    "EMPORIA_TRUST_LOCAL_HEADER_AUTH": "1",

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
