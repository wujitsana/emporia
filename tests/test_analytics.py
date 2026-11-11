"""P287: an agent's own analytics — earned, spent, owed, and what its Emporia activity cost."""
import json
import sqlite3
import time

from emporia import analytics

NOW = 1_800_000_000.0
DAY = analytics.DAY


def _iso(t):
    from datetime import UTC, datetime
    return datetime.fromtimestamp(t, UTC).isoformat()


def test_figures_from_payments_settlements_holds_and_record():
    relay = {
        "payments": [
            {"agent_id": "beta", "amount_cents": 100, "rail": "x402", "payment_type": "purchase", "created_at": _iso(NOW - DAY)},
            {"agent_id": "beta", "amount_cents": 25, "rail": "x402", "payment_type": "room_entry", "created_at": _iso(NOW - 2 * DAY)},
            {"agent_id": "beta", "amount_cents": 999, "rail": "x402", "created_at": _iso(NOW - 40 * DAY)},   # outside
            {"agent_id": "beta", "amount_cents": 50, "rail": "tempo", "status": "refunded", "created_at": _iso(NOW - DAY)},
        ],
        "settlements": [
            {"winner_id": "beta", "winner_payout_cents": 195, "platform_fee_cents": 5, "rail": "x402",
             "transfer_status": "captured", "status": "settled", "created_at": _iso(NOW - DAY)},
            {"winner_id": "beta", "winner_payout_cents": 98, "platform_fee_cents": 2, "rail": "tempo",
             "transfer_status": "pending", "status": "settled", "created_at": _iso(NOW - DAY)},
            {"winner_id": "beta", "winner_payout_cents": 0, "status": "refunded", "created_at": _iso(NOW - DAY)},
            {"winner_id": "alpha", "winner_payout_cents": 500, "status": "settled", "created_at": _iso(NOW - DAY)},
        ],
        "holds": [{"payer_agent_id": "beta", "amount_atomic": 500000, "state": "authorized"},
                  {"payer_agent_id": "beta", "amount_atomic": 900000, "state": "captured"}],
        "record": {"games": {"finished": 4, "won": 1, "abandoned": 2}, "as_buyer": {"purchases": 3}},
        "missing": [],
    }
    model = {"usd": 0.43, "available": True, "by_kind": {}, "sessions": 3}
    st = analytics.agent_stats("beta", relay, model, turns=7, turn_cap=300, days=30, now=NOW)
    assert st["spent_cents"] == 125 and st["spent_by_rail"] == {"x402": 125}
    assert st["spent_on"] == {"purchase": 100, "room_entry": 25}
    assert st["earned_cents"] == 195 and st["owed_cents"] == 98 and st["fees_on_winnings_cents"] == 7
    assert st["held_in_escrow_usdc"] == 0.5
    assert st["win_rate"] == 0.25 and st["as_buyer"]["purchases"] == 3
    assert st["net_cents"] == 195 - 125 - 43
    text = analytics.render(st)
    assert "earned   $1.95" in text and "owed $0.98" in text and "net      $0.27" in text and "7 relay-started" in text


def test_net_is_unknown_without_model_cost():
    st = analytics.agent_stats("beta", {"payments": [], "settlements": [], "holds": [], "record": {}}, None, now=NOW)
    assert st["net_cents"] is None and "model    unknown" in analytics.render(st)


def test_model_cost_by_conversation_from_hermes_state_db(tmp_path):
    db = sqlite3.connect(tmp_path / "state.db")
    db.execute("CREATE TABLE sessions (source TEXT, chat_id TEXT, started_at REAL, estimated_cost_usd REAL, "
               "actual_cost_usd REAL, input_tokens INT, output_tokens INT)")
    rows = [("emporia", "emporia:alpha", NOW - DAY, 0.10, None, 100, 10),
            ("emporia", "emporia:alpha:dm:beta", NOW - DAY, 0.05, 0.04, 50, 5),
            ("emporia", "emporia:alpha:session:sess_1", NOW - DAY, 0.02, None, 20, 2),
            ("emporia", "emporia:alpha:session:sess_old", NOW - 90 * DAY, 9.0, None, 1, 1),   # outside
            ("telegram", "123", NOW - DAY, 5.0, None, 1, 1)]                                 # not Emporia
    db.executemany("INSERT INTO sessions VALUES (?,?,?,?,?,?,?)", rows)
    db.commit()
    db.close()
    m = analytics.model_cost(tmp_path, "alpha", NOW - 30 * DAY)
    assert m["available"] and m["sessions"] == 3 and m["usd"] == 0.16          # actual cost wins over estimate
    assert m["by_kind"] == {"main": {"usd": 0.1, "sessions": 1}, "dm": {"usd": 0.04, "sessions": 1},
                            "session": {"usd": 0.02, "sessions": 1}}
    assert analytics.model_cost(tmp_path / "none", "alpha", 0)["available"] is False


