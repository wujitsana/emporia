"""One deterministic environment for the whole suite, pinned before emporia loads (P60).

`test_emporia.py` already pinned five permissive flags at module scope, with a
comment noting "profile .env may have stricter settings". That was right about
the flags and stopped short of the rest. Measured: on a developer machine the
profile `.env` injects **ten** further variables into every test run, three of
them live credentials —

    STRIPE_SECRET_KEY   NVIDIA_API_KEY   EMPORIA_NOUS_JWT
    OPENROUTER_API_KEY  STRIPE_PROFILE_ID   STRIPE_API_VERSION
    EMPORIA_AGENT_ID    EMPORIA_RELAY_URL
    EMPORIA_GUARDRAILS_MODE   EMPORIA_NEMO_GUARDRAILS_ENABLED

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
    "NVIDIA_API_KEY": "",
    "OPENROUTER_API_KEY": "",
    "EMPORIA_NOUS_JWT": "",

    # Behaviour switches the profile would otherwise choose for us. NeMo stays
    # off (the code default): it is the one guardrail layer that makes a network
    # call, and a suite that silently enables it is a suite that silently sends
    # test payloads to a third party.
    "EMPORIA_NEMO_GUARDRAILS_ENABLED": "0",
    "EMPORIA_GUARDRAILS_MODE": "enforce",
    "EMPORIA_AGENT_ID": "",
    "EMPORIA_RELAY_URL": "http://localhost:8088",
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
