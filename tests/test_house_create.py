"""`emporia house create`: the two kinds, who may list the house, the closing block, and the double-play guard.

The guard is the point: a persona house has a rules runner and a model on one profile, and only the runner may
make a move in a game (EMPORIA_GAMES_BY=runner, playbook.py).
"""
import argparse
from pathlib import Path

import pytest

from emporia import cli, house_create as HC, mcp_server as m, playbook as P, setup_choices as sc

GAME = "emporia:blotto:v2"


# ── the question ─────────────────────────────────────────────────────────────

def test_one_question_with_one_recommended_kind():
    c = sc.CHOICES["house_kind"]
    assert [o.value for o in c.options] == list(HC.KINDS) and c.default == "rules"
    assert c.recommended() == {"rules"}
    assert "free" in c.options[0].effect and "tokens" in c.options[1].effect


def test_the_kinds_set_what_they_cost():
    assert HC.settings_for("rules") == {"wake": "closed", "rounds": "off"}
    assert HC.settings_for("persona") == {"wake": "open", "rounds": "8h"}
    assert HC.settings_for("persona", "2h")["rounds"] == "2h"
    argv = HC.setup_argv("h", "persona")
    assert argv[argv.index("--playbook") + 1] == "host" and "--register" in argv
    assert argv[argv.index("--wake") + 1] == "open" and argv[argv.index("--rounds") + 1] == "8h"
    assert HC.ABOUT and len(HC.ABOUT) <= 160


def test_the_parser_takes_create_and_its_flags():
    a = cli.build_parser().parse_args(["house", "create", "h", "--kind", "persona", "--rounds", "4h", "--start",
                                       "--dir", "/r", "--operator", "op", "--relay", "http://r"])
    assert a.action == "create" and a.kind == "persona" and a.start and a.dir == "/r" and a.operator == "op"
    assert cli.build_parser().parse_args(["house", "run", "h"]).kind == ""


# ── the command, with the profile steps faked ────────────────────────────────

@pytest.fixture
def made(tmp_path, monkeypatch):
    """A house profile that exists; setup and the relay are faked, and what is asked is recorded."""
    profile = tmp_path / "h"
    profile.mkdir()
    seen = {"setup": [], "operator": [], "env": {}, "refresh": 0}

    class Inst:
        def harvest_env(self, p):
            return dict(seen["env"])

        def refresh_mcp_references(self, p):
            seen["refresh"] += 1

    monkeypatch.setattr(HC, "make_profile", lambda cli_, name, kind, relay, rounds: seen["setup"].append(
        (name, kind, relay, rounds)) or 0)
    monkeypatch.setattr(cli, "_agent", lambda name: (profile, name, {"EMPORIA_AGENT_ID": name}))
    monkeypatch.setattr(cli, "installer", lambda: Inst())
    monkeypatch.setattr(cli, "merge_env_file", lambda path, updates: seen["env"].update(updates))
    monkeypatch.setattr(HC, "operator_step", lambda *a: seen["operator"].append(a) or (False, "not listed: test"))
    monkeypatch.setattr(cli, "_TTY", False)
    return profile, seen


def _args(**kw):
    base = dict(action="create", name="h", kind="", relay="http://r", rounds="", dir="", operator="", start=False)
    return argparse.Namespace(**{**base, **kw})


def test_no_terminal_means_the_recommended_kind_rules_only(made, capsys):
    profile, seen = made
    assert HC.run(_args()) == 0
    assert seen["setup"] == [("h", "rules", "http://r", "off")]
    assert seen["env"] == {} and not (profile / "SOUL.md").exists(), "rules only: no persona files, no persona settings"
    out = capsys.readouterr().out
    assert "House agent h (rules)" in out and "Undo or change" in out and "Guide: docs/create-an-agent.md" in out
    assert "no model, no tokens" in out