def test_turns_and_collect_name_what_the_relay_refused(tmp_path):
    (tmp_path / "emporia_turns.json").write_text(json.dumps({"turns": [time.time() - 10, time.time() - 2 * DAY]}))
    assert analytics.turns_used(tmp_path) == 1                 # where the plugin kept it before
    new = tmp_path / "plugin-data" / "emporia" / "turns.json"
    new.parent.mkdir(parents=True)
    new.write_text(json.dumps({"turns": [time.time() - 10, time.time() - 20, time.time() - 30]}))
    assert analytics.turns_used(tmp_path) == 3                 # its plugin-data wins
    new.write_text("[]")
    assert analytics.turns_used(tmp_path) == 0

    def get(path):
        return (403, {"detail": "no"}) if "records" in path else (200, {"settlements": [], "holds": [], "games": {}})
    out = analytics.collect(get, "alpha")
    assert out["missing"] == ["payments"] and out["payments"] == []


def test_room_and_topic_entry_fees_count_as_the_creators_earnings():
    """A room's or an Agora topic's paid entry is settled to its creator (`winner_id`), with the
    payer in `payer_id` and the room or topic id in `session_id` (ROADMAP "Settlements by
    resource"). The creator's ?agent_id= read had no arm for those rows, so its entry fees never
    counted. Now they come back — each with its `kind`, the payer's identifiers redacted — and
    the stats count them as earnings from rooms and topics. No schema change."""
    from datetime import UTC, datetime

    from fastapi.testclient import TestClient

    from emporia import relay_server as rs
    from emporia.relay_server import record_settlement
    from tests.test_emporia import _register, _register_keyed, _signed_room

    client = TestClient(rs.app, client=("127.0.0.1", 51288))
    creator, payer, stranger = "stats_creator", "stats_payer", "stats_stranger"
    _register_keyed(client, creator)
    _register(client, payer)
    _register(client, stranger)
    r = client.post("/rooms", json=_signed_room(creator, {
        "name": "Stats Paid Room", "creator_id": creator,
        "room_type": "private", "gate_type": "paid", "entry_fee_cents": 150}),
        headers={"x-emporia-agent-id": creator})
    assert r.status_code == 200, r.text
    room_id = r.json()["room_id"]
    topic_id = "topic_stats_paid"
    with rs.DB_LOCK, rs.get_db() as conn:
        conn.execute("INSERT INTO agora_topics (topic_id, slug, name, gate_type, entry_fee_cents, creator_id, "
                     "created_at) VALUES (?, ?, 'Stats', 'paid_invite', 75, ?, ?)",
                     (topic_id, "stats-paid", creator, datetime.now(UTC).isoformat()))
    for resource, cents, rail, transfer in ((room_id, 150, "x402", "direct"),
                                            (topic_id, 75, "tempo", "direct")):
        record_settlement({
            "session_id": resource, "winner_id": creator, "outcome_type": "room_entry",
            "status": "settled", "total_price_cents": cents, "platform_fee_cents": 0,
            "winner_payout_cents": cents, "platform_fee_bps": 0,
            "payment_intent_ids": [f"pi_stats_{resource}"], "transfer_id": f"tr_stats_{resource}",
            "transfer_status": transfer, "payer_id": payer, "rail": rail,
        })

    def get(who, path):
        r = client.get(path, headers={"x-emporia-agent-id": who})
        return r.status_code, r.json()

    status, body = get(creator, f"/payments/settlements?agent_id={creator}&limit=500")
    assert status == 200, body
    sales = {s["session_id"]: s for s in body["settlements"]}
    assert set(sales) == {room_id, topic_id}
    assert sales[room_id]["kind"] == "room" and sales[topic_id]["kind"] == "topic"
    for s in sales.values():             # the payer's receipt identifiers are not the creator's
        assert s["payer_id"] == "[redacted]" and s["payment_intent_ids"] == ["[redacted]"]
        assert s["transfer_id"] == "[redacted]"
    # The payer still reads its own receipts in full, and a stranger the creator's not at all.
    status, own = get(payer, f"/payments/settlements?agent_id={payer}")
    mine = [s for s in own["settlements"] if s["session_id"] in (room_id, topic_id)]
    assert len(mine) == 2 and all(s["payer_id"] == payer and s["payment_intent_ids"][0].startswith("pi_")
                                  for s in mine)
    assert get(stranger, f"/payments/settlements?agent_id={creator}")[0] == 403

    relay = analytics.collect(lambda path: get(creator, path), creator)
    st = analytics.agent_stats(creator, relay, days=7)
    assert st["earned_cents"] == 225 and st["earned_by_rail"] == {"x402": 150, "tempo": 75}
    assert st["earned_by_source"] == {"rooms": 150, "topics": 75}
    assert st["access_sales"] == {"rooms": 1, "topics": 1}
    assert "from   rooms $1.50, topics $0.75" in analytics.render(st)


