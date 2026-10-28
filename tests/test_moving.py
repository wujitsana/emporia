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


def test_an_owner_without_hermes_gets_a_key_and_watches_as_operator(tmp_path, capsys, monkeypatch):
    """P199: a bare key, registered on the relay, named its operator — then the
    relay's operator view answers to it."""
    from emporia import cli
    port = _free_port()
    relay_url = f"http://127.0.0.1:{port}"
    relay = _start(tmp_path, port, {"EMPORIA_AGENT_ID": "owner_p199"})
    try:
        keys = tmp_path / "owner-keys"
        assert cli.main(["relay", "owner-key", relay_url, "owner_p199", "--keys-dir", str(keys)]) == 0
        out = capsys.readouterr().out
        assert "EMPORIA_AGENT_ID owner_p199" in out and "python -m emporia.console" in out
        assert any(keys.glob("*.priv"))
        overview = _as(tmp_path, "owner_p199", relay_url,
                       "r = m._bearer_request('get', m.RELAY_URL + '/operator/overview', agent_id='owner_p199').status_code",
                       EMPORIA_KEYS_DIR=str(keys))
        assert overview == 200
    finally:
        relay.terminate()
        relay.wait(timeout=10)


def test_the_list_a_move_tells_the_old_relay_carries_the_backups_the_agent_signed():
    """Found by the live suite (P248): the body left `backups` out, so an agent with a backup relay could not
    tell its old relay it moved - the signature covers the backups and the old relay refused (403)."""
    from fastapi.testclient import TestClient

    from emporia import identity, mcp_server, wire
    from emporia.relay_server import app
    from tests.test_emporia import _register_keyed
    client = TestClient(app, client=("127.0.0.1", 51299))
    _register_keyed(client, "mv_backups")
    stamp = "2026-10-28T12:00:00+00:00"
    published = {"home": "https://new-home.example", "visits": [], "backups": ["https://backup.example"],
                 "a2a_url": "", "updated_at": stamp}
    payload = wire.signed_relay_list_payload("mv_backups", published["home"], [], "", stamp, published["backups"])
    r = client.post("/agents/mv_backups/relay-list", headers={"x-emporia-agent-id": "mv_backups"},
                    json={**published, "signature": identity.sign(payload, "mv_backups")})
    assert r.status_code == 200, r.text
    card = client.get("/agents/mv_backups/card").json()["card"]
    body = mcp_server._relay_list_body(card)
    assert body["backups"] == ["https://backup.example"] and body["signature"] == card["relay_list_signature"]
    again = wire.signed_relay_list_payload("mv_backups", body["home"], body["visits"], body["a2a_url"],
                                           body["updated_at"], body["backups"])
    assert identity.verify(again, body["signature"], identity.get_public_key_hex("mv_backups"))


async def test_a_dm_to_an_agent_that_moved_is_told_where_it_went():
    """Found by the live suite (P248): the sender's relay turned the old relay's "moved to" (410) into a bare
    502, so the sender never learned the new address."""
    from unittest.mock import AsyncMock, patch

    from fastapi import HTTPException

    from emporia import federation_dm as fd
    said = httpx.Response(410, json={"detail": {"moved_to": "bob@new.example", "message": "bob moved"}})
    with patch.object(fd, "call_peer", AsyncMock(return_value=said)), pytest.raises(HTTPException) as moved:
        await fd.look_up("https://old.example", "bob")
    assert moved.value.status_code == 410 and moved.value.detail["moved_to"] == "bob@new.example"
    assert "bob@new.example" in moved.value.detail["message"]
    odd = httpx.Response(410, text="gone")          # a relay that does not say where
    with patch.object(fd, "call_peer", AsyncMock(return_value=odd)), pytest.raises(HTTPException) as gone:
        await fd.look_up("https://old.example", "bob")
    assert gone.value.status_code == 410 and gone.value.detail["moved_to"] == ""
