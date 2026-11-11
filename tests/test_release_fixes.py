"""Small pre-release fixes: the MCP server's stderr, the stale 'not available yet' list, and the like."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from emporia import features, mcp_server, setup_choices

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("transport", ["stdio", "http", "sse"])
def test_the_mcp_server_starts_without_a_banner_or_an_update_box(monkeypatch, transport):
    calls = []
    monkeypatch.setattr(mcp_server, "_auto_register", lambda: None)
    monkeypatch.setattr(mcp_server, "_fit_tools_to_relays", lambda: None)
    monkeypatch.setattr(mcp_server.mcp, "run", lambda **kw: calls.append(kw))
    monkeypatch.setenv("EMPORIA_MCP_TRANSPORT", transport)
    import fastmcp
    held = fastmcp.settings.check_for_updates
    try:
        mcp_server.main()
        assert fastmcp.settings.check_for_updates == "off"
    finally:
        fastmcp.settings.check_for_updates = held
    assert len(calls) == 1 and calls[0]["show_banner"] is False


# ── the "Verify it yourself" commands ───────────────────────────────────────────────────────

def _beacon_script():
    import importlib.util
    spec = importlib.util.spec_from_file_location("emporia_verify_beacon", ROOT / "scripts" / "verify_beacon.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _game_on_round(round_no: int, sig):
    from datetime import UTC, datetime

    from emporia import rand
    at = datetime.fromtimestamp(rand.round_time(round_no - 20), UTC).isoformat()
    commits = {"a": {"seat": 0, "hash": "0" * 64, "beacon_round": round_no - 1, "at": at},
               "b": {"seat": 1, "hash": "1" * 64, "beacon_round": round_no, "at": at}}
    return {"session_id": "sess_x", "module_type": "emporia:bargain:v2", "config": {"rand": {"commits": commits}},
            "state": {"data": {"rand": {"commits": commits, "round": round_no, "beacon_sig": sig}}}}


def test_verify_beacon_checks_a_game_against_drands_own_round(monkeypatch, capsys):
    from emporia import beacon
    from tests.test_rand import R_LATE, recorded_get
    mod = _beacon_script()
    game = _game_on_round(R_LATE["round"], R_LATE["signature"])
    monkeypatch.setattr(mod, "get_json", lambda url: game)
    monkeypatch.setattr(beacon, "http_get", recorded_get)             # drand's answer, from a recording
    assert mod.main(["sess_x", "--relay", "http://relay.example"]) == 0
    out = capsys.readouterr().out
    assert "the relay stored exactly the signature drand serves" in out and R_LATE["randomness"] in out
    assert out.strip().endswith("all checks hold")
    # a relay that stored another round's signature, or one that is not drand's, is caught
    from tests.test_rand import R1000
    for bad in (R1000["signature"], "ab" * 48):
        monkeypatch.setattr(mod, "get_json", lambda url, bad=bad: _game_on_round(R_LATE["round"], bad))
        assert mod.main(["sess_x", "--relay", "http://relay.example"]) == 1
        assert "FAIL" in capsys.readouterr().out
    # offline: no network is asked, and the output says so
    monkeypatch.setattr(mod, "get_json", lambda url: game)
    monkeypatch.setattr(beacon, "http_get", lambda url: (_ for _ in ()).throw(AssertionError("asked drand")))
    assert mod.main(["sess_x", "--relay", "http://relay.example", "--offline"]) == 0
    assert "offline: drand itself was not asked" in capsys.readouterr().out


def test_verify_beacon_reports_a_game_that_does_not_draw_or_has_not_drawn_yet(monkeypatch, capsys):
    mod = _beacon_script()
    monkeypatch.setattr(mod, "get_json", lambda url: {"module_type": "emporia:blotto:v2", "state": {"data": {}}})
    assert mod.main(["sess_x", "--relay", "http://r"]) == 2
    from tests.test_rand import R_LATE
    monkeypatch.setattr(mod, "get_json", lambda url: _game_on_round(R_LATE["round"], None))
    assert mod.main(["sess_x", "--relay", "http://r"]) == 2
    assert "no randomness has been drawn" in capsys.readouterr().out


def test_log_verify_takes_a_key_directory_for_an_agent_that_is_not_hermes(monkeypatch, tmp_path, capsys):
    import argparse
    import json
    import subprocess

    from emporia import agent_init, cli
    agent_init.init_agent(name="logger", key_dir=tmp_path / "k", relay="http://relay.example", register=False)
    seen = {}

    def fake_run(argv, env=None, **kw):
        seen.update(env=env, code=argv[-1])
        out = {"ok": True, "status": "first", "detail": "first look: a log of 3 entries, kept to check next time",
               "key_steps": {"logged": 0, "pending": 0}}
        return subprocess.CompletedProcess(argv, 0, stdout=json.dumps(out) + "\n", stderr="")
    monkeypatch.setattr(cli.subprocess, "run", fake_run)
    args = argparse.Namespace(name="", dir=str(tmp_path / "k"), compare_with=None)
    assert cli.cmd_log_verify(args) == 0
    assert seen["env"]["EMPORIA_KEYS_DIR"] == str(tmp_path / "k") and seen["env"]["EMPORIA_NO_DOTENV"] == "1"
    assert seen["env"]["EMPORIA_RELAY_URL"] == "http://relay.example" and "verify_relay" in seen["code"]
    assert "logger's relay: first look" in capsys.readouterr().out
    assert cli.cmd_log_verify(argparse.Namespace(name="", dir=str(tmp_path / "nope"), compare_with=None)) == 1
    assert "emporia agent init" in capsys.readouterr().out
    parsed = cli.build_parser().parse_args(["log", "verify", "--dir", "/x"])
    assert parsed.dir == "/x" and parsed.name == ""


# ── doctor: the facilitator's fee payer needs gas ───────────────────────────────────────────

def test_the_facilitator_fee_payer_check_warns_on_no_gas_and_notes_failures():
    from emporia import facilitator_check as fc
    base, sol = "eip155:84532", "solana:EtWTRABZaYq6iMfeYKouRu166VU2xqa1"
    supported = {"kinds": [{"network": sol, "scheme": "exact", "extra": {"feePayer": "SoLpayer"}}],
                 "signers": {"eip155:*": ["0xEvmSigner"], "solana:*": ["SoLpayer"]}}
    assert fc.fee_payers(supported, [base, sol]) == {base: ["0xEvmSigner"], sol: ["SoLpayer"]}
    asked = []

    def post(url, body):
        asked.append((url, body["method"]))
        empty = body["params"][0] in ("0xEvmSigner",)
        if body["method"] == "eth_getBalance":
            return {"result": "0x0" if empty else "0x2386f26fc10000"}
        return {"result": {"value": 5_000_000}}
    out = fc.check("https://fac.example/", [base, sol], {"EMPORIA_X402_RPC_EIP155_84532": "https://my-rpc.example"},
                   get=lambda url: supported, post=post)
    assert [lvl for lvl, *_ in out] == ["warn", "note"]          # base: its only payer is empty; solana: funded
    level, msg, fix = out[0]
    assert "none of" in msg and base in msg and "has gas" in msg and "EMPORIA_X402_FACILITATOR_URL" in fix
    assert out[1][1] == f"1 of 1 fee payer(s) on {sol} have gas"
    assert ("https://my-rpc.example", "eth_getBalance") in asked and any(m == "getBalance" for _, m in asked)
    # funded: one quiet line
    assert fc.check("https://fac.example", [base], {}, get=lambda u: supported,
                    post=lambda url, body: {"result": "0x1"}) == [("note", f"1 of 1 fee payer(s) on {base} have gas", "")]
    # many payers, some empty, one unreadable (a facilitator settles from a funded one): one line, no warning
    many = {"signers": {"eip155:*": [f"0x{i}" for i in range(15)]}}

    def some_funded(url, body):
        if body["params"][0] == "0x14":
            raise OSError("rpc hiccup")
        return {"result": "0x1" if body["params"][0] in ("0x0", "0x1", "0x2") else "0x0"}
    assert fc.check("https://fac.example", [base], {}, get=lambda u: many, post=some_funded) == [
        ("note", f"3 of 14 fee payer(s) on {base} have gas (1 could not be read)", "")]
    # failures are notes, never errors: facilitator down, RPC down, no signer named, no facilitator at all
    def down(*a, **k):
        raise OSError("down")
    assert [lvl for lvl, *_ in fc.check("https://fac.example", [base], {}, get=down)] == ["note"]
    assert [lvl for lvl, *_ in fc.check("https://fac.example", [base], {}, get=lambda u: supported, post=down)] == ["note"]
    assert [lvl for lvl, *_ in fc.check("https://fac.example", [base], {}, get=lambda u: {})] == ["note"]
    assert fc.check("", [base], {}) == [] and fc.check("https://f", [], {}) == []


def test_doctor_shows_the_fee_payer_warning(monkeypatch, capsys):
    from emporia import cli, facilitator_check
    monkeypatch.setattr(cli, "health", lambda: {"version": "1", "fee_bps": 100,
                                                "x402_enabled": True, "x402_networks": ["eip155:84532"]})
    monkeypatch.setattr(cli, "_relay_code_behind", lambda h: "")
    monkeypatch.setattr(cli, "doctor_federation", lambda rep, h: None)
    monkeypatch.setenv("EMPORIA_X402_FACILITATOR_URL", "https://fac.example")
    monkeypatch.setattr(facilitator_check, "check", lambda *a, **k: [
        ("warn", "the x402 facilitator's fee payer 0xabc on eip155:84532 has no gas: its settlements will fail", "fix it"),
        ("note", "could not read something", "")])
    rep = cli.Report()
    cli.doctor_relay(rep)
    out = capsys.readouterr().out
    assert "! the x402 facilitator's fee payer 0xabc" in out and "fix: fix it" in out and "✓ could not read something" in out


# ── payment answers carry the settlement transaction ───────────────────────────────────────

class _Resp:
    def __init__(self, code, body, headers=None):
        self.status_code, self._b, self.headers = code, body, headers or {}

    def json(self):
        return self._b


def _payment_response(**kw):
    import base64
    import json
    return base64.b64encode(json.dumps({"success": True, "transaction": "0xabc123", "network": "eip155:84532",
                                        "payer": "0xpayer", **kw}).encode()).decode()


def test_paid_answers_include_the_settlement_transaction(monkeypatch):
    sess = {"payment_rules": {"price": "1"}, "participants": ["a", "b"]}
    ok = _Resp(200, {"status": "joined", "room_id": "r1"}, {"payment-response": _payment_response()})
    monkeypatch.setattr(mcp_server, "_bearer_request", lambda *a, **k: ok)
    monkeypatch.setattr(mcp_server, "AGENT_ID", "payer1")
    out = mcp_server.join_room("r1", agent_id="payer1")
    assert out["success"] and out["settlement"] == {"transaction": "0xabc123", "network": "eip155:84532",
                                                    "payer": "0xpayer"}
    out = mcp_server.fund_session("s1", agent_id="payer1")
    assert out["settlement"]["transaction"] == "0xabc123"
    out = mcp_server.subscribe_agora_topic("t1", agent_id="payer1")
    assert out["settlement"]["transaction"] == "0xabc123"
    # no header (free entry, a card): no settlement key; a failed settlement: none; the body's own (Tempo) is kept
    monkeypatch.setattr(mcp_server, "_bearer_request", lambda *a, **k: _Resp(200, {"status": "joined"}))
    assert "settlement" not in mcp_server.join_room("r1", agent_id="payer1")
    failed = _Resp(200, {"status": "joined"}, {"payment-response": _payment_response(success=False)})
    monkeypatch.setattr(mcp_server, "_bearer_request", lambda *a, **k: failed)
    assert "settlement" not in mcp_server.join_room("r1", agent_id="payer1")
    tempo = _Resp(200, {"status": "joined", "settlement": {"rail": "tempo", "transaction": "0xtempo"}})
    monkeypatch.setattr(mcp_server, "_bearer_request", lambda *a, **k: tempo)
    assert mcp_server.join_room("r1", agent_id="payer1")["settlement"] == {"rail": "tempo", "transaction": "0xtempo"}
    assert sess


# ── close_room ──────────────────────────────────────────────────────────────────────────────

def test_a_creator_closes_its_room_and_the_history_stays():
    from datetime import UTC, datetime

    from fastapi.testclient import TestClient

    from emporia import autonomy, features, relay_server as rs, wire
    from emporia.identity import sign
    from tests.test_emporia import _register_keyed, _signed_room, _stable_of

    client = TestClient(rs.app, client=("127.0.0.1", 51234))
    for n in ("cl_owner", "cl_member", "cl_late"):
        _register_keyed(client, n)
    h = lambda n: {"x-emporia-agent-id": n}                                   # noqa: E731
    room = client.post("/rooms", json=_signed_room("cl_owner", {"name": "cl room"}), headers=h("cl_owner")).json()
    rid = room["room_id"]
    assert client.post(f"/rooms/{rid}/join", json={"agent_id": "cl_member"}, headers=h("cl_member")).status_code == 200
    from emporia import rooms as rooms_db
    rooms_db.post_message(rid, "cl_owner", "before closing", db_path=rs.DATABASE_PATH)

    def body(who="cl_owner"):
        at = datetime.now(UTC).isoformat()
        return {"at": at, "signature": sign(wire.signed_room_close_payload(rid, at, _stable_of(who)), who)}

    assert client.post(f"/rooms/{rid}/close", json=body("cl_member"), headers=h("cl_member")).status_code in (401, 403)
    forged = body()
    forged["signature"] = "A" * 86
    assert client.post(f"/rooms/{rid}/close", json=forged, headers=h("cl_owner")).status_code in (400, 401, 403)
    done = client.post(f"/rooms/{rid}/close", json=body(), headers=h("cl_owner"))
    assert done.status_code == 200 and done.json()["removed"] == ["cl_member"], done.text
    got = client.get(f"/rooms/{rid}", headers=h("cl_owner")).json()
    assert got["closed"] is True and got["members"] == ["cl_owner"]
    assert [a["act"] for a in got["signed_acts"]][-1] == "close"
    # nobody joins or is invited again; closing twice says so; the history is still there
    assert client.post(f"/rooms/{rid}/join", json={"agent_id": "cl_late"}, headers=h("cl_late")).status_code == 403
    assert client.post(f"/rooms/{rid}/join", json={"agent_id": "cl_member"}, headers=h("cl_member")).status_code == 403
    assert client.post(f"/rooms/{rid}/close", json=body(), headers=h("cl_owner")).status_code == 409
    msgs = client.get(f"/rooms/{rid}/messages", headers=h("cl_owner"))
    assert msgs.status_code == 200 and "before closing" in msgs.text
    # the signed acts check out, and put the creator alone in the room
    from emporia.identity import get_public_key_hex, room_members_by_acts, signing_key_for
    keys = {n: get_public_key_hex(n) for n in ("cl_owner", "cl_member")}
    assert room_members_by_acts(got, lambda a: keys[a]) == {"cl_owner"}
    assert "close_room" in features.BY_NAME["core"].tools and "close_room" in autonomy.WRITE_TOOLS
    assert signing_key_for


def test_doctor_never_prints_an_empty_dashboard_tab_section(capsys):
    from emporia import cli
    for compact in (False, True):
        rep = cli.Report(compact=compact)
        cli.doctor_dashboard(rep, [])
        rep.flush()
    out = capsys.readouterr().out
    assert out.count("Dashboard tab") == 2 and out.count("no agent profile was found here") == 2
    assert "emporia setup agent NAME" in out


# ── standing, record and results find an agent's sessions through the participants table ──────

def _finished(sid, players, status="completed"):
    from datetime import UTC, datetime

    from emporia import relay_server as rs
    from emporia.module_sdk import get_interaction_module
    now = datetime.now(UTC).isoformat()
    state = get_interaction_module("emporia:blotto:v2").initial_state(players, {})
    rs.save_session(session_id=sid, module_type="emporia:blotto:v2", config={}, payment_rules={"mode": "free"},
                    state=state, current_agent=players[0], step_number=3, status=status, participants=players,
                    created_at=now, updated_at=now)
    for p in players:
        rs.add_participant(sid, p)


def test_an_agents_sessions_come_from_the_participants_table_with_the_same_results():
    from fastapi.testclient import TestClient

    from emporia import relay_server as rs
    from emporia.routes import standing
    from tests.test_emporia import _register

    client = TestClient(rs.app, client=("127.0.0.1", 51233))
    names = ["pt_a", "pt_b", "pt_c", "pt_x_d", "pt_xxd"]          # "pt_x_d" is a LIKE pattern that also matches "pt_xxd"
    for n in names:
        _register(client, n)
    _finished("pt_s1", ["pt_a", "pt_b"])
    _finished("pt_s2", ["pt_b", "pt_c"])
    _finished("pt_s3", ["pt_a", "pt_c"], status="abandoned")
    _finished("pt_s4", ["pt_xxd", "pt_c"])
    with rs.DB_LOCK, rs.get_db() as conn:
        def old_way(agent):        # the scan this replaces: a LIKE over the JSON, then the exact filter the callers apply
            rows = conn.execute("SELECT session_id, participants_json FROM sessions WHERE participants_json LIKE ? "
                                "AND status IN ('completed', 'abandoned')", (f'%"{agent}"%',)).fetchall()
            import json
            return {r["session_id"] for r in rows if agent in json.loads(r["participants_json"])}

        def new_way(agent):
            return {r["session_id"] for r in conn.execute(
                "SELECT session_id FROM sessions WHERE session_id IN (SELECT session_id FROM participants "
                "WHERE agent_id = ?) AND status IN ('completed', 'abandoned')", (agent,))}

        for agent in names:
            assert new_way(agent) == old_way(agent), agent
        plan = " ".join(str(tuple(r)) for r in conn.execute(
            "EXPLAIN QUERY PLAN SELECT * FROM sessions WHERE session_id IN (SELECT session_id FROM participants "
            "WHERE agent_id = ?) AND status IN ('completed', 'abandoned')", ("pt_a",)))
    assert "idx_participants_agent" in plan or "sqlite_autoindex_participants" in plan, plan
    assert "SCAN sessions" not in plan, plan

    rec = {n: client.get(f"/agents/{n}/record").json()["games"] for n in names}
    assert rec["pt_a"]["finished"] == 1 and rec["pt_a"]["abandoned"] == 1 and rec["pt_x_d"]["finished"] == 0
    assert rec["pt_xxd"]["finished"] == 1                           # and "pt_x_d" does not collect its game
    got = {r["session_id"] for r in client.get("/results", params={"agent_id": "pt_c"}).json()["results"]}
    assert got == {"pt_s2", "pt_s4"}
    assert [r["session_id"] for r in client.get("/results", params={"agent_id": "pt_x_d"}).json()["results"]] == []
    games = standing._public_games_of("pt_c")
    assert {g["session_id"] for g in games} == {"pt_s2", "pt_s4"} and standing._public_games_of("pt_x_d") == []
    assert "participants_json LIKE" not in (ROOT / "src" / "emporia" / "routes" / "standing.py").read_text()


# ── the README's "Setup choices" table follows setup_choices.py ─────────────────────────────

#: setup choice key -> the README row's first cell. Keys of `emporia agent init` (init_*) are in docs/any-agent.md.
README_ROWS = {
    "profile": "Profile", "role": "Role", "keys": "Keys", "wallet": "Wallets", "wake": "Wake mode",
    "rounds": "Rounds", "optional": "Optional features", "playbook": "Playbook",
    "about": "Bio (`about`)", "register": "Register now", "sync_model": "Same model as this profile",
    "signer": "Signer", "relay": "Relay",
}
#: Where the README words an option differently from its stored value.
README_ALIAS = {("keys", "env"): ".env", ("keys", "files"): "key files"}


def _readme_rows() -> dict[str, list[str]]:
    text = (ROOT / "README.md").read_text()
    table = text.split("## Setup choices")[1].split("\n### ")[0]
    rows = {}
    for line in table.splitlines():
        if line.startswith("| ") and not line.startswith(("| Setting", "|---")):
            cells = [c.strip() for c in re.split(r"(?<!\\)\|", line.strip().strip("|"))]
            rows[cells[0]] = cells
    return rows


def test_every_setup_choice_has_a_readme_row_with_its_options_and_default():
    rows = _readme_rows()
    # `emporia agent init` (init_*) is in docs/any-agent.md; `emporia house create` (house_kind) and a relay's
    # money (relay_*) in docs/deploy-relay.md, generated by scripts/build_docs.py.
    asked = (set(setup_choices.CHOICES) - {k for k in setup_choices.CHOICES if k.startswith(("init_", "relay_"))}
             - {"house_kind"})
    assert asked == set(README_ROWS), f"README_ROWS is out of step with setup_choices: {asked ^ set(README_ROWS)}"
    for key, label in README_ROWS.items():
        assert label in rows, f"README 'Setup choices' has no row {label!r}"
        _, options, recommended = rows[label][:3]
        choice = setup_choices.CHOICES[key]
        if key in ("wallet", "optional"):        # described in groups, not value by value
            continue
        for o in choice.options:
            word = README_ALIAS.get((key, o.value), o.value)
            assert word.lower() in options.lower(), f"{label}: option {o.value!r} is not in the README row"
        default = README_ALIAS.get((key, choice.default), choice.default)
        assert default.lower() in recommended.lower(), f"{label}: recommended {choice.default!r} is not in the row"


def test_the_optional_features_row_lists_every_feature_and_the_recommended_set():
    _, options, recommended = _readme_rows()["Optional features"][:3]
    # `models` is the declare-your-model question: part of core now, still asked in setup
    shown = {"rounds", "models", *features.OPTIONAL}
    assert {w.strip() for w in options.split(",")} == shown
    assert {w.strip() for w in recommended.split(",")} == {"rounds", "models", *features.DEFAULTS}
    assert {o.value for o in setup_choices.CHOICES["optional"].options} == shown
    assert setup_choices.CHOICES["optional"].recommended() == {"rounds", "models", *features.DEFAULTS}
