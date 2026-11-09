"""Open game records: `GET /games/export`, the offline verifier and its format (docs/protocol.md).

The good export is built here from a REAL recorded drand round (round 1000, test_rand.py), signed with test keys:
the same checks the CLI runs pass on it, and fail on every kind of tampering. No network."""
import copy
import hashlib
import json
import subprocess
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from emporia import game_export, identity, rand, wire
from emporia import relay_server as rs
from emporia.module_sdk import SessionAction, SessionState
from emporia.modules import liars_dice as LD
from emporia.relay_server import app
from tests.test_games_pack import DICE, _act, _first_bid, _game, _order, _reveal

ROOT = Path(__file__).resolve().parent.parent
R1000 = "b44679b9a59af2ec876b1a6b1ad52ea9b1615fc3982b19576350f93447cb1125e342b73a8dd2bacbe47e4b6b63ed5e39"
WHY = "Reading the table and what the bids imply"


@pytest.fixture
def client():
    return TestClient(app, client=("127.0.0.1", 51233))


def _stable(agent):
    from tests.ids import stable_of
    return stable_of(agent)


def _entry(prev, sid, agent, step, action, payload, say=""):
    sig = identity.sign(wire.signed_action_payload(_stable(agent), action, payload, WHY, sid, step, say=say), agent)
    frame = f"{prev}:{agent}:{action}:{json.dumps(payload, sort_keys=True)}:{sig}"
    return {"sender": agent, "stable_id": _stable(agent), "action": action, "payload": payload, "rationale": WHY,
            "step_number": step,
            "say": say, "timestamp": 1700000000 + step, "signature": sig,
            "block_hash": hashlib.sha256(frame.encode()).hexdigest()}


def good_export() -> dict:
    """A finished 3-player Liar's dice game on drand round 1000, with signed commitments and moves."""
    sid = f"sess_fixture{uuid.uuid4().hex[:6]}"
    agents = [f"fx{i}_{uuid.uuid4().hex[:6]}" for i in range(3)]
    keys = {a: identity.get_public_key_hex(a) for a in agents}
    at = datetime.fromtimestamp(rand.round_time(900), UTC).isoformat()
    secrets, commits = {}, {}
    for seat, a in enumerate(agents):
        s, nonce = rand.new_secret()
        h = rand.commit_hash(bytes.fromhex(s), bytes.fromhex(nonce))
        secrets[a] = (s, nonce)
        commits[a] = {"seat": seat, "stable_id": _stable(a), "hash": h, "beacon_round": 1000, "at": at,
                      "signature": identity.sign(wire.signed_rand_commit_payload(sid, _stable(a), seat, h, 1000, at), a)}
    mod = LD.LiarsDiceModule()
    state = mod.initial_state(agents, {"players": 3, "dice": 2, "rand": {"scope": sid, "commits": commits}})
    data = {**state.data, "rand": {**state.data["rand"], "beacon_sig": R1000}}
    state = SessionState(data=data, current_agent=agents[0], step_number=0, metadata=state.metadata)
    order = LD.turn_order(sid, bytes.fromhex(R1000), agents)
    moves, prev, step = [{"session_id": sid, "genesis": True, "block_hash": "GENESIS"}], "GENESIS", 0
    plan = [(order[0], "bid", {"quantity": 2, "face": 3}), (order[1], "liar", {})] + \
           [(a, "reveal", {"s": secrets[a][0], "nonce": secrets[a][1]}) for a in agents]
    for who, action, payload in plan:
        assert mod.validate_action(state, SessionAction(who, action, payload)) == (True, "")
        state = mod.apply_action(state, SessionAction(who, action, payload)).new_state
        e = _entry(prev, sid, who, step, action, payload, say="two threes, surely" if action == "bid" else "")
        moves.append(e)
        prev, step = e["block_hash"], step + 1
    over, result = mod.is_terminal(state)
    assert over
    seats = [{"seat": i, "agent_id": a, "stable_id": wire.stable_id_for(keys[a]), "public_key_hex": keys[a],
              "key_log": [], "operator_distinct": True} for i, a in enumerate(agents)]
    body = game_export.referee_body(sid, DICE, result)
    return {"format": game_export.FORMAT, "session_id": sid, "module": DICE, "config": state.data and
            {"players": 3, "dice": 2, "chat": True}, "status": "completed", "created_at": at, "ended_at": at,
            "seats": seats, "moves": moves, "table_talk": {"untrusted": True, "note": game_export.UNTRUSTED,
                                                           "messages": []},
            "rand": state.data["rand"], "final_state": state.to_dict(), "result": result,
            "referee": {"body": body, "relay_public_key": identity.get_relay_public_key_hex(),
                        "signature": identity.sign_raw_relay(game_export.referee_bytes(body))}}


