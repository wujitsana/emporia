"""P297: an agent's playbook — what it may do on its own, as its person set it."""
import argparse

import pytest

from emporia import mcp_server as m
from emporia import playbook as P


def test_custom_adds_no_bounds_and_is_the_default():
    pb = P.resolve({})
    assert pb == P.PRESETS["custom"] and pb.games == P.ALL and pb.sells and pb.buys


def test_a_preset_with_the_persons_changes_on_top():
    pb = P.resolve({"EMPORIA_PLAYBOOK": "player", "EMPORIA_PLAYBOOK_GAMES": "blotto,chess",
                    "EMPORIA_PLAYBOOK_MAX_STAKE_CENTS": "250", "EMPORIA_PLAYBOOK_SELLS": "yes"})
    assert pb.games == ("emporia:blotto:v1", "emporia:chess:v1") and pb.max_stake_cents == 250 and pb.sells
    assert P.resolve({"EMPORIA_PLAYBOOK": "player", "EMPORIA_PLAYBOOK_GAMES": "set-plays"}).games == P.SET_PLAYS


def test_a_changed_preset_says_what_was_changed():
    """Found by the end-to-end run: a merchant given set plays still read
    "sells its listings; plays and buys nothing" beside the games it takes."""
    assert P.about(P.PRESETS["merchant"]) == P.ABOUT["merchant"]
    pb = P.resolve({"EMPORIA_PLAYBOOK": "merchant", "EMPORIA_PLAYBOOK_GAMES": "set-plays"})
    assert "plays" not in P.about(pb) and "merchant" in P.about(pb) and "games" in P.about(pb)


@pytest.mark.parametrize("env", [{"EMPORIA_PLAYBOOK": "wizard"}, {"EMPORIA_PLAYBOOK_SELLS": "maybe"},
                                 {"EMPORIA_PLAYBOOK_MAX_STAKE_CENTS": "lots"}, {"EMPORIA_PLAYBOOK_ANSWERS": "friends"}])
def test_an_unreadable_setting_is_an_error_and_bounds_everything(env, monkeypatch):
    with pytest.raises(P.PlaybookError):
        P.resolve(env)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    pb = P.current()
    assert pb.games == P.NONE and not pb.sells and not pb.buys and pb.answers == "nobody"


def test_what_a_player_may_play():
    pb = P.PRESETS["player"]
    assert P.may_play(pb, "emporia:blotto:v1", 100) == ""
    assert "up to $1.00" in P.may_play(pb, "emporia:blotto:v1", 500)
    assert "does not take" in P.may_play(pb, "emporia:chess:v1")
    assert P.may_sell(pb) and P.may_buy(pb)


def test_the_bridge_wakes_only_for_what_the_playbook_takes():
    quiet = P.Playbook("x", P.NONE, None, "nobody", False, False, "model")
    assert not P.wakes_for(quiet, {"type": "dm_received"})
    assert not P.wakes_for(quiet, {"type": "challenge", "payload": {"session_id": "s"}})
    assert P.wakes_for(quiet, {"type": "your_turn"}), "a game already under way always wakes it"
    player = P.PRESETS["player"]
    assert not P.wakes_for(player, {"type": "challenge", "payload": {"module_type": "emporia:chess:v1"}})
    assert not P.wakes_for(player, {"type": "challenge", "payload": {"module_type": "emporia:blotto:v1",
                                                                       "stake_per_participant": "5.00"}})
    assert P.wakes_for(player, {"type": "challenge", "payload": {"session_id": "s"}}), "join_session checks it"


def test_the_tools_refuse_what_the_playbook_does_not_take(monkeypatch):
    monkeypatch.setenv("EMPORIA_PLAYBOOK", "player")
    monkeypatch.setattr(m, "AGENT_ID", "pb_me")
    def no_relay(*a, **k):
        raise AssertionError("refused before the relay is asked")
    monkeypatch.setattr(m, "_bearer_request", no_relay)
    assert "does not sell" in m.create_listing("a service")["error"]
    assert "does not take" in m.create_session("emporia:chess:v1")["error"]
    assert "up to $1.00" in m.create_session("emporia:blotto:v1", stake_per_participant="2.00")["error"]
    assert "does not buy" in m.create_session("emporia:service:v1")["error"]
    assert "does not buy" in m.post_bounty("t", "emporia:blotto:v1", 100, "2026-12-01T00:00:00Z")["error"]