def test_a_sessions_settlement_kind_is_its_module():
    from datetime import UTC, datetime

    from emporia import relay_server as rs
    now = datetime.now(UTC).isoformat()
    with rs.DB_LOCK, rs.get_db() as conn:
        for sid, module, config in (("kind_game", "emporia:chess:v1", "{}"),
                                    ("kind_service", "emporia:service:v1", "{}")):
            conn.execute("INSERT OR REPLACE INTO sessions (session_id, module_type, config_json, payment_rules_json, "
                         "state_json, current_agent, participants_json, created_at, updated_at) "
                         "VALUES (?, ?, ?, '{}', '{}', '', '[]', ?, ?)", (sid, module, config, now, now))
        rows = [{"settlement_id": f"stl_{sid}", "session_id": sid, "payer_id": None}
                for sid in ("kind_game", "kind_service", "kind_gone")]
        rows.append({"settlement_id": "stl_access_gone", "session_id": "room_gone", "payer_id": "p"})
        kinds = rs._settlement_kinds(conn, rows)
    assert kinds == {"stl_kind_game": "game", "stl_kind_service": "service",
                     "stl_kind_gone": "session", "stl_access_gone": "access"}


def test_earnings_by_source_and_payout_states_from_settlement_kinds():
    """Games and services apart; a payout the relay still holds is owed, and a failed
    transfer is not earned. A relay that names no kind still counts an access sale."""
    t = _iso(NOW - DAY)
    rows = [
        {"winner_id": "a", "winner_payout_cents": 100, "kind": "game", "transfer_status": "escrow_captured", "created_at": t},
        {"winner_id": "a", "winner_payout_cents": 200, "kind": "service", "transfer_status": "direct", "created_at": t},
        {"winner_id": "a", "winner_payout_cents": 40, "payer_id": "[redacted]", "transfer_status": "direct", "created_at": t},
        {"winner_id": "a", "winner_payout_cents": 50, "kind": "room", "transfer_status": "pending", "created_at": t},
        {"winner_id": "a", "winner_payout_cents": 60, "kind": "topic", "transfer_status": "capture_failed: x", "created_at": t},
    ]
    st = analytics.agent_stats("a", {"settlements": rows}, now=NOW)
    assert st["earned_by_source"] == {"games": 100, "services": 200, "rooms_topics": 40}
    assert st["earned_cents"] == 340 and st["owed_cents"] == 50 and st["failed_payout_cents"] == 60
    assert st["access_sales"] == {"rooms_topics": 1, "rooms": 1, "topics": 1}


def test_the_relay_gives_an_agent_its_own_payments_only():
    from fastapi.testclient import TestClient
    from emporia import relay_server as rs
    from tests.test_emporia import _register_keyed
    client = TestClient(rs.app)
    _register_keyed(client, "stats_me")     # a token speaks only for a registered, active agent (final review FR-5)
    with rs.DB_LOCK, rs.get_db() as conn:
        for pid, who in (("pay_mine_1", "stats_me"), ("pay_other_1", "stats_other")):
            conn.execute("INSERT INTO payments (payment_id, agent_id, amount_cents, payment_intent_id, payment_type, "
                         "created_at, rail) VALUES (?, ?, 25, ?, 'room_entry', '2026-11-01T00:00:00+00:00', 'x402')",
                         (pid, who, pid))
    r = client.get("/payments/records?mine=1", headers={"X-Emporia-Agent-Id": "stats_me"})
    assert r.status_code == 200 and [p["payment_id"] for p in r.json()["payments"]] == ["pay_mine_1"]
    assert client.get("/payments/records?mine=1").status_code == 401
