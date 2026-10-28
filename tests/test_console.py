"""P126: the local console — signs in to the relay as a local agent with that
agent's own key, opens its sealed DMs, and answers only its owner."""
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from emporia import crypto_box, identity
from emporia.console.agents import discover
from emporia.console.app import COOKIE, Console, build_app
from emporia.relay_server import app as relay_app
from tests.test_emporia import _enc_pair


@pytest.fixture
def client():
    return TestClient(relay_app, client=("127.0.0.1", 51217))


def _console(session_token=None):
    transport = httpx.ASGITransport(app=relay_app, client=("203.0.113.9", 4000))
    return Console([identity.KEY_DIR], "http://relay.test", session_token=session_token,
                   transport=transport)


def test_an_agents_own_profile_keys_win_over_a_stale_copy(tmp_path):
    for profile, agents in (("alpha", ["alpha"]), ("hub", ["alpha", "hub"])):
        d = tmp_path / profile / "home" / ".hermes" / "keys"
        d.mkdir(parents=True)
        for a in agents:
            (d / f"{a}.priv").write_bytes(b"x" * 32)
        (d / "_relay_identity.priv").write_bytes(b"x" * 32)
    found = discover(sorted(tmp_path.glob("*/home/.hermes/keys"), reverse=True))
    assert sorted(found) == ["alpha", "hub"]
    assert found["alpha"].key_dir.parts[-4] == "alpha"


def test_the_console_signs_in_as_the_agent_with_its_own_key(client):
    """From a non-local address and without any agent header: only the agent's
    signed session can make the relay answer as that agent."""
    _enc_pair(client, "p126_a", "p126_b")
    c = TestClient(build_app(_console()))
    r = c.get("/relay/p126_a/agents/p126_a/inbox?unread_only=true",
              headers={"x-emporia-agent-id": "p126_b"})       # ignored, never forwarded
    assert r.status_code == 200, r.text
    assert r.json()["agent_id"] == "p126_a"
    assert r.headers["cache-control"] == "no-store"
    assert c.get("/relay/nobody_here/agents").status_code == 404
    assert "p126_a" in c.get("/agents").json()["agents"]


def test_the_console_opens_a_sealed_dm_for_its_recipient(client):
    _enc_pair(client, "p126_c", "p126_d")
    pubs = {a: identity.get_encryption_public_key_hex(a) for a in ("p126_c", "p126_d")}
    nonce = "a" * 32
    env = crypto_box.seal("for d only", pubs,
                          crypto_box.message_aad("dm", "dm_x", "p126_c", "chat", nonce))
    msgs = [{"message_id": "m1", "sender_id": "p126_c", "msg_type": "chat", "nonce": nonce,
             "content": json.dumps(env)}, {"message_id": "m2", "content": "plain"}]
    c = TestClient(build_app(_console()))
    r = c.post("/dm/p126_d/open", json={"thread_id": "dm_x", "messages": msgs})
    assert r.json() == {"opened": {"m1": "for d only"}}
    other = c.post("/dm/p126_d/open", json={"thread_id": "dm_other", "messages": msgs})
    assert other.json() == {"opened": {"m1": None}}


def test_standalone_answers_only_its_link_holder_on_localhost(client):
    _enc_pair(client, "p126_e", "p126_f")
    c = TestClient(build_app(_console(session_token="s" * 32)), base_url="http://127.0.0.1:8089")
    assert c.get("/agents").status_code == 401
    assert c.get("/session?key=wrong", follow_redirects=False).status_code == 401
    ok = c.get("/session?key=" + "s" * 32, follow_redirects=False)
    assert ok.status_code == 303 and COOKIE in ok.headers["set-cookie"]
    assert "httponly" in ok.headers["set-cookie"].lower()
    assert "samesite=strict" in ok.headers["set-cookie"].lower()
    assert c.get("/agents").status_code == 200
    rebound = TestClient(build_app(_console(session_token="s" * 32)),
                         base_url="http://attacker.example")
    rebound.cookies.set(COOKIE, "s" * 32)
    assert rebound.get("/agents").status_code == 401, "a non-local Host is refused"
    # P198: the link opens once
    assert c.get("/session?key=" + "s" * 32, follow_redirects=False).status_code == 401