def _status(report, check):
    return [f["status"] for f in report["findings"] if f["check"] == check]


def test_a_good_export_verifies_every_check():
    line = good_export()
    report = game_export.verify_line(line, relay_key=line["referee"]["relay_public_key"])
    assert report["verdict"] == "ok", [f for f in report["findings"] if f["status"] != "ok"]
    for check in ("log_chain", "move_signature", "rand", "beacon_bls", "result", "referee"):
        assert "ok" in _status(report, check), check
    assert not any(f["status"] == "unchecked" for f in report["findings"])


def _tampered():
    """(what it changes, the check that must fail, the changed line) for each kind of tampering."""
    base = good_export()

    def t(label, check, fn):
        line = copy.deepcopy(base)
        fn(line)
        return label, check, line

    def flip_result(line):
        line["result"]["winner"] = next(a for a in (s["agent_id"] for s in line["seats"]) if a != line["result"]["winner"])

    def drop_move(line):
        del line["moves"][1]

    def edit_payload(line):
        line["moves"][1]["payload"]["quantity"] = 1

    def forge_move_sig(line):
        line["moves"][1]["signature"] = line["moves"][2]["signature"]

    def other_beacon(line):
        line["rand"]["beacon_sig"] = line["rand"]["beacon_sig"][:-2] + "00"
        line["final_state"]["data"]["rand"]["beacon_sig"] = line["rand"]["beacon_sig"]

    def swap_secret(line):
        a = line["seats"][0]["agent_id"]
        line["final_state"]["data"]["rand"]["reveals"][a]["s"] = "ab" * 32
        line["rand"] = line["final_state"]["data"]["rand"]

    def forge_commit(line):
        a = line["seats"][1]["agent_id"]
        line["final_state"]["data"]["rand"]["commits"][a]["hash"] = "cd" * 32
        line["rand"] = line["final_state"]["data"]["rand"]

    def fake_dice(line):
        a = line["seats"][0]["agent_id"]
        line["result"]["values"][a] = [6, 6]
        line["referee"]["body"] = game_export.referee_body(line["session_id"], line["module"], line["result"])

    def resign_with_other_key(line):
        line["referee"]["relay_public_key"] = identity.get_public_key_hex(f"nobody_{uuid.uuid4().hex[:6]}")

    return [
        t("a winner flipped", "referee", flip_result),
        t("a move dropped", "log_chain", drop_move),
        t("a payload edited", "log_chain", edit_payload),
        t("a move signature swapped", "move_signature", forge_move_sig),
        t("another beacon signature", "beacon_bls", other_beacon),
        t("a revealed secret swapped", "rand", swap_secret),
        t("a commitment replaced", "rand", forge_commit),
        t("dice invented, referee re-hashed", "rand", fake_dice),
        t("the referee key changed", "referee", resign_with_other_key),
    ]


@pytest.mark.parametrize("i", range(9))
def test_every_kind_of_tampering_is_caught(i):
    label, check, line = _tampered()[i]
    report = game_export.verify_line(line)
    assert report["verdict"] == "fail", label
    assert "fail" in _status(report, check), (label, [f for f in report["findings"] if f["status"] == "fail"])


def test_a_relay_key_that_is_not_the_pinned_one_fails_the_referee_check():
    line = good_export()
    other = identity.get_public_key_hex(f"pin_{uuid.uuid4().hex[:6]}")
    assert game_export.verify_line(line, relay_key=other)["verdict"] == "fail"
    assert "not pinned" in json.dumps(game_export.verify_line(line)["findings"])


def test_the_cli_passes_a_good_file_and_fails_a_tampered_one(tmp_path):
    good, bad = tmp_path / "good.jsonl", tmp_path / "bad.jsonl"
    lines = [good_export(), good_export()]
    good.write_text("".join(json.dumps(x) + "\n" for x in lines))
    broken = copy.deepcopy(lines[1])
    broken["moves"][1]["payload"]["face"] = 6
    bad.write_text(json.dumps(lines[0]) + "\n" + json.dumps(broken) + "\n")
    run = [sys.executable, "-I", str(ROOT / "scripts" / "verify_export.py")]
    ok = subprocess.run([*run, str(good)], capture_output=True, text=True, timeout=240)
    assert ok.returncode == 0 and "2 game(s) checked, 0 failed" in ok.stdout, ok.stdout + ok.stderr
    no = subprocess.run([*run, str(bad)], capture_output=True, text=True, timeout=240)
    assert no.returncode == 1 and "1 failed" in no.stdout and "log_chain" in no.stdout, no.stdout + no.stderr
    assert subprocess.run([*run, str(tmp_path / "none")], capture_output=True, text=True).returncode == 2


