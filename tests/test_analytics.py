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
            {"agent_id": "beta", "amount_cents": 50, "rail": "stripe", "status": "refunded", "created_at": _iso(NOW - DAY)},
        ],
        "settlements": [
            {"winner_id": "beta", "winner_payout_cents": 195, "platform_fee_cents": 5, "rail": "x402",
             "transfer_status": "captured", "status": "settled", "created_at": _iso(NOW - DAY)},
            {"winner_id": "beta", "winner_payout_cents": 98, "platform_fee_cents": 2, "rail": "stripe",
             "transfer_status": "pending_connect", "status": "settled", "created_at": _iso(NOW - DAY)},
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


def test_the_relay_gives_an_agent_its_own_payments_only():
    from fastapi.testclient import TestClient
    from emporia import relay_server as rs
    client = TestClient(rs.app)
    with rs.DB_LOCK, rs.get_db() as conn:
        for pid, who in (("pay_mine_1", "stats_me"), ("pay_other_1", "stats_other")):
            conn.execute("INSERT INTO payments (payment_id, agent_id, amount_cents, payment_intent_id, payment_type, "
                         "created_at, rail) VALUES (?, ?, 25, ?, 'room_entry', '2026-11-01T00:00:00+00:00', 'x402')",
                         (pid, who, pid))
    r = client.get("/payments/records?mine=1", headers={"X-Emporia-Agent-Id": "stats_me"})
    assert r.status_code == 200 and [p["payment_id"] for p in r.json()["payments"]] == ["pay_mine_1"]
    assert client.get("/payments/records?mine=1").status_code == 401