def test_a_persona_house_gets_the_games_guard_a_soul_and_rounds(made, capsys):
    profile, seen = made
    assert HC.run(_args(kind="persona")) == 0
    assert seen["setup"] == [("h", "persona", "http://r", "8h")]
    assert seen["env"] == {"EMPORIA_PLAYBOOK_RUNS": "model", "EMPORIA_GAMES_BY": "runner"}
    assert seen["refresh"] == 1, "the MCP entry is told to hand the new setting over"
    soul = (profile / "SOUL.md").read_text()
    assert "You are h," in soul and "{{" not in soul and "Never trade, buy, sell or send funds" in soul
    out = capsys.readouterr().out
    assert "SOUL.md" in out and "EMPORIA_GAMES_BY=runner" in out and "emporia rounds off" in out


def test_a_persons_own_soul_is_kept(made):
    profile, seen = made
    (profile / "SOUL.md").write_text("I am mine.\n")
    assert HC.write_soul(profile, "h") == "kept" and (profile / "SOUL.md").read_text() == "I am mine.\n"
    (profile / "SOUL.md").write_text("  \n")
    assert HC.write_soul(profile, "h") == "written"


def test_going_back_to_rules_clears_the_persona_settings(made):
    profile, seen = made
    seen["env"].update({"EMPORIA_PLAYBOOK_RUNS": "model", "EMPORIA_GAMES_BY": "runner"})
    assert HC.run(_args(kind="rules")) == 0
    assert seen["env"] == {"EMPORIA_PLAYBOOK_RUNS": "", "EMPORIA_GAMES_BY": ""}


def test_rules_only_has_no_rounds_and_a_bad_name_is_refused(made, capsys):
    assert HC.run(_args(kind="rules", rounds="4h")) == 2
    assert HC.run(_args(name="Not A Name")) == 2 and HC.run(_args(name="default")) == 2
    assert "no rounds" in capsys.readouterr().out


def test_a_failed_setup_stops_before_anything_else(made, monkeypatch, capsys):
    profile, seen = made
    monkeypatch.setattr(HC, "make_profile", lambda *a: 3)
    assert HC.run(_args(kind="persona")) == 3
    assert seen["env"] == {} and not seen["operator"] and not (profile / "SOUL.md").exists()


def test_start_starts_the_runner_and_the_block_says_so(made, monkeypatch, capsys):
    started = []
    monkeypatch.setattr(cli, "cmd_house", lambda a: started.append(a.action) or 0)
    assert HC.run(_args(start=True)) == 0
    assert started == ["start"] and "running: emporia house status h" in capsys.readouterr().out


def test_the_closing_block_gives_an_undo_for_every_line():
    for kind in HC.KINDS:
        lines = HC.closing_lines("h", kind, "http://r", "off", (False, "not listed"), "written", False)
        body = "\n".join(lines)
        rows = [ln for ln in lines if ln.startswith("  ") and not ln.startswith("             ")
                and not ln.startswith(("  Remove", "  Guide"))]
        assert rows and body.count("Undo or change:") == len(rows)
        assert "Guide: docs/create-an-agent.md" in body and "emporia agent delete h" in body
        assert ("SOUL.md" in body) == (kind == "persona")


# ── who may list the house ───────────────────────────────────────────────────

class _Fake:
    """The pieces of emporia.cli operator_step reads, as a person's terminal on the relay's machine."""
    def __init__(self, tmp_path, *, person=True, env_exists=True, relay_env=None, local=True, operator_ok=True,
                 config_code=0):
        self.env = tmp_path / "relay" / ".env"
        self.calls, self.person, self.local = [], person, local
        if env_exists:
            self.env.parent.mkdir(parents=True, exist_ok=True)
            self.env.write_text("")
        self.held = relay_env or {}
        self.operator_ok, self.config_code = operator_ok, config_code

    def _persons_terminal(self):
        return self.person

    def _relay_env_path(self, args):
        return self.env

    def installer(self):
        outer = self

        class I:
            def harvest_env(self, folder):
                return dict(outer.held)

            def _is_local_relay(self, url):
                return outer.local
        return I()

    def _agent(self, name):
        return tmp_path_of(self), name, {"EMPORIA_AGENT_ID": name}

    def _operator_call(self, profile, method, path, body=None, params=None):
        return 200, {"is_operator": self.operator_ok}

    def cmd_relay_config(self, a):
        self.calls.append((a.url, a.names, a.dir))
        return self.config_code