# ── the route ────────────────────────────────────────────────────────────────

def _finished(client, n=2):
    sid, agents = _game(client, DICE, n)
    order = _order(sid)
    _act(client, sid, order[0], "bid", {"quantity": 1, "face": 2}, say="trust me")
    _act(client, sid, order[1], "liar", say="I do not")
    for a in agents:
        assert _reveal(client, sid, a).status_code == 200
    assert rs.load_session(sid)["status"] == "completed"
    return sid, agents


def test_the_export_lists_finished_public_games_with_what_a_verifier_needs(client, monkeypatch):
    sid, agents = _finished(client)
    r = client.get("/games/export", params={"module": DICE, "limit": 200})
    assert r.status_code == 200 and r.headers["content-type"].startswith("application/x-ndjson")
    lines = [json.loads(x) for x in r.text.splitlines()]
    line = next(x for x in lines if x["session_id"] == sid)
    assert line["format"] == game_export.FORMAT and line["module"] == DICE
    assert [s["agent_id"] for s in line["seats"]] == agents and all("operator_distinct" in s for s in line["seats"])
    assert all(s["public_key_hex"] and s["stable_id"] for s in line["seats"])
    assert any(m.get("action") == "liar" for m in line["moves"])
    assert line["table_talk"]["untrusted"] is True and [m["content"] for m in line["table_talk"]["messages"]] \
        == ["trust me", "I do not"] and all(m["untrusted"] for m in line["table_talk"]["messages"])
    assert line["rand"]["beacon_sig"] and set(line["rand"]["reveals"]) == set(agents)
    assert line["result"]["termination"] in ("bid_held", "bid_failed")
    assert identity.verify_raw(game_export.referee_bytes(line["referee"]["body"]), line["referee"]["signature"],
                               line["referee"]["relay_public_key"])
    assert "fail" in _status(game_export.verify_line(line), "beacon_bls"), "the test beacon is no drand signature"
    from emporia import beacon
    monkeypatch.setattr(beacon, "verify", lambda rnd, sig: True)      # the BLS check has its own tests (test_rand.py)
    report = game_export.verify_line(line)
    assert _status(report, "referee") == ["ok"] and _status(report, "log_chain") == ["ok"]
    assert _status(report, "move_signature").count("ok") == len(line["moves"]) - 1
    assert "ok" in _status(report, "rand"), "commitments, reveals and recomputed dice check out (test rules)"


def test_private_unfinished_and_other_games_are_never_exported(client, monkeypatch):
    public, _ = _finished(client)
    private, _ = _finished(client)
    live, agents = _game(client, DICE, 3)
    _first_bid(client, live)
    real = rs._session_visibility
    monkeypatch.setattr(rs, "_session_visibility",
                        lambda s: "participants" if s["session_id"] == private else real(s))
    out = [json.loads(x) for x in client.get("/games/export", params={"limit": 200}).text.splitlines()]
    ids = {x["session_id"] for x in out}
    assert public in ids and private not in ids and live not in ids
    sealed = rs.session_sealed
    monkeypatch.setattr(rs, "session_sealed", lambda s: s["session_id"] == public or sealed(s))
    ids = {json.loads(x)["session_id"] for x in client.get("/games/export", params={"limit": 200}).text.splitlines()}
    assert public not in ids, "a sealed game is never included"


def test_since_and_limit_page_through_the_export(client):
    a, _ = _finished(client)
    b, _ = _finished(client)
    r = client.get("/games/export", params={"module": DICE, "limit": 1, "since": "2000-01-01"})
    first = json.loads(r.text.splitlines()[0])["session_id"]
    nxt = r.headers["x-next-since"]
    assert r.headers["x-export-count"] == "1" and nxt
    rest = [json.loads(x)["session_id"] for x in client.get("/games/export", params={"module": DICE, "since": nxt,
                                                                                    "limit": 200}).text.splitlines()]
    assert first not in rest
    assert a not in rest or b in rest


def test_the_mcp_tool_and_sdk_method_exist_and_are_marked_untrusted():
    from emporia import mcp_surface
    from emporia.agent_sdk import EmporiaAgent
    assert "records" in mcp_surface.UNTRUSTED_TEXT          # the MCP tool: records(kind="export")
    assert callable(getattr(EmporiaAgent, "export_games", None))
