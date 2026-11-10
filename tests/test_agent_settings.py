"""`emporia agent settings NAME`: an agent's own limits and behaviour on one screen (a person's)."""
import argparse
import importlib.util
import json
from pathlib import Path

import pytest

from emporia import agent_settings as S
from emporia import cli, rounds

REPO = Path(__file__).resolve().parent.parent


@pytest.fixture
def shop(tmp_path, monkeypatch):
    """A fake profile whose rounds job list is empty (no Hermes is run) and that is no terminal."""
    profile = tmp_path / "shop"
    profile.mkdir()
    (profile / ".env").write_text("EMPORIA_AGENT_ID=shop\n")
    monkeypatch.setattr(cli, "_agent", lambda name: (profile, "shop", {"EMPORIA_AGENT_ID": "shop"}))
    monkeypatch.setattr(cli, "_TTY", False)
    monkeypatch.setattr(rounds, "run_hermes", lambda argv: "")
    return profile


def run(*extra, name="shop"):
    args = cli.build_parser().parse_args(["agent", "settings", name, *extra])
    return cli.cmd_agent_settings(args.url_or_name, args)


def env_of(profile):
    return cli.installer().harvest_env(profile)


def test_the_table_lists_every_setting_with_its_current_value_and_default(shop, capsys):
    assert run() == 0
    out = capsys.readouterr().out
    for group in S.GROUPS:
        assert group in out
    for s in S.SETTINGS:
        assert s.label in out, s.key
    assert "Games per operator a day: 6 a day (default)" in out
    assert "Games open at once: 3 (default)" in out
    assert "New rooms or topics a day: 3 a day (read-only)" in out
    assert "Relay-started turns a day: 300 in 24 hours (default)" in out
    assert "Messages per conversation an hour: 20 an hour (default)" in out
    assert "Unattended spend a day: none (default)" in out        # no network listed: nothing is a testnet
    assert "Largest single payment: 1000 cents (default)" in out
    assert "Ask you above: never asks you (default)" in out
    assert "Real money (mainnet): locked (testnets only)" in out and "emporia wallet mainnet shop" in out
    assert "Wake mode: direct (default)" in out
    assert "Playbook preset: custom (default)" in out
    assert "emporia features on|off" in out
    assert "Read-only here" in out, "without a terminal it only shows"


def test_the_testnet_default_for_unattended_spend_is_shown_as_the_default(shop, capsys):
    cli.merge_env_file(shop / ".env", {"EMPORIA_X402_PAY_NETWORKS": "base-sepolia"})
    run()
    assert "Unattended spend a day: 25 cents a day (default)" in capsys.readouterr().out


def test_the_table_is_short_enough_for_a_chat_message(shop, capsys):
    run()
    assert len(capsys.readouterr().out) < 3000


def test_json_prints_the_table(shop, capsys):
    assert run("--json") == 0
    data = json.loads(capsys.readouterr().out)
    assert [r["key"] for r in data["settings"]] == [s.key for s in S.SETTINGS]
    games = next(r for r in data["settings"] if r["key"] == "games_per_operator")
    assert games["default"] is True and games["value"] == "6 a day"
    assert data["restart"] == "hermes -p shop gateway restart"


def test_without_a_terminal_it_changes_nothing(shop, capsys):
    before = (shop / ".env").read_text()
    assert run() == 0
    assert (shop / ".env").read_text() == before
    assert "--set KEY=VALUE" in capsys.readouterr().out


def test_set_writes_through_the_same_writer_and_names_the_restart(shop, capsys, monkeypatch):
    seen = []
    real = cli.merge_env_file
    monkeypatch.setattr(cli, "merge_env_file", lambda path, updates: seen.append((path, dict(updates))) or real(path, updates))
    assert run("--set", "games_per_operator=10", "--set", "daily_turns=0", "--set", "wake=open",
               "--set", "payment_approval_cents=200", "--set", "thread_limit=5", "--set", "open_games=2",
               "--set", "max_payment_cents=300", "--set", "unattended_spend_cents=40") == 0
    env = env_of(shop)
    assert env["EMPORIA_PLAYBOOK_GAMES_PER_OPERATOR_PER_DAY"] == "10"
    assert env["EMPORIA_DAILY_TURNS"] == "0" and env["EMPORIA_WAKE"] == "open"
    assert env["EMPORIA_PAYMENT_APPROVAL_CENTS"] == "200" and env["EMPORIA_THREAD_LIMIT"] == "5"
    assert env["EMPORIA_MAX_OPEN_GAMES"] == "2" and env["EMPORIA_X402_MAX_PAYMENT_CENTS"] == "300"
    assert env["EMPORIA_PLAYBOOK_AUTONOMOUS_SPEND_CENTS_PER_DAY"] == "40"
    assert len(seen) == 1 and seen[0][0] == shop / ".env", "one write, through merge_env_file"
    out = capsys.readouterr().out
    assert "Games per operator a day: 6 a day (default) -> 10 a day" in out
    assert "Wake mode: direct (default) -> open" in out
    assert "hermes -p shop gateway restart" in out
    # what the runtime reads is what was set
    from emporia import playbook, wake
    assert playbook.resolve(env).games_per_operator_per_day == 10 and wake.resolve(env) == "open"