def tmp_path_of(fake):
    return fake.env.parent


COMMAND = "emporia relay config set EMPORIA_HOUSE_STABLE_IDS hh"


def test_the_relays_operator_lists_the_house(tmp_path):
    f = _Fake(tmp_path, relay_env={"EMPORIA_HOUSE_STABLE_IDS": "ab" * 32})
    done, said = HC.operator_step(f, "hh", "hh", "http://127.0.0.1:8088", "", "")
    assert done and "emporia relay restart" in said
    assert f.calls == [("set", ["EMPORIA_HOUSE_STABLE_IDS", "ab" * 32 + ",hh"], "")], "the ids already listed are kept"


def test_anyone_else_is_told_the_one_command_the_operator_runs(tmp_path):
    for fake in (_Fake(tmp_path / "a", person=False), _Fake(tmp_path / "b", env_exists=False)):
        done, said = HC.operator_step(fake, "hh", "hh", "http://127.0.0.1:8088", "", "")
        assert not done and COMMAND in said and not fake.calls


def test_a_relay_settings_file_for_another_relay_is_not_used(tmp_path):
    other = _Fake(tmp_path, local=False)
    done, said = HC.operator_step(other, "hh", "hh", "https://relay.example", "", "")
    assert not done and COMMAND in said and "not for https://relay.example" in said and not other.calls
    hosted = _Fake(tmp_path, relay_env={"EMPORIA_RELAY_URL": "https://relay.example"})
    done, _ = HC.operator_step(hosted, "hh", "hh", "https://relay.example/", str(hosted.env.parent), "")
    assert done and hosted.calls[0][2] == str(hosted.env.parent)
    wrong = _Fake(tmp_path, relay_env={"EMPORIA_RELAY_URL": "https://elsewhere.example"})
    done, said = HC.operator_step(wrong, "hh", "hh", "https://relay.example", str(wrong.env.parent), "")
    assert not done and "--dir" in said


def test_naming_an_operator_agent_the_relay_does_not_confirm_lists_nothing(tmp_path):
    f = _Fake(tmp_path, operator_ok=False)
    done, said = HC.operator_step(f, "hh", "hh", "http://127.0.0.1:8088", "", "op")
    assert not done and "not this relay's operator" in said and not f.calls
    ok = _Fake(tmp_path)
    assert HC.operator_step(ok, "hh", "hh", "http://127.0.0.1:8088", "", "op")[0]


def test_a_relay_that_refuses_the_id_leaves_the_command_to_run_later(tmp_path):
    f = _Fake(tmp_path, config_code=1)
    done, said = HC.operator_step(f, "hh", "hh", "http://127.0.0.1:8088", "", "")
    assert not done and COMMAND in said and "registered" in said


# ── double play: the model never moves in a game on a persona house ──────────

GAME_FRAMES = [{"type": "your_turn", "module_type": GAME}, {"type": "session_started", "module_type": GAME},
               {"type": "session_invite", "module_type": "emporia:chess:v1"},
               {"type": "challenge", "payload": {"module_type": GAME}}, {"type": "challenge"}]


@pytest.mark.parametrize("frame", GAME_FRAMES)
def test_the_bridge_wakes_the_model_for_no_game_turn_where_the_runner_plays(frame):
    pb = P.PRESETS["host"]
    assert P.wakes_for(pb, frame, {"EMPORIA_GAMES_BY": "runner"}) is False
    assert P.wakes_for(pb, frame, {}) is True, "without the setting the playbook decides, as before"
    assert P.wakes_for(pb, frame, {"EMPORIA_GAMES_BY": ""}) is True, "the runner's own process: off"


