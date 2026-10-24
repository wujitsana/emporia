"""P144: checking a game without trusting the relay — and catching one that cheats."""
import copy
import hashlib
import json
import uuid

import pytest
from fastapi.testclient import TestClient

from emporia import commit_reveal
from emporia.relay_server import app
from emporia.verify import verify_game
from tests.test_emporia import _register_keyed, _signed_action_body


@pytest.fixture(scope="module")
def game():
    """One honest Blotto game: the record an agent would fetch, and its receipts."""
    client = TestClient(app, client=("127.0.0.1", 51224))
    a, b = f"va_{uuid.uuid4().hex[:6]}", f"vb_{uuid.uuid4().hex[:6]}"
    for x in (a, b):
        _register_keyed(client, x)
    sid = client.post("/sessions", json={"module_type": "emporia:blotto:v1", "creator_agent_id": a},
                      headers={"x-emporia-agent-id": a}).json()["session_id"]
    client.post(f"/sessions/{sid}/join", json={"agent_id": b}, headers={"x-emporia-agent-id": b})
    moves = {a: [30, 30, 30, 5, 5], b: [20] * 5}
    nonces = {x: commit_reveal.new_nonce() for x in (a, b)}
    receipts = []
    for action in ("commit", "reveal"):
        for x in (a, b):
            payload = ({"commitment": commit_reveal.commitment(moves[x], nonces[x])} if action == "commit"
                       else {"move": moves[x], "nonce": nonces[x]})
            r = client.post(f"/sessions/{sid}/action",
                            json=_signed_action_body(client, sid, x, action, payload, "Reading this opponent"))
            assert r.status_code == 200, r.text
            if x == a:
                receipts.append(r.json()["receipt"])
    hdr = {"x-emporia-agent-id": a}
    record = {
        "session": client.get(f"/sessions/{sid}", headers=hdr).json(),
        "log": client.get(f"/sessions/{sid}/audit", headers=hdr).json()["chain"],
        "public_keys": {x: client.get(f"/agents/{x}", headers=hdr).json()["public_key_hex"] for x in (a, b)},
        "receipts": receipts,
        "relay_key": client.get("/health").json()["relay_public_key"],
        "relay_verdict": next(r for r in client.get("/results", params={"agent_id": a}).json()["results"]
                              if r["session_id"] == sid),
    }
    return record, a, b


def _check(record):
    return verify_game(**record)


def _rechain(log):
    """What a careful rogue relay does after editing an entry: recompute every
    hash so the chain itself still verifies."""
    prev = "GENESIS"
    for e in log:
        if e.get("genesis"):
            continue
        frame = f"{prev}:{e['sender']}:{e['action']}:{json.dumps(e['payload'], sort_keys=True)}:{e['signature']}"
        e["parent_block_hash"], e["block_hash"] = prev, hashlib.sha256(frame.encode()).hexdigest()
        prev = e["block_hash"]
    return log


def test_an_honest_game_checks_out(game):
    record, a, _ = game
    report = _check(record)
    assert report["verdict"] == "ok", [f for f in report["findings"] if f["status"] != "ok"]
    checks = {f["check"] for f in report["findings"] if f["status"] == "ok"}
    assert {"log_chain", "move_signature", "receipt", "verdict"} <= checks


def test_a_dropped_move_is_caught_by_the_receipt(game):
    """Dropping the last entry leaves a valid chain; only the receipt shows it."""
    record, a, _ = game
    r = copy.deepcopy(record)
    r["log"] = [e for e in r["log"] if not (e.get("sender") == a and e.get("action") == "reveal")]
    report = _check(r)
    assert report["verdict"] == "fail"
    assert any("does not contain it" in f["detail"] for f in report["findings"])


def test_a_rewritten_move_is_caught_by_its_signature(game):
    record, a, _ = game
    r = copy.deepcopy(record)
    for e in r["log"]:
        if e.get("sender") == a and e.get("action") == "reveal":
            e["payload"]["move"] = [100, 0, 0, 0, 0]
    _rechain(r["log"])
    report = _check(r)
    assert any(f["check"] == "log_chain" and f["status"] == "ok" for f in report["findings"]), \
        "the rogue relay recomputed the chain"
    assert any(f["check"] == "move_signature" and f["status"] == "fail" for f in report["findings"])
    assert report["verdict"] == "fail"


def test_a_wrong_winner_is_caught_by_replaying_the_rules(game):
    record, a, b = game
    r = copy.deepcopy(record)
    r["relay_verdict"]["winner"] = b
    report = _check(r)
    [v] = [f for f in report["findings"] if f["check"] == "verdict"]
    assert v["status"] == "fail" and a in v["detail"] and b in v["detail"]


def test_a_receipt_from_another_key_is_not_trusted(game):
    record, _, _ = game
    r = copy.deepcopy(record)
    r["relay_key"] = "00" * 32
    assert _check(r)["verdict"] == "fail"


def test_an_agents_own_process_can_replay_the_rules():
    """P150: found in rehearsal — only the relay imported the bundled games, so
    from an agent's MCP process every verdict was "unchecked"."""
    import subprocess
    import sys
    code = ("from emporia import verify, module_sdk; "
            "assert 'emporia:blotto:v1' not in module_sdk.MODULE_REGISTRY; "
            "module_sdk.load_bundled_modules(); "
            "assert 'emporia:blotto:v1' in module_sdk.MODULE_REGISTRY; print('ok')")
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60)
    assert r.stdout.strip() == "ok", r.stderr[-800:]