def test_joining_checks_the_sessions_game_and_stake(monkeypatch):
    monkeypatch.setenv("EMPORIA_PLAYBOOK", "player")
    monkeypatch.setattr(m, "AGENT_ID", "pb_me")

    class R:
        def __init__(self, d):
            self._d = d

        def json(self):
            return self._d
    session = {"module_type": "emporia:blotto:v1", "payment_rules": {"stake_per_participant": "3.00"}}
    monkeypatch.setattr(m, "_bearer_request", lambda method, url, **k: R(session))
    assert "up to $1.00" in m.join_session("s1")["error"]
    session["module_type"] = "emporia:service:v1"
    assert "does not sell" in m.join_session("s1")["error"], "providing someone's purchase is selling"


@pytest.mark.parametrize("answer, said", [
    (404, "no session s1 on this relay"),
    (403, "cannot tell whether it is a game"),
    (500, "the relay answered 500"),
    (ConnectionError("down"), "could not load session s1"),
])
def test_a_session_that_cannot_be_read_is_not_joined_as_a_purchase(monkeypatch, answer, said):
    """Review follow-up L9: a failed read of the session left no module, so the playbook took
    the join for a sale: a `player` was told it "does not sell", about a game it could not see."""
    monkeypatch.setenv("EMPORIA_PLAYBOOK", "player")
    monkeypatch.setattr(m, "AGENT_ID", "pb_me")
    sent = []

    class R:
        status_code = answer

        def json(self):
            return {"detail": "nope"}

    def relay(method, url, **k):
        sent.append((method, url))
        if isinstance(answer, Exception):
            raise answer
        return R()
    monkeypatch.setattr(m, "_bearer_request", relay)
    out = m.join_session("s1")
    assert out["success"] is False and said in out["error"] and "does not sell" not in out["error"], out
    assert [s for s in sent if s[0] == "post"] == [], "no join was sent"


def test_without_playbook_bounds_a_session_shown_to_its_players_only_can_still_be_joined(monkeypatch):
    """A paid service shows itself to its participants only, so its provider-to-be reads 403;
    with no bounds to apply (`custom`), the relay decides the join, as before."""
    monkeypatch.delenv("EMPORIA_PLAYBOOK", raising=False)
    monkeypatch.setattr(m, "AGENT_ID", "pb_me")
    posted = []

    class R:
        def __init__(self, code, body):
            self.status_code, self._b, self.text = code, body, ""

        def json(self):
            return self._b

    def relay(method, url, **k):
        if method == "post":
            posted.append(url)
            return R(200, {"session_id": "s1", "participants": ["buyer", "pb_me"]})
        return R(403, {"detail": "Not a participant in this session"})
    monkeypatch.setattr(m, "_bearer_request", relay)
    m.join_session("s1")
    assert any(u.endswith("/sessions/s1/join") for u in posted), posted


def test_my_settings_shows_it(monkeypatch):
    monkeypatch.setenv("EMPORIA_PLAYBOOK", "merchant")
    monkeypatch.setattr(m, "_bearer_request", lambda *a, **k: (_ for _ in ()).throw(RuntimeError()))
    view = m.my_settings(agent_id="pb_me")["playbook"]
    assert view["preset"] == "merchant" and view["sells"] and not view["buys"] and view["about"]


def test_the_person_sets_it_with_a_command(tmp_path, monkeypatch, capsys):
    from emporia import cli
    profile = tmp_path / "shop"
    profile.mkdir()
    (profile / ".env").write_text("EMPORIA_AGENT_ID=shop\n")
    monkeypatch.setattr(cli, "_agent", lambda name: (profile, "shop", {"EMPORIA_AGENT_ID": "shop"}))
    monkeypatch.setattr(cli, "_TTY", False)
    a = argparse.Namespace(preset="player", games=None, max_stake="250", answers=None, sells=None, buys=None,
                           runs=None)
    assert cli.cmd_agent_playbook("shop", a) == 0
    env = cli.installer().harvest_env(profile)
    assert env["EMPORIA_PLAYBOOK"] == "player" and env["EMPORIA_PLAYBOOK_MAX_STAKE_CENTS"] == "250"
    assert "stakes up to $2.50" in capsys.readouterr().out
    bad = argparse.Namespace(preset="", games=None, max_stake="lots", answers=None, sells=None, buys=None, runs=None)
    assert cli.cmd_agent_playbook("shop", bad) == 1
    assert cli.installer().harvest_env(profile)["EMPORIA_PLAYBOOK_MAX_STAKE_CENTS"] == "250", "nothing written"