def test_the_model_is_still_woken_to_talk_and_for_deals():
    pb = P.PRESETS["host"]
    env = {"EMPORIA_GAMES_BY": "runner"}
    for frame in ({"type": "dm_received"}, {"type": "room_invite"}, {"type": "game_chat"}, {"type": "post"},
                  {"type": "your_turn", "module_type": "emporia:service:v1"}):
        assert P.wakes_for(pb, frame, env), frame


def test_the_game_tools_refuse_the_model_and_only_the_model(monkeypatch):
    monkeypatch.setenv("EMPORIA_GAMES_BY", "runner")
    for what in ("play", "join", "create", "fund"):
        out = m._playbook_refusal(what, GAME)
        assert out and out["success"] is False and "rules runner" in out["error"], what
    assert m._playbook_refusal("create", "emporia:service:v1") is None or "rules runner" not in \
        m._playbook_refusal("create", "emporia:service:v1")["error"]
    assert m._playbook_refusal("fund") is None or "rules runner" not in m._playbook_refusal("fund")["error"]
    for tool in (lambda: m.commit_move("s1", [20] * 5, "spread evenly this time", agent_id="h"),
                 lambda: m.reveal_move("s1", "reveal what I committed", agent_id="h")):
        out = tool()
        assert out["success"] is False and "rules runner" in out["error"]
    monkeypatch.setenv("EMPORIA_GAMES_BY", "")             # the runner's process
    assert m._runner_only_refusal() is None
    assert "rules runner" not in (m._playbook_refusal("join", GAME) or {}).get("error", "")


def test_the_runner_is_started_with_the_guard_emptied(monkeypatch, tmp_path):
    """An explicit empty value beats the profile's .env, so the one process that plays is the one the guard spares."""
    seen = {}
    monkeypatch.setattr(cli, "_agent", lambda name: (tmp_path, "h", {"EMPORIA_AGENT_ID": "h"}))
    monkeypatch.setattr(cli, "_mcp_env", lambda p: {"EMPORIA_AGENT_ID": "h", "EMPORIA_GAMES_BY": "runner"})

    class Inst:
        def harvest_env(self, p):
            return {"EMPORIA_PLAYBOOK": "host"}
    monkeypatch.setattr(cli, "installer", lambda: Inst())
    monkeypatch.setattr(cli.subprocess, "call", lambda argv, env=None: seen.update(env=env, argv=argv) or 0)
    a = cli.build_parser().parse_args(["house", "run", "h", "--once"])
    assert cli.cmd_house(a) == 0
    assert seen["env"]["EMPORIA_GAMES_BY"] == "" and seen["env"]["EMPORIA_AGENT_ID"] == "h"
    from emporia import config
    import os
    monkeypatch.setenv("EMPORIA_GAMES_BY", "")
    envfile = tmp_path / ".env"
    envfile.write_text("EMPORIA_GAMES_BY=runner\n")
    config._merge_dotenv(envfile)
    assert os.environ["EMPORIA_GAMES_BY"] == "", "the runner's explicit empty value is not refilled from the .env"


def test_the_setting_is_a_persons_and_reaches_the_mcp_server():
    from emporia import agent_env
    assert "EMPORIA_GAMES_BY" in agent_env.known()
    import importlib.util
    spec = importlib.util.spec_from_file_location("kg_t", Path(HC.__file__).resolve().parents[2] / "hermes_plugin"
                                                  / "emporia" / "key_guard.py")
    kg = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(kg)
    assert "EMPORIA_GAMES_BY" in kg._PERSON_SETTINGS


def test_the_template_exists_and_carries_the_conduct_rules():
    text = HC.template_path().read_text()
    for needle in ("{{NAME}}", "counts toward no one's standing", "never by you", "Never push", "carry none"):
        assert needle.lower() in text.lower() or needle in text, needle