def test_the_console_only_reads_as_the_agent(client):
    """P198: whatever the agent's keys could do, the dashboard only watches."""
    _enc_pair(client, "p198_a", "p198_b")
    c = TestClient(build_app(_console()))
    for method in ("post", "put", "patch", "delete"):
        r = getattr(c, method)("/relay/p198_a/listings") if method != "delete" else \
            c.delete("/relay/p198_a/admin/sessions/x")
        assert r.status_code == 405, (method, r.status_code)
    # P234: nor marking its own inbox read — read means the agent has had it
    assert c.post("/relay/p198_a/agents/p198_a/inbox/mark-read", json=[]).status_code == 405
    assert c.post("/relay/p198_a/agents/p198_a/inbox/mark-all-read").status_code == 405


def test_the_console_checks_a_game_itself_with_the_agents_own_receipts(client):
    """P146: the dashboard's verdict comes from this machine, not the relay."""
    import uuid

    from emporia import commit_reveal
    from tests.test_emporia import _register_keyed, _signed_action_body
    a, b = f"cv_{uuid.uuid4().hex[:6]}", f"cw_{uuid.uuid4().hex[:6]}"
    for x in (a, b):
        _register_keyed(client, x)
    sid = client.post("/sessions", json={"module_type": "emporia:blotto:v1", "creator_agent_id": a},
                      headers={"x-emporia-agent-id": a}).json()["session_id"]
    client.post(f"/sessions/{sid}/join", json={"agent_id": b}, headers={"x-emporia-agent-id": b})
    moves, nonces = {a: [30, 30, 30, 5, 5], b: [20] * 5}, {a: commit_reveal.new_nonce(), b: commit_reveal.new_nonce()}
    rdir = identity.KEY_DIR / "receipts"
    rdir.mkdir(exist_ok=True)
    for action in ("commit", "reveal"):
        for x in (a, b):
            payload = ({"commitment": commit_reveal.commitment(moves[x], nonces[x])} if action == "commit"
                       else {"move": moves[x], "nonce": nonces[x]})
            r = client.post(f"/sessions/{sid}/action",
                            json=_signed_action_body(client, sid, x, action, payload, "Reading this opponent"))
            if x == a:
                with (rdir / f"{a}.{sid}.jsonl").open("a") as f:
                    f.write(json.dumps(r.json()["receipt"]) + "\n")
    c = TestClient(build_app(_console()))
    report = c.get(f"/verify/{a}/{sid}").json()
    assert report["verdict"] == "ok", [f for f in report["findings"] if f["status"] != "ok"]
    assert sum(f["check"] == "receipt" and f["status"] == "ok" for f in report["findings"]) == 2
    assert c.get(f"/verify/{a}/sess_missing").status_code == 404


def test_the_console_opens_a_private_sessions_sealed_text(client):
    """P149: a sealed deliverable, opened for its player on this machine only."""
    import uuid
    from tests.test_emporia import _enc_pair
    a, b = f"co_{uuid.uuid4().hex[:6]}", f"cp_{uuid.uuid4().hex[:6]}"
    _enc_pair(client, a, b)
    env = json.dumps(crypto_box.seal(
        "the report", {x: identity.get_encryption_public_key_hex(x) for x in (a, b)},
        crypto_box.session_aad("sess_q", "deliverable")))
    c = TestClient(build_app(_console()))
    r = c.post(f"/session/{a}/sess_q/open", json={"items": [
        {"id": "1", "field": "deliverable", "content": env},
        {"id": "2", "field": "deliverable", "content": "plain"},
        {"id": "3", "field": "chat", "content": env}]})
    assert r.json() == {"opened": {"1": "the report", "3": None}}, "bound to its field"