def test_default_puts_a_setting_back(shop):
    run("--set", "daily_turns=50")
    assert env_of(shop)["EMPORIA_DAILY_TURNS"] == "50"
    run("--set", "daily_turns=default")
    assert env_of(shop).get("EMPORIA_DAILY_TURNS", "") == ""


@pytest.mark.parametrize("item, allowed", [
    ("games_per_operator=lots", "a whole number, 0 or more"),
    ("daily_turns=-5", "a whole number, 0 or more"),
    ("max_payment_cents=2.5", "whole cents"),
    ("wake=sideways", "closed, direct or open"),
    ("playbook=wizard", "custom, player, merchant"),
    ("rounds=3h", "off, 2h, 4h"),
    ("networks=moonbase", "base-sepolia"),
    ("networks=base", "emporia wallet mainnet shop"),
    ("nonsense=1", "Keys:"),
    ("new_rooms_per_day=9", "read-only"),
    ("mainnet=on", "emporia wallet mainnet shop"),
    ("features=all", "emporia features on|off FEATURE"),
    ("wake", "KEY=VALUE"),
])
def test_a_bad_value_is_refused_with_the_allowed_range_and_nothing_is_written(shop, capsys, item, allowed):
    before = (shop / ".env").read_text()
    assert run("--set", "daily_turns=7", "--set", item) == 1
    assert allowed in capsys.readouterr().out
    assert (shop / ".env").read_text() == before, "a refused call writes nothing, not even its good part"


def test_a_testnet_network_list_is_accepted_and_none_clears_it(shop):
    assert run("--set", "networks=base-sepolia,solana-devnet") == 0
    assert env_of(shop)["EMPORIA_X402_PAY_NETWORKS"] == "eip155:84532,solana:EtWTRABZaYq6iMfeYKouRu166VU2xqa1"
    assert run("--set", "networks=none") == 0
    assert env_of(shop).get("EMPORIA_X402_PAY_NETWORKS", "") == ""


def test_a_preset_starts_the_playbook_clean(shop):
    run("--set", "games_per_operator=2")
    assert run("--set", "playbook=merchant") == 0
    env = env_of(shop)
    assert env["EMPORIA_PLAYBOOK"] == "merchant" and env["EMPORIA_PLAYBOOK_GAMES_PER_OPERATOR_PER_DAY"] == ""


def test_rounds_go_through_the_rounds_code(shop, monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(rounds, "turn_on", lambda name, every: calls.append(("on", name, every)) or "rounds on")
    monkeypatch.setattr(rounds, "turn_off", lambda name: calls.append(("off", name)) or "rounds off")
    assert run("--set", "rounds=8h") == 0
    assert run("--set", "rounds=off") == 0
    assert calls == [("on", "shop", "8h"), ("off", "shop")]
    assert "hermes -p shop gateway restart" not in capsys.readouterr().out, "rounds are a cron job, not the gateway's"


def test_an_unknown_agent_is_said(monkeypatch, capsys):
    monkeypatch.setattr(cli, "_agent", lambda name: (Path("/nowhere"), name, {}))
    assert run(name="ghost") == 1
    assert "not an Emporia agent profile" in capsys.readouterr().out


def test_the_menu_picks_a_number_shows_the_options_and_keeps_on_enter(shop, monkeypatch, capsys):
    monkeypatch.setattr(cli, "_TTY", True)
    monkeypatch.setattr(cli, "_persons_terminal", lambda: True)
    keys = [s.key for s in S.SETTINGS]
    number = lambda key: str(keys.index(key) + 1)        # noqa: E731
    typed = iter([number("daily_turns"), "120",                  # a number, then its value
                  number("games_per_operator"), "",              # Enter keeps
                  number("wake"),                                # a choice
                  number("new_rooms_per_day"),                   # read-only: says how
                  number("daily_turns"), "-4",                   # refused, asked about again by the menu
                  "99",                                          # not a row
                  "list",                                        # the table again
                  ""])                                         # done
    monkeypatch.setattr(cli, "prompt", lambda question, default="": next(typed))
    asked = {}

    def choose(question, options, default=0):
        asked["wake"] = (question, [k for k, _ in options], options[default][0], "\n".join(t for _, t in options))
        return "closed"
    monkeypatch.setattr(cli, "choose", choose)
    assert run() == 0
    out = capsys.readouterr().out
    assert env_of(shop)["EMPORIA_DAILY_TURNS"] == "120" and env_of(shop)["EMPORIA_WAKE"] == "closed"
    assert "EMPORIA_PLAYBOOK_GAMES_PER_OPERATOR_PER_DAY" not in env_of(shop)
    assert "Allowed: a whole number, 0 or more (0 = no cap)" in out and "Recommended: 300 (the default)" in out
    assert asked["wake"][1] == ["direct", "open", "closed"] and asked["wake"][2] == "direct"
    assert "(recommended)" in asked["wake"][3]
    assert "Kept." in out and "Allowed: a whole number" in out
    assert "Type a number from the list" in out and "Change it:" not in out
    assert out.count("Settings for shop") == 2, "shown once, and again on `list`"
    assert "not allowed. Allowed: a whole number" in out, "a refused value is said, and the menu goes on"
    assert "Changed:" in out and "Relay-started turns a day: 300 in 24 hours (default) -> 120 in 24 hours" in out
    assert "hermes -p shop gateway restart" in out and "hermes gateway restart" in out


def test_in_an_agents_own_terminal_even_a_tty_only_shows(shop, monkeypatch, capsys):
    monkeypatch.setattr(cli, "_TTY", True)
    monkeypatch.setenv("HERMES_SESSION_ID", "x")
    monkeypatch.setattr(cli, "prompt", lambda *a, **k: pytest.fail("an agent's terminal is never asked"))
    assert run() == 0
    assert "Read-only here" in capsys.readouterr().out


# -- the key guard holds it for the person --------------------------------------------------

def _guard():
    spec = importlib.util.spec_from_file_location("emporia_key_guard_s", REPO / "hermes_plugin" / "emporia" / "key_guard.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.parametrize("command", [
    "emporia agent settings shop --set daily_turns=0",
    "emporia agent settings shop --set wake=open --set open_games=0",
    "hermes -p shop emporia agent settings",
    "hermes emporia agent settings shop --set payment_approval_cents=100000",
    ".venv/bin/python -m emporia.cli agent settings shop --set max_payment_cents=999999",
])
def test_an_agent_cannot_run_agent_settings(command):
    out = _guard().pre_tool_call(tool_name="terminal", args={"command": command})
    assert out and out["action"] == "block", command


