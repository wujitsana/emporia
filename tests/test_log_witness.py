"""2.1: the agent's bridge checks its relay's transparency log once a day, with no model turn."""
import asyncio
import io
import json
from datetime import UTC, datetime

from emporia import log_witness as lw
from emporia.plugins.platforms.emporia import bridge

NOW = datetime(2026, 11, 18, 12, 0, tzinfo=UTC)


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def witness(tmp_path, results, clock=None, rng=lambda: 0.5):
    calls = []

    def verify(relay):
        calls.append(relay)
        r = results[min(len(calls), len(results)) - 1]
        if isinstance(r, Exception):
            raise r
        return r
    clock = clock or Clock()
    w = lw.Witness("https://r.example/", tmp_path / "plugin-data" / "emporia" / lw.RECORD, verify=verify,
                   clock=clock, now=lambda: NOW, rng=rng)
    return w, calls, clock


OK = {"ok": True, "status": "ok", "detail": "d", "state": {"size": 12, "epoch": 2}, "problems": []}
BAD = {"ok": False, "status": "fail", "detail": "the log shrank from 12 to 9 entries",
       "state": {"size": 12, "epoch": 2, "root": "ab", "signature": "SECRET-SIG"},
       "problems": ["the log shrank from 12 to 9 entries"]}


def test_first_check_comes_two_minutes_after_start_then_daily(tmp_path):
    w, calls, clock = witness(tmp_path, [OK])
    assert not w.due()
    first = lw.FIRST_CHECK_SECONDS + 0.5 * lw.FIRST_CHECK_SPREAD
    clock.t = first - 1
    assert not w.due()
    clock.t = first
    assert w.due()
    rec = w.run()
    assert rec["status"] == "ok" and rec["size"] == 12 and rec["epoch"] == 2 and not rec["notice"]
    assert calls == ["https://r.example"]
    clock.t += lw.CHECK_EVERY_SECONDS - 1
    assert not w.due()
    clock.t += 1
    assert w.due()


def test_the_record_holds_words_and_numbers_never_key_material(tmp_path):
    w, _, clock = witness(tmp_path, [BAD])
    clock.t = 1000
    w.run()
    raw = (tmp_path / "plugin-data" / "emporia" / lw.RECORD).read_text()
    assert "SECRET-SIG" not in raw and '"root"' not in raw
    data = json.loads(raw)
    assert {"status", "size", "epoch", "checked_at", "problems"} <= set(data)


def test_a_relay_with_no_log_is_not_a_problem(tmp_path):
    off = {"ok": True, "status": "off", "detail": "this relay keeps no transparency log", "state": None,
           "problems": []}
    w, _, clock = witness(tmp_path, [off])
    clock.t = 1000
    rec = w.run()
    assert rec["ok"] and not rec["notice"] and rec["problems"] == []
    assert lw.line(rec, NOW) == "Log checked just now: off on this relay"


def test_an_unreachable_relay_is_quiet_and_tried_again_within_the_hour(tmp_path):
    down = {"ok": False, "status": "unreachable", "detail": "could not reach", "problems": ["could not fetch"]}
    w, _, clock = witness(tmp_path, [down, OK])
    clock.t = 1000
    rec = w.run()
    assert not rec["notice"] and rec["problems"] == []
    clock.t += lw.RETRY_SECONDS
    assert w.due()
    # a check that raises is the same: quiet
    w2, _, c2 = witness(tmp_path / "b", [RuntimeError("boom")])
    c2.t = 1000
    assert not w2.run()["notice"]


def test_a_problem_is_one_notice_and_not_repeated(tmp_path):
    again = {**BAD, "problems": ["the log shrank from 12 to 7 entries"]}      # same problem, other numbers
    w, _, clock = witness(tmp_path, [BAD, again, again, BAD])
    clock.t = 1000
    first = w.run()
    assert first["notice"] and not first["ok"]
    for _ in range(3):
        clock.t += lw.CHECK_EVERY_SECONDS
        assert not w.run()["notice"]
    # the record survives a restart: a new Witness on the same file does not tell it again
    w2 = lw.Witness("https://r.example", w.path, verify=lambda r: BAD, clock=clock, now=lambda: NOW,
                    rng=lambda: 0.5)
    clock.t += lw.CHECK_EVERY_SECONDS
    assert not w2.run()["notice"]


def test_a_different_problem_is_told_again(tmp_path):
    other = {**BAD, "problems": ["https://x.example was shown another history: two different logs"]}
    w, _, clock = witness(tmp_path, [BAD, other])
    clock.t = 1000
    assert w.run()["notice"]
    clock.t += lw.CHECK_EVERY_SECONDS
    assert w.run()["notice"]


def test_status_line_words(tmp_path):
    w, _, clock = witness(tmp_path, [OK, BAD])
    clock.t = 1000
    assert lw.line(w.run(), NOW) == "Log checked just now: OK, 12 entries"
    clock.t += lw.CHECK_EVERY_SECONDS
    assert "PROBLEM: the log shrank" in lw.line(w.run(), NOW)
    assert lw.line({}, NOW).startswith("Log not checked yet")


def test_setting_is_on_unless_the_person_turns_it_off():
    assert lw.enabled({}) and lw.enabled({lw.SETTING: "on"})
    assert not lw.enabled({lw.SETTING: "off"}) and not lw.enabled({lw.SETTING: "0"})


