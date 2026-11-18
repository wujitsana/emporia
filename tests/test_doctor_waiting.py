"""Doctor says when a reply has waited more than 30 minutes and no round will read it."""
import json
from pathlib import Path
from types import SimpleNamespace

from emporia import cli


class _Rep:
    def __init__(self):
        self.warned = []

    def warn(self, text, fix=""):
        self.warned.append((text, fix))

    def ok(self, text):
        pass


def _answer(monkeypatch, wait):
    monkeypatch.setattr(cli, "_as_agent", lambda profile, code, **k: SimpleNamespace(
        stdout=json.dumps({"success": True, "wait_s": wait}) + "\n", stderr="", returncode=0))


def test_a_reply_waiting_over_half_an_hour_with_no_round_is_a_warning(monkeypatch):
    _answer(monkeypatch, 3 * 3600)
    rep = _Rep()
    cli.doctor_waiting_replies(rep, Path("/x/profiles/scout"), "off")
    assert len(rep.warned) == 1 and "180 minutes" in rep.warned[0][0] and "rounds on" in rep.warned[0][1]


def test_nothing_to_say_when_rounds_run_the_wait_is_short_or_unknown(monkeypatch):
    for state, wait in (("enabled, every 4h (job abc)", 99999), ("off", 600), ("off", None)):
        _answer(monkeypatch, wait)
        rep = _Rep()
        cli.doctor_waiting_replies(rep, Path("/x/profiles/scout"), state)
        assert rep.warned == [], (state, wait)


def test_the_relay_says_how_long_the_oldest_reply_has_waited(client):
    import sys

    sys.path.insert(0, str(Path(__file__).parent))
    from test_whats_next import _agents, _inbox, as_
    me, other = _agents(client, "wait", "wother")
    assert client.get(f"/agents/{me}/next", headers=as_(me)).json()["inbox"]["oldest_reply_wait_s"] is None
    _inbox(me, "dm_received", {"thread_id": "dm_w", "sender_id": other, "content": "hi"})
    got = client.get(f"/agents/{me}/next", headers=as_(me)).json()["inbox"]["oldest_reply_wait_s"]
    assert isinstance(got, int) and 0 <= got < 60