@pytest.mark.parametrize("args", [
    {"command": "echo EMPORIA_DAILY_TURNS=0 >> /opt/profiles/shop/.env"},
    {"command": "hermes config set EMPORIA_THREAD_LIMIT 0"},
])
def test_an_agent_cannot_write_the_cost_ceilings_either(args):
    out = _guard().pre_tool_call(tool_name="terminal", args=args)
    assert out and out["action"] == "block", args


def test_set_is_refused_while_an_unattended_turn_is_live(shop, capsys):
    import time
    folder = shop / "plugin-data" / "emporia"
    folder.mkdir(parents=True)
    (folder / "autonomy.json").write_text(json.dumps({"turns": {"k": {"origin": "relay", "at": time.time()}}}))
    assert run("--set", "daily_turns=7") == 1
    assert "relay turn is running" in capsys.readouterr().out
    assert "EMPORIA_DAILY_TURNS" not in (shop / ".env").read_text()
    (folder / "autonomy.json").write_text(json.dumps({"turns": {}}))
    assert run("--set", "daily_turns=7") == 0, "no turn live: a script may set"


def test_every_setting_the_screen_writes_is_held_by_the_key_guard():
    g = _guard()
    for s in S.SETTINGS:
        if not s.env:
            continue
        out = g.pre_tool_call(tool_name="terminal", args={"command": f"echo {s.env}=1 >> /x/shop/.env"})
        assert out and out["action"] in ("block", "approve"), s.env
        out = g.pre_tool_call(tool_name="write_file", args={"path": "/h/shop/.env", "content": f"{s.env}=1\n"})
        assert out and out["action"] in ("block", "approve"), f"{s.env} in the profile .env"


def test_a_file_written_by_a_tool_cannot_carry_person_only_commands_or_settings():
    g = _guard()
    for tool, args in (
            ("write_file", {"path": "go.sh", "content": "emporia agent settings shop --set daily_turns=0\n"}),
            ("patch", {"path": "go.sh", "old_string": "x", "new_string": "emporia wake open"}),
            ("write_file", {"path": "go.py", "content": "env = {'EMPORIA_WAKE': 'open'}"}),
            ("patch", {"path": "a", "patch": "*** Update File: a\n+EMPORIA_MAX_OPEN_GAMES=0"})):
        out = g.pre_tool_call(tool_name=tool, args=args)
        assert out and out["action"] == "block", args
    assert g.pre_tool_call(tool_name="write_file", args={"path": "notes.md", "content": "hello, a plain note"}) is None


def test_other_agent_commands_are_unchanged():
    assert _guard().pre_tool_call(tool_name="terminal", args={"command": "emporia agent playbook shop --preset player"})[
        "action"] == "approve"
    assert _guard().pre_tool_call(tool_name="terminal", args={"command": "git status"}) is None
