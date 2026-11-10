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
                    "EMPORIA_PLAYBOOK_SELLS": "yes"})
    assert pb.games == ("emporia:blotto:v2", "emporia:chess:v1") and pb.sells
    assert P.resolve({"EMPORIA_PLAYBOOK": "player", "EMPORIA_PLAYBOOK_GAMES": "set-plays"}).games == P.SET_PLAYS


def test_a_changed_preset_says_what_was_changed():
    """Found by the end-to-end run: a merchant given set plays still read
    "sells its listings; plays and buys nothing" beside the games it takes."""
    assert P.about(P.PRESETS["merchant"]) == P.ABOUT["merchant"]
    pb = P.resolve({"EMPORIA_PLAYBOOK": "merchant", "EMPORIA_PLAYBOOK_GAMES": "set-plays"})
    assert "plays" not in P.about(pb) and "merchant" in P.about(pb) and "games" in P.about(pb)


@pytest.mark.parametrize("env", [{"EMPORIA_PLAYBOOK": "wizard"}, {"EMPORIA_PLAYBOOK_SELLS": "maybe"},
                                 {"EMPORIA_PLAYBOOK_ANSWERS": "friends"}])
def test_an_unreadable_setting_is_an_error_and_bounds_everything(env, monkeypatch):
    with pytest.raises(P.PlaybookError):
        P.resolve(env)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    pb = P.current()
    assert pb.games == P.NONE and not pb.sells and not pb.buys and pb.answers == "nobody"


def test_what_a_player_may_play():
    pb = P.PRESETS["player"]
    assert P.may_play(pb, "emporia:blotto:v2") == ""
    assert "does not take" in P.may_play(pb, "emporia:chess:v1")
    assert P.may_sell(pb) and P.may_buy(pb)


def test_the_bridge_wakes_only_for_what_the_playbook_takes():
    quiet = P.Playbook("x", P.NONE, "nobody", False, False, "model")
    assert not P.wakes_for(quiet, {"type": "dm_received"})
    assert not P.wakes_for(quiet, {"type": "challenge", "payload": {"session_id": "s"}})
    assert P.wakes_for(quiet, {"type": "your_turn"}), "a game already under way always wakes it"
    player = P.PRESETS["player"]
    assert not P.wakes_for(player, {"type": "challenge", "payload": {"module_type": "emporia:chess:v1"}})
    assert P.wakes_for(player, {"type": "challenge", "payload": {"session_id": "s"}}), "join_session checks it"


def test_the_tools_refuse_what_the_playbook_does_not_take(monkeypatch):
    monkeypatch.setenv("EMPORIA_PLAYBOOK", "player")
    monkeypatch.setattr(m, "AGENT_ID", "pb_me")
    def no_relay(*a, **k):
        raise AssertionError("refused before the relay is asked")
    monkeypatch.setattr(m, "_bearer_request", no_relay)
    assert "does not take" in m.challenge("emporia:chess:v1")["error"]
    assert "does not buy" in m.create_session("emporia:service:v1")["error"]


def test_joining_checks_the_sessions_game(monkeypatch):
    monkeypatch.setenv("EMPORIA_PLAYBOOK", "player")
    monkeypatch.setattr(m, "AGENT_ID", "pb_me")

    class R:
        def __init__(self, d):
            self._d = d

        def json(self):
            return self._d
    session = {"module_type": "emporia:chess:v1"}
    monkeypatch.setattr(m, "_bearer_request", lambda method, url, **k: R(session))
    assert "does not take" in m.join_session("s1")["error"]
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
    a = argparse.Namespace(preset="player", games=None, answers=None, sells=None, buys=None,
                           runs=None)
    assert cli.cmd_agent_playbook("shop", a) == 0
    env = cli.installer().harvest_env(profile)
    assert env["EMPORIA_PLAYBOOK"] == "player" and "EMPORIA_PLAYBOOK_MAX_STAKE_CENTS" not in env
    assert "stake" not in capsys.readouterr().out.lower()
    no_pledges = argparse.Namespace(preset="", games=None, answers=None, sells=None, buys=None,
                                    runs=None, pledges="no")
    assert cli.cmd_agent_playbook("shop", no_pledges) == 0
    assert cli.installer().harvest_env(profile)["EMPORIA_PLAYBOOK_PLEDGES"] == "no"
    assert "Pledges no" in capsys.readouterr().out
    args = cli.build_parser().parse_args(["agent", "playbook", "shop", "--pledges", "yes"])
    assert args.pledges == "yes"
    bad = argparse.Namespace(preset="", games="nonsense", answers="friends", sells=None, buys=None, runs=None)
    assert cli.cmd_agent_playbook("shop", bad) == 1
    assert cli.installer().harvest_env(profile)["EMPORIA_PLAYBOOK_ANSWERS"] != "friends", "nothing written"


def test_an_invite_to_a_game_says_its_game():
    player = P.PRESETS["player"]
    assert P.wakes_for(player, {"type": "session_invite", "module_type": "emporia:blotto:v2"})
    assert not P.wakes_for(player, {"type": "session_invite", "module_type": "emporia:chess:v1"})


def test_a_fresh_mcp_process_knows_which_sessions_are_games():
    """Found by the end-to-end run: in an agent's own MCP process no game module
    was imported yet, so every game read as a purchase and a player making a
    blotto game was refused with "does not buy"."""
    import os
    import subprocess
    import sys
    code = ("from emporia import mcp_server as m, module_sdk; "
            "assert 'emporia:blotto:v2' not in module_sdk.MODULE_REGISTRY; "
            "m._bearer_request = lambda *a, **k: (_ for _ in ()).throw(AssertionError('no relay')); "
            "print(m._create_session('emporia:chess:v1', agent_id='pb_me')['error']); "
            "print(m.create_session('emporia:service:v1', agent_id='pb_me')['error'])")
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60,
                       env={**os.environ, "EMPORIA_PLAYBOOK": "player"})
    chess, service = r.stdout.strip().splitlines()[-2:] if r.stdout.strip() else ("", "")
    assert "does not take emporia:chess:v1" in chess, r.stderr[-800:]
    assert "does not buy" in service, r.stderr[-800:]


