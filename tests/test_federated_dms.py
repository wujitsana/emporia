"""P185: DMs between two live relays, linked both ways — sealed end to end,
with the recipient's consent, and readable by neither operator."""
import json
import os
import socket
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _clean_env() -> dict:
    return {k: v for k, v in os.environ.items() if not k.startswith(("EMPORIA_", "STRIPE_", "HERMES_"))}


@pytest.fixture
def two_relays(tmp_path):
    a, b = _free_port(), _free_port()
    url = {a: f"http://127.0.0.1:{a}", b: f"http://127.0.0.1:{b}"}
    procs = []
    for port, peer in ((a, b), (b, a)):
        d = tmp_path / f"r{port}"
        d.mkdir()
        env = {**_clean_env(), "EMPORIA_NO_DOTENV": "1", "EMPORIA_RELAY_PORT": str(port),
               "EMPORIA_RELAY_URL": url[port], "EMPORIA_DB_PATH": str(d / "r.sqlite3"),
               "EMPORIA_KEYS_DIR": str(d / "keys"), "EMPORIA_LOG_DIR": str(d / "logs"),
               "EMPORIA_AUDIT_DIR": str(d / "audit"), "FEDERATED_RELAYS": url[peer],
               "EMPORIA_FEDERATED_DMS": "1"}
        procs.append(subprocess.Popen([sys.executable, "-m", "emporia.relay_server"], env=env,
                                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
    try:
        for port in (a, b):
            for _ in range(80):
                try:
                    if httpx.get(url[port] + "/health", timeout=1).status_code == 200:
                        break
                except httpx.HTTPError:
                    time.sleep(0.25)
            else:
                pytest.fail("relay did not start")
        yield tmp_path, url[a], url[b]
    finally:
        for p in procs:
            p.terminate()
            p.wait(timeout=10)


def _as(tmp: Path, name: str, relay: str, code: str):
    env = {**_clean_env(), "EMPORIA_NO_DOTENV": "1", "EMPORIA_RELAY_URL": relay, "EMPORIA_AGENT_ID": name,
           "EMPORIA_KEYS_DIR": str(tmp / f"keys_{name}")}
    out = subprocess.run([sys.executable, "-c", "import json\nfrom emporia import mcp_server as m\n" + code
                          + "\nprint(json.dumps(r))"], env=env, capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr[-1500:]
    return json.loads(out.stdout.strip().splitlines()[-1])


def _read(tmp, name, relay):
    return _as(tmp, name, relay, "t = m.list_dm_threads()[0]['thread_id']\nr = m.get_dm_messages(t)['messages']")


def test_a_dm_crosses_relays_sealed_and_only_with_consent(two_relays):
    tmp, a, b = two_relays
    ha, hb = a.split("//")[1], b.split("//")[1]
    for name, relay in (("alice", a), ("carol", a), ("bob", b)):
        assert _as(tmp, name, relay, f"r = m.register_agent({name!r})")["success"]
    # bob has not opted in yet: refused, and the same answer as no such agent
    r = _as(tmp, "alice", a, f"r = m.send_dm('bob@{hb}', 'too early')")
    assert not r["success"] and "404" in r["error"]
    assert _as(tmp, "bob", b, "r = m.set_dm_policy(True)")["success"]
    assert _as(tmp, "alice", a, f"r = m.send_dm('bob@{hb}', 'hello bob')")["success"]
    got = _read(tmp, "bob", b)
    assert [(x["sender_id"], x["content"], x["verified"], x["decrypted"]) for x in got] == \
        [(f"alice@{ha}", "hello bob", True, True)]
    # a reply needs no opt-in from alice: she started the thread
    assert _as(tmp, "bob", b, f"r = m.send_dm('alice@{ha}', 'hi alice')")["success"]
    assert [x["content"] for x in _read(tmp, "alice", a)] == ["hello bob", "hi alice"]
    # carol never opted in, and never wrote to bob
    assert not _as(tmp, "bob", b, f"r = m.send_dm('carol@{ha}', 'hey')")["success"]
    # neither relay holds anything but sealed envelopes
    for relay in (a, b):
        port = relay.rsplit(":", 1)[1]
        rows = sqlite3.connect(tmp / f"r{port}" / "r.sqlite3").execute("SELECT content FROM dm_messages").fetchall()
        assert rows and all(json.loads(c)["alg"].startswith("x25519") for (c,) in rows)
        assert not any("hello" in c or "hi alice" in c for (c,) in rows)


def test_dms_keep_flowing_after_a_relay_rotates_its_key(tmp_path):
    """P191: B rotates its key and restarts; A follows B's signed hand-over."""
    from tests.test_moving import _start
    pa, pb = _free_port(), _free_port()
    a, b = f"http://127.0.0.1:{pa}", f"http://127.0.0.1:{pb}"
    dms = {"EMPORIA_FEDERATED_DMS": "1"}
    relay_a = _start(tmp_path, pa, {**dms, "FEDERATED_RELAYS": b})
    relay_b = _start(tmp_path, pb, {**dms, "FEDERATED_RELAYS": a})
    try:
        hb = b.split("//")[1]
        for name, relay in (("alice", a), ("bob", b)):
            assert _as(tmp_path, name, relay, f"r = m.register_agent({name!r})")["success"]
        assert _as(tmp_path, "bob", b, "r = m.set_dm_policy(True)")["success"]
        assert _as(tmp_path, "alice", a, f"r = m.send_dm('bob@{hb}', 'before')")["success"]
        old_key = httpx.get(f"{b}/health").json()["relay_public_key"]
        relay_b.terminate()
        relay_b.wait(timeout=10)
        rotated = subprocess.run(
            [sys.executable, "-c", "from emporia import identity; print(identity.rotate_relay_key()['new'])"],
            env={**_clean_env(), "EMPORIA_NO_DOTENV": "1", "EMPORIA_KEYS_DIR": str(tmp_path / f"r{pb}" / "keys")},
            capture_output=True, text=True, timeout=30)
        assert rotated.returncode == 0, rotated.stderr
        relay_b = _start_again(tmp_path, pb, {**dms, "FEDERATED_RELAYS": a})
        assert httpx.get(f"{b}/health").json()["relay_public_key"] == rotated.stdout.strip() != old_key
        after = _as(tmp_path, "bob", b, f"r = m.send_dm('alice@{a.split('//')[1]}', 'after')")
        assert after["success"], after
        still = _as(tmp_path, "alice", a, f"r = m.send_dm('bob@{hb}', 'still')")
        assert still["success"], still
    finally:
        for p in (relay_a, relay_b):
            if p.poll() is None:
                p.terminate()
                p.wait(timeout=10)


def _start_again(tmp, port, extra):
    """Restart a relay on its existing folder (database and keys kept)."""
    d = tmp / f"r{port}"
    env = {**_clean_env(), "EMPORIA_NO_DOTENV": "1", "EMPORIA_RELAY_PORT": str(port),
           "EMPORIA_RELAY_URL": f"http://127.0.0.1:{port}", "EMPORIA_DB_PATH": str(d / "r.sqlite3"),
           "EMPORIA_KEYS_DIR": str(d / "keys"), "EMPORIA_LOG_DIR": str(d / "logs"),
           "EMPORIA_AUDIT_DIR": str(d / "audit"), **extra}
    p = subprocess.Popen([sys.executable, "-m", "emporia.relay_server"], env=env,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(80):
        try:
            if httpx.get(f"http://127.0.0.1:{port}/health", timeout=1).status_code == 200:
                return p
        except httpx.HTTPError:
            time.sleep(0.25)
    p.terminate()
    pytest.fail("relay did not restart")
