"""P190: an agent moves home — even after its old relay is gone — and is found
again through the backup relay its own signed list named."""
import json
import subprocess
import sys
import time

import httpx
import pytest

from tests.test_federated_dms import _clean_env, _free_port


def _start(tmp, port, extra=None):
    d = tmp / f"r{port}"
    d.mkdir()
    env = {**_clean_env(), "EMPORIA_NO_DOTENV": "1", "EMPORIA_RELAY_PORT": str(port),
           "EMPORIA_RELAY_URL": f"http://127.0.0.1:{port}", "EMPORIA_DB_PATH": str(d / "r.sqlite3"),
           "EMPORIA_KEYS_DIR": str(d / "keys"), "EMPORIA_LOG_DIR": str(d / "logs"),
           "EMPORIA_AUDIT_DIR": str(d / "audit"), **(extra or {})}
    p = subprocess.Popen([sys.executable, "-m", "emporia.relay_server"], env=env,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(80):
        try:
            if httpx.get(f"http://127.0.0.1:{port}/health", timeout=1).status_code == 200:
                return p
        except httpx.HTTPError:
            time.sleep(0.25)
    p.terminate()
    pytest.fail("relay did not start")


def _as(tmp, name, relay, code, **env_over):
    env = {**_clean_env(), "EMPORIA_NO_DOTENV": "1", "EMPORIA_RELAY_URL": relay, "EMPORIA_AGENT_ID": name,
           "EMPORIA_KEYS_DIR": str(tmp / f"keys_{name}"), **env_over}
    out = subprocess.run([sys.executable, "-c", "import json\nfrom emporia import mcp_server as m\n" + code
                          + "\nprint(json.dumps(r))"], env=env, capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr[-1500:]
    return json.loads(out.stdout.strip().splitlines()[-1])


def test_moving_home_after_the_old_relay_is_gone(tmp_path):
    pa, pb = _free_port(), _free_port()
    a, b = f"http://127.0.0.1:{pa}", f"http://127.0.0.1:{pb}"
    relay_a, relay_b = _start(tmp_path, pa), _start(tmp_path, pb)
    try:
        # alice lives on A, with B as her backup
        assert _as(tmp_path, "alice", a, "r = m.register_agent('alice')", EMPORIA_BACKUP_RELAYS=b)["success"]
        assert _as(tmp_path, "alice", a, f"t = m._RELAY_CTX.set({b!r})\nr = m.register_agent('alice')",
                   EMPORIA_BACKUP_RELAYS=b)["success"]
        assert (tmp_path / "keys_alice" / "records" / f"127.0.0.1_{pa}.json").exists()   # its record, kept
        # bob, on B, looks her up once
        assert _as(tmp_path, "bob", b, "r = m.register_agent('bob')")["success"]
        first = _as(tmp_path, "bob", b, f"r = m.agent_card('alice@127.0.0.1:{pa}')")
        assert first["verified"] and first["card"]["relay_list"]["backups"] == [b]

        relay_a.terminate()
        relay_a.wait(timeout=10)                                     # A is gone, unannounced

        found = _as(tmp_path, "bob", b, f"r = m.agent_card('alice@127.0.0.1:{pa}')")
        assert found["success"] and found["found_on"] == b and found["card"]["relay_list"]["home"] == a

        moved = _as(tmp_path, "alice", b, f"r = m.move_home({a!r})", EMPORIA_BACKUP_RELAYS=b)
        assert moved["success"] and moved["old_relay"] == "not answering", moved
        assert moved["record_carried"] is True and "copy kept" in moved["record_from"]

        after = _as(tmp_path, "bob", b, f"r = m.agent_card('alice@127.0.0.1:{pa}')")
        assert after["card"]["relay_list"]["home"] == b               # signed by alice; A had no say
        record = _as(tmp_path, "bob", b, "r = m.counterparty_record('alice')")
        assert record["verified"] and record["elsewhere"][0]["relay"] == a
    finally:
        for p in (relay_a, relay_b):
            if p.poll() is None:
                p.terminate()
                p.wait(timeout=10)