def test_settings_are_read_only_and_hold_no_secret(tmp_path, monkeypatch):
    """P178: each local agent's settings, with the command that changes each."""
    from unittest.mock import patch as _patch

    from emporia import cli
    profile = tmp_path / "profiles" / "lambda"
    keys = profile / "home" / ".hermes" / "keys"
    keys.mkdir(parents=True)
    (profile / "config.yaml").write_text("mcp_servers:\n  emporia:\n    env:\n      EMPORIA_AGENT_ID: lambda\n"
                                         f"      EMPORIA_KEYS_DIR: {keys}\n")
    secret = "11" * 32
    (profile / ".env").write_text("EMPORIA_X402_PAY_NETWORKS=eip155:84532\nEMPORIA_X402_EVM_PRIVATE_KEY=0x" + secret
                                  + "\nEMPORIA_X402_EVM_KEY_SOURCE=env:EMPORIA_X402_EVM_PRIVATE_KEY\n")
    with _patch.object(identity, "KEY_DIR", keys):
        identity.generate_or_load_keypair("lambda")
    monkeypatch.setattr(cli, "health", lambda timeout=3.0, url="": None)
    c = TestClient(build_app(Console([keys], "http://relay.test", session_token=None)))
    r = c.get("/settings")
    assert r.status_code == 200 and secret not in r.text
    [agent] = r.json()["agents"]
    items = {i["label"]: i for i in agent["items"]}
    assert items["Mainnet (real money)"]["value"].startswith("locked")
    assert items["Mainnet (real money)"]["person_only"] is True
    assert items["EVM wallet"]["value"].startswith("0x") and "its .env" in items["EVM wallet"]["about"]
    assert items["Asks you above"]["level"] == "warn"
    assert items["Where it may pay"]["command"] == "emporia wallet limits lambda --networks base-sepolia,solana-devnet"
    assert c.post("/settings").status_code == 405, "nothing is changed from the browser"


def test_the_console_opens_an_encrypted_rooms_messages_for_a_member(client):
    """P238: rooms showed 🔒 only on the dashboard; DMs opened on this machine."""
    _enc_pair(client, "p238_a", "p238_b")
    aad = crypto_box.message_aad("room", "room_x", "p238_b", "chat", "n1")
    env = crypto_box.seal("hello room", {"p238_a": identity.get_encryption_public_key_hex("p238_a")}, aad)
    c = TestClient(build_app(_console()))
    r = c.post("/room/p238_a/open", json={"room_id": "room_x", "messages": [
        {"message_id": "m1", "sender_id": "p238_b", "msg_type": "chat", "nonce": "n1",
         "content": json.dumps(env)}]})
    assert r.status_code == 200 and r.json()["opened"] == {"m1": "hello room"}
    assert c.post("/room/p238_a/open", json={}).status_code == 400


def test_the_dashboards_console_exits_when_the_dashboard_does(tmp_path):
    """P239: a console that outlived its dashboard served day-old code."""
    import subprocess
    import sys
    import time
    from pathlib import Path
    if not Path("/proc/self/status").exists():
        pytest.skip("needs /proc")
    repo = Path(__file__).resolve().parents[1]
    marker = tmp_path / "child.pid"
    parent = subprocess.Popen([sys.executable, "-c", f"""
import os, subprocess, sys, time
child = subprocess.Popen([sys.executable, "-c",
    "import time\\nfrom emporia.console.__main__ import _exit_with_parent\\n_exit_with_parent(0.2)\\ntime.sleep(30)"],
    cwd={str(repo)!r}, start_new_session=True)
open({str(marker)!r}, "w").write(str(child.pid))
time.sleep(0.5)
"""], cwd=str(repo))
    parent.wait(timeout=20)
    pid = int(marker.read_text())
    status = Path(f"/proc/{pid}/status")
    for _ in range(50):
        # gone, or a zombie nobody has reaped yet (a container's init may not)
        if not status.exists() or "\nState:\tZ" in status.read_text():
            break
        time.sleep(0.1)
    else:
        raise AssertionError("the console kept running after its parent exited")
