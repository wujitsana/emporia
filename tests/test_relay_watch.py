"""`emporia relay watch`: the summarizer, the `expected` tag on normal non-2xx answers, and the named events
the relay logs for it (rate_limited, agent_registered, signin_failed, payment_*, unhandled_error)."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

from fastapi.testclient import TestClient

from emporia import relay_watch
from emporia.logging_config import ip_tag

NOW = datetime(2026, 11, 10, 12, 0, tzinfo=UTC)


def _line(minutes_ago: float, msg: str = "request", **fields) -> str:
    ts = (NOW - timedelta(minutes=minutes_ago)).isoformat(timespec="milliseconds")
    return json.dumps({"ts": ts, "level": "INFO", "logger": "emporia.relay", "msg": msg,
                       "request_id": fields.pop("request_id", "r1"), **fields})


def _req(minutes_ago, status, path="/x", method="GET", ip="10.0.0.1", **extra):
    return _line(minutes_ago, status_code=status, path=path, method=method, client_ip=ip,
                 duration_ms=1.0, **extra)


# ─── the summarizer ──────────────────────────────────────────────────────────

def test_summary_counts_everything_inside_the_window_only():
    lines = [
        "relay-1  | " + _req(5, 200),                       # a docker compose prefix is tolerated
        _req(10, 200, ip="10.0.0.2"),
        _req(120, 500, path="/old"),                         # outside a 1h window
        "not json at all",
        _req(3, 404, path="/agents/alice/key-log", expected=True),
        _req(3, 404, path="/agents/bob/key-log"),
        _req(2, 403, path="/rooms/abcdef123456", method="POST"),
        _req(2, 403, path="/rooms/zzzzzzzzzz00", method="POST"),
        _line(1, "rate_limited", limit="ip_auth", path="/agents/register", ip_tag="t"),
        _line(1, "rate_limited", limit="agent", agent_id="a", path="/posts"),
        _line(1, "rate_limited", limit="ip_auth", path="/dashboard/session", ip_tag="t"),
        _line(4, "agent_registered", agent_id="a"),
        _line(4, "signin_failed", reason="not_registered", agent_id="z", expected=True),
        _line(4, "signin_failed", reason="bad_signature", agent_id="a"),
        _line(4, "payment_settled", rail="x402", network="eip155:84532", cents=500),
        _line(4, "payment_settled", rail="stripe", network="", cents=250),
        _line(4, "payment_failed", rail="x402", network="eip155:84532", cents=None, reason="insufficient"),
        _line(4, "ws_rejected", channel="room", reason="not_a_member"),
    ]
    s = relay_watch.summarize(lines, since=timedelta(hours=1), now=NOW)
    assert s["requests"] == 6
    assert s["server_errors"] == 0
    assert s["client_errors_total"] == 3                      # the expected 404 is not in the list
    assert s["expected_hidden"] == 1
    top = {(c["status"], c["path"]): c["count"] for c in s["client_errors"]}
    assert top[(403, "/rooms/:id")] == 2                      # ids folded into one route
    assert top[(404, "/agents/:id/key-log")] == 1
    assert s["rate_limited"] == {"ip_auth": 2, "agent": 1}
    assert s["registrations"] == 1
    assert s["signin_failures"] == {"not_registered": 1, "bad_signature": 1}
    assert s["signin_failures_expected"] == 1
    assert s["payments"] == {"settled": 2, "failed": 1, "cents_settled": 750}
    assert s["ws_rejected"] == {"not_a_member": 1}
    assert s["busiest_clients"][0] == {"client": ip_tag("10.0.0.1"), "count": 5}
    assert "10.0.0.1" not in json.dumps(s)                    # clients are tags, never addresses


def test_server_errors_come_from_5xx_lines_and_unhandled_error_without_double_counting():
    lines = [
        _req(5, 502, path="/a", request_id="aaa"),
        _line(4, "unhandled_error", path="/b", method="GET", exception_type="KeyError", request_id="bbb"),
        _req(4, 500, path="/b", request_id="bbb"),           # the same request seen twice: one error
        _line(3, "unhandled_error", path="/c", exception_type="ValueError", request_id="ccc"),
        _line(2, "unhandled_error", path="/d", exception_type="OSError", request_id="ddd"),
    ]
    s = relay_watch.summarize(lines, since=timedelta(hours=1), now=NOW)
    assert s["server_errors"] == 4
    assert [e["request_id"] for e in s["last_server_errors"]] == ["bbb", "ccc", "ddd"]
    assert s["last_server_errors"][0]["exception_type"] == "KeyError"
    text, attention = relay_watch.verdict(s, {"ok": True})
    assert attention and text.startswith("attention: 4 server errors in the last 1h")


def test_verdict_and_rendering_for_a_quiet_relay_and_a_dead_one():
    s = relay_watch.summarize([_req(1, 200), _req(2, 404, expected=True)],
                              since=timedelta(minutes=30), now=NOW)
    assert relay_watch.verdict(s, {"ok": True}) == ("all normal", False)
    out = relay_watch.render(s, {"ok": True, "version": "1", "schema_version": 2}, "all normal")
    assert "verdict:   all normal" in out and "(+1 expected, hidden)" in out
    text, attention = relay_watch.verdict(s, {"ok": False})
    assert attention and "does not answer /health" in text
    failed = relay_watch.summarize([_line(1, "payment_failed", rail="x402", network="n")],
                                   since=timedelta(minutes=30), now=NOW)
    assert relay_watch.verdict(failed, {"ok": True})[1] is True


def test_parse_since_and_path_normalizing():
    assert relay_watch.parse_since("30m") == timedelta(minutes=30)
    assert relay_watch.parse_since("2d") == timedelta(days=2)
    assert relay_watch.parse_since("3") == timedelta(hours=3)
    try:
        relay_watch.parse_since("soon")
    except ValueError:
        pass
    else:
        raise AssertionError("a bad --since must be refused")
    assert relay_watch.normalize_path("/agents/register") == "/agents/register"
    assert relay_watch.normalize_path("/sessions/sess_0123456789ab/chat") == "/sessions/:id/chat"


def test_cli_watch_reads_a_log_file_and_exits_by_verdict(tmp_path, capsys):
    from emporia import cli

    quiet = tmp_path / "quiet.log"
    now = datetime.now(UTC)
    ts = now.isoformat(timespec="milliseconds")
    row = {"ts": ts, "level": "INFO", "logger": "emporia.relay", "msg": "request", "request_id": "r",
           "status_code": 200, "path": "/x", "method": "GET", "client_ip": "1.1.1.1"}
    quiet.write_text(json.dumps(row) + "\n")
    bad = tmp_path / "bad.log"
    bad.write_text(json.dumps(row) + "\n" + json.dumps(
        {**row, "msg": "unhandled_error", "exception_type": "KeyError", "request_id": "boom"}) + "\n")
    ok = {"status": "ok", "version": "1", "schema_version": 3}

    class _R:
        status_code = 200
        def json(self):
            return ok
    with patch("httpx.get", return_value=_R()):
        assert cli.main(["relay", "watch", "--log", str(quiet), "--since", "1h"]) == 0
        assert "all normal" in capsys.readouterr().out
        assert cli.main(["relay", "watch", "--log", str(bad), "--json"]) == 1
        data = json.loads(capsys.readouterr().out)
        assert data["attention"] and data["last_server_errors"][0]["exception_type"] == "KeyError"
        assert cli.main(["relay", "watch", "--log", str(tmp_path / "missing.log")]) == 2


# ─── expected tagging and named events, against the real app ─────────────────

def _records(caplog, message):
    return [r for r in caplog.records if r.name == "emporia.relay" and r.message == message]


def test_unknown_agent_key_log_404_is_tagged_expected_but_keeps_its_status(client, caplog):
    with caplog.at_level(logging.INFO, logger="emporia.relay"):
        r = client.get("/agents/nobody_such_agent_xyz/key-log")
    assert r.status_code == 404
    line = [x for x in _records(caplog, "request") if x.path.endswith("/key-log")][0]
    assert line.expected is True


def test_a_real_client_error_is_not_tagged(client, caplog):
    with caplog.at_level(logging.INFO, logger="emporia.relay"):
        r = client.post("/agents/register", json={"agent_id": "x", "public_key_hex": ""})
    assert r.status_code == 400
    line = _records(caplog, "request")[-1]
    assert not hasattr(line, "expected")


def test_a_switched_off_module_404_is_tagged_expected(client, caplog):
    with patch("emporia.relay_modules.off_because", return_value="the labs module is off"), \
         caplog.at_level(logging.INFO, logger="emporia.relay"):
        r = client.get("/labs")
    assert r.status_code == 404
    assert _records(caplog, "request")[-1].expected is True


def test_signin_by_an_unregistered_agent_is_a_tagged_404_and_a_named_event(client, caplog):
    from emporia import relay_server

    ch = client.post("/dashboard/challenge").json()
    body = {"agent_id": "never_registered_zz", "challenge_id": ch["challenge_id"],
            "relay": "https://relay.test", "signature": "00" * 64}
    with patch.object(relay_server, "_names_this_relay", return_value=True), \
         caplog.at_level(logging.INFO, logger="emporia.relay"):
        r = client.post("/dashboard/session", json=body)
    assert r.status_code == 404
    ev = _records(caplog, "signin_failed")[0]
    assert ev.reason == "not_registered" and ev.expected is True
    assert _records(caplog, "request")[-1].expected is True
    assert "signature" not in ev.__dict__ and "nonce" not in ev.__dict__


def test_signin_with_a_bad_signature_is_a_warning_and_not_tagged(client, caplog):
    from emporia import relay_server
    from tests.test_emporia import _register

    _register(client, "watch_signin_agent")
    ch = client.post("/dashboard/challenge").json()
    body = {"agent_id": "watch_signin_agent", "challenge_id": ch["challenge_id"],
            "relay": "https://relay.test", "signature": "00" * 64}
    with patch.object(relay_server, "_names_this_relay", return_value=True), \
         caplog.at_level(logging.INFO, logger="emporia.relay"):
        r = client.post("/dashboard/session", json=body)
    assert r.status_code == 403
    ev = _records(caplog, "signin_failed")[0]
    assert ev.reason == "bad_signature" and ev.levelno == logging.WARNING
    assert not hasattr(_records(caplog, "request")[-1], "expected")


def test_registration_is_a_named_event(client, caplog):
    from tests.test_emporia import _register

    with caplog.at_level(logging.INFO, logger="emporia.relay"):
        _register(client, "watch_registered_agent")
    ev = _records(caplog, "agent_registered")[0]
    assert ev.agent_id == "watch_registered_agent" and ev.stable_id


def test_an_ip_rate_limit_refusal_is_a_named_event_with_a_hashed_client(caplog):
    from emporia import relay_server as rs

    tc = TestClient(rs.app, client=("203.0.113.77", 4000))
    rs._RATE_WINDOWS.clear()
    with patch.dict(rs._RATE_LIMITS, {"auth": (1, 60.0)}), \
         caplog.at_level(logging.INFO, logger="emporia.relay"):
        assert tc.post("/dashboard/challenge").status_code == 200
        assert tc.post("/dashboard/challenge").status_code == 429
    rs._RATE_WINDOWS.clear()
    ev = _records(caplog, "rate_limited")[0]
    assert ev.limit == "ip_auth" and ev.path == "/dashboard/challenge"
    assert ev.ip_tag == ip_tag("203.0.113.77")
    assert not hasattr(ev, "client_ip")
    assert "203.0.113.77" not in json.dumps({k: str(v) for k, v in ev.__dict__.items()})


def test_an_agent_rate_limit_refusal_names_the_agent_and_the_path(client, caplog):
    from fastapi import HTTPException

    from emporia import relay_server as rs
    from emporia.logging_config import request_info_ctx

    tok = request_info_ctx.set({"path": "/posts", "expected": False})
    try:
        with patch.object(rs.AGENT_REGISTRY, "check_rate_limit", return_value=False), \
             caplog.at_level(logging.INFO, logger="emporia.relay"):
            try:
                rs._enforce_agent_rate_limit("noisy_agent")
            except HTTPException as e:
                assert e.status_code == 429
    finally:
        request_info_ctx.reset(tok)
    ev = _records(caplog, "rate_limited")[0]
    assert (ev.limit, ev.agent_id, ev.path) == ("agent", "noisy_agent", "/posts")


def test_x402_outcomes_are_named_payment_events(caplog):
    from emporia import relay_server as rs

    with caplog.at_level(logging.INFO, logger="emporia.relay"):
        rs._record_x402_settlement(None, {"success": True, "transaction": "0xabc"},
                                   network="eip155:84532", payer="0xpayer", cents=300)
        rs._record_x402_settlement(None, {"success": False, "errorReason": "insufficient_funds"},
                                   network="eip155:84532", payer="0xpayer", cents=300)
    ok = _records(caplog, "payment_settled")[0]
    bad = _records(caplog, "payment_failed")[0]
    assert (ok.rail, ok.network, ok.cents) == ("x402", "eip155:84532", 300)
    assert bad.reason == "insufficient_funds" and bad.levelno == logging.WARNING
    dumped = json.dumps({k: str(v) for k, v in {**ok.__dict__, **bad.__dict__}.items()})
    assert "0xpayer" not in dumped and "0xabc" not in dumped


def test_a_recorded_stripe_settlement_is_a_named_payment_event(caplog):
    from emporia import relay_server as rs

    with caplog.at_level(logging.INFO, logger="emporia.relay"):
        rs.record_settlement({"session_id": "watch_sess_1", "winner_id": "w", "total_price_cents": 400,
                              "payment_intent_ids": ["pi_watch_1"], "payer_id": "p", "rail": "stripe",
                              "transfer_status": "transferred"})
    ev = _records(caplog, "payment_settled")[0]
    assert (ev.rail, ev.cents) == ("stripe", 400)