def test_the_bridge_writes_one_fixed_notice_for_a_problem(tmp_path):
    w, _, clock = witness(tmp_path, [BAD])
    clock.t = 1000
    out = io.StringIO()
    asyncio.run(bridge.witness_pass(w, out, "my_agent"))
    lines = out.getvalue().splitlines()
    assert len(lines) == 1
    msg = json.loads(lines[0])
    assert msg["frame_type"] == "log_check" and msg["sender"] == "emporia:relay"
    assert msg["text"].startswith("Your relay's transparency log did not check out: it may have rewritten")
    assert "log_states.json" in msg["text"] and "emporia log verify my_agent" in msg["text"]
    clock.t += lw.CHECK_EVERY_SECONDS
    asyncio.run(bridge.witness_pass(w, out, "my_agent"))
    assert len(out.getvalue().splitlines()) == 1


def test_the_bridge_says_nothing_when_all_is_well(tmp_path):
    w, _, clock = witness(tmp_path, [OK])
    clock.t = 1000
    out = io.StringIO()
    asyncio.run(bridge.witness_pass(w, out, "my_agent"))
    assert out.getvalue() == ""


def test_the_bridge_builds_no_witness_when_the_person_turned_it_off(monkeypatch, tmp_path):
    monkeypatch.setenv(lw.SETTING, "off")
    assert bridge.build_witness("https://r.example", str(tmp_path)) is None
    monkeypatch.setenv(lw.SETTING, "on")
    monkeypatch.delenv("EMPORIA_RELAY_URL", raising=False)
    w = bridge.build_witness("https://r.example/", str(tmp_path))
    assert w.path == tmp_path / "plugin-data" / "emporia" / lw.RECORD and w.relay == "https://r.example"


def _notice(tmp_path, result, name="my_agent"):
    w, _, clock = witness(tmp_path, [result])
    clock.t = 1000
    return json.loads(lw.notice_line(name, w.run()))["text"]


def test_the_notice_text_follows_the_kind_of_failure(tmp_path):
    key = {**BAD, "problems": ["the relay signs with a key you never pinned, and no signed hand-over leads to it"]}
    stopped = {**BAD, "status": "stopped", "problems": ["this relay no longer serves the transparency log it kept"]}
    assert "signing key changed without a signed hand-over" in _notice(tmp_path / "a", key)
    t = _notice(tmp_path / "b", stopped)
    assert "stopped serving the log it kept" in t and "rewritten" not in t
    assert "may have rewritten" in _notice(tmp_path / "c", BAD)
    assert set(json.loads(lw.notice_line("x", {"problems": ["p"]}))) >= {"v", "text", "sender"}


def test_unreachable_keeps_an_earlier_problem(tmp_path):
    down = {"ok": False, "status": "unreachable", "detail": "x", "problems": []}
    w, _, clock = witness(tmp_path, [BAD, down])
    clock.t = 1000
    w.run()
    clock.t += lw.CHECK_EVERY_SECONDS
    rec = w.run()
    assert rec["status"] == "unreachable" and rec["problems"] == BAD["problems"] and not rec["notice"]
    assert rec["size"] == 12 and rec["epoch"] == 2
    assert "PROBLEM: the log shrank" in lw.line(rec, NOW)


def test_a_problem_that_clears_and_returns_is_noticed_again(tmp_path):
    w, _, clock = witness(tmp_path, [BAD, BAD, OK, BAD])
    notices = []
    for _ in range(4):
        clock.t += lw.CHECK_EVERY_SECONDS * 2
        notices.append(w.run()["notice"])
    assert notices == [True, False, False, True]


def test_checks_are_spread_by_a_random_source(tmp_path):
    w, _, clock = witness(tmp_path, [OK, OK], rng=lambda: 0.0)
    assert w.next_at == lw.FIRST_CHECK_SECONDS
    assert witness(tmp_path / "b", [OK], rng=lambda: 1.0)[0].next_at == lw.FIRST_CHECK_SECONDS + lw.FIRST_CHECK_SPREAD
    clock.t = 1000
    w.run()
    assert w.next_at == 1000 + lw.CHECK_EVERY_SECONDS * 0.8
    w._rng = lambda: 1.0
    w.run()
    assert w.next_at == 1000 + lw.CHECK_EVERY_SECONDS * 1.2
    down = {"ok": False, "status": "unreachable", "detail": "x", "problems": []}
    w2, _, c2 = witness(tmp_path / "c", [down], rng=lambda: 1.0)
    w2.run()
    assert w2.next_at == lw.RETRY_SECONDS * 1.1


def test_a_lost_stored_head_is_said_in_the_status_line(tmp_path):
    first = {"ok": True, "status": "first", "detail": "d", "state": {"size": 12, "epoch": 2}, "problems": []}
    w, _, clock = witness(tmp_path, [OK, first])
    clock.t = 1000
    w.run()
    clock.t += lw.CHECK_EVERY_SECONDS
    rec = w.run()
    assert not rec["notice"]
    assert lw.line(rec, NOW).endswith(lw.FIRST_AGAIN) and "stored head was missing" in lw.line(rec, NOW)
    # a first look with no earlier OK is just a first look
    w2, _, c2 = witness(tmp_path / "b", [first])
    c2.t = 1000
    assert "stored head" not in lw.line(w2.run(), NOW)