def test_an_invite_to_a_game_says_its_game_and_stake():
    player = P.PRESETS["player"]
    assert P.wakes_for(player, {"type": "session_invite", "module_type": "emporia:blotto:v1", "stake_cents": 0})
    assert not P.wakes_for(player, {"type": "session_invite", "module_type": "emporia:chess:v1"})
    assert not P.wakes_for(player, {"type": "session_invite", "module_type": "emporia:blotto:v1", "stake_cents": 500})


def test_a_fresh_mcp_process_knows_which_sessions_are_games():
    """Found by the end-to-end run: in an agent's own MCP process no game module
    was imported yet, so every game read as a purchase and a player making a
    blotto game was refused with "does not buy"."""
    import os
    import subprocess
    import sys
    code = ("from emporia import mcp_server as m, module_sdk; "
            "assert 'emporia:blotto:v1' not in module_sdk.MODULE_REGISTRY; "
            "m._bearer_request = lambda *a, **k: (_ for _ in ()).throw(AssertionError('no relay')); "
            "print(m.create_session('emporia:chess:v1', agent_id='pb_me')['error']); "
            "print(m.create_session('emporia:blotto:v1', agent_id='pb_me', stake_per_participant='2.00')['error'])")
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60,
                       env={**os.environ, "EMPORIA_PLAYBOOK": "player"})
    chess, blotto = r.stdout.strip().splitlines()[-2:] if r.stdout.strip() else ("", "")
    assert "does not take emporia:chess:v1" in chess, r.stderr[-800:]
    assert "up to $1.00" in blotto, r.stderr[-800:]


def test_the_stake_limit_holds_to_the_stake_its_creator_signed(monkeypatch):
    """Security follow-up (11-05 audit): the playbook's stake limit read the relay's unsigned
    copy of a game's stake, while payment signs for the creator's signed one (P280). A relay
    showing $0.50 for a game its creator signed at $3.00 got a $1.00-limit player past the
    playbook, into a game whose hold is signed for $3.00. The limit now checks the signed
    stake, and a game whose relay copy disagrees with it is refused."""
    import json

    from fastapi.testclient import TestClient

    from emporia import relay_server as rs
    from emporia.relay_server import app
    from tests.test_emporia import _publish_enc_key, _register_keyed
    client = TestClient(app, client=("127.0.0.1", 51298))
    for a in ("pbs_maker", "pbs_joiner"):
        _register_keyed(client, a)
        _publish_enc_key(client, a)

    def game(stake: str) -> str:
        r = client.post("/sessions", json={"module_type": "emporia:blotto:v1", "payment_rules": {
            "stake_per_participant": stake, "currency": "USD"}},
            headers={"x-emporia-agent-id": "pbs_maker"})
        assert r.status_code == 200, r.text
        return r.json()["session_id"]

    def keep_stake(sid: str, stake: str) -> None:   # the relay changes the stake it keeps
        with rs.get_db() as conn:
            row = conn.execute("SELECT payment_rules_json FROM sessions WHERE session_id = ?", (sid,)).fetchone()
            conn.execute("UPDATE sessions SET payment_rules_json = ? WHERE session_id = ?",
                         (json.dumps({**json.loads(row[0]), "stake_per_participant": stake}), sid))

    monkeypatch.setenv("EMPORIA_PLAYBOOK", "player")       # stakes up to $1.00
    monkeypatch.setattr(m, "AGENT_ID", "pbs_joiner")
    monkeypatch.setattr(m, "_open_games_refusal", lambda aid, mt: None)
    monkeypatch.setattr(m, "_bearer_request", lambda method, url, agent_id="", **kw: getattr(client, method)(
        "/" + url.split("/", 3)[3], headers={"x-emporia-agent-id": agent_id or "pbs_joiner"},
        **{k: v for k, v in kw.items() if k in ("json", "params")}))

    lowered = game("3.00")
    keep_stake(lowered, "0.50")
    for out in (m.join_session(lowered), m.fund_session(lowered), m.create_payment_intent(50, session_id=lowered)):
        assert not out["success"] and out.get("playbook") == "player", out
        assert "signed" in out["error"], out["error"]
    assert "up to $1.00" in m.join_session(game("3.00"))["error"], "the signed stake, as shown, is over the limit"
    fine = m.join_session(game("0.50"))
    assert "playbook" not in fine, fine
