"""Which relay setup joins: there is no default relay and Emporia's public relay is not on the list. A relay on this
machine or another one (its URL typed) are the choices; `--relay` skips the question; without a terminal and without
`--relay` setup stops with a one-line message; `emporia relay seed` makes #relays."""
import argparse
import builtins
import os
import re
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from emporia import agent_init, cli, rounds, setup_choices as sc



# -- the question itself ------------------------------------------------------------

def test_the_question_offers_this_machine_and_another_relay_by_url_and_nothing_of_ours():
    c = sc.relay_choice()
    assert [o.value for o in c.options] == ["local", "remote"]
    labels = dict(c.labels())
    assert labels["local"].startswith("A relay on this machine") and "emporia relay host" in labels["local"]
    assert labels["remote"].startswith("Another relay — type its URL\n")
    text = "\n".join(label for _, label in c.labels()).lower()
    assert "public" not in text and "wujitsana" not in text and "project" not in text


def test_no_code_names_a_relay_of_ours_as_a_default():
    """The review: a catalog plugin sends nobody's traffic to its author's server by default. The address
    may be in the docs as information, never in code."""
    root = Path(__file__).resolve().parents[1]
    assert not hasattr(sc, "PUBLIC_RELAY_URL") and not hasattr(sc, "public_relay")
    offenders = [str(f.relative_to(root)) for base in ("src/emporia", "hermes_plugin", "installer", "scripts")
                 for f in (root / base).rglob("*.py") if "wujitsana.net" in f.read_text()]
    assert not offenders, offenders


# -- `emporia setup agent`'s relay menu ----------------------------------------------

def _typing(monkeypatch, *answers):
    it = iter(answers)
    monkeypatch.setattr(cli, "_TTY", True)
    monkeypatch.setattr(cli, "_menus", lambda: None)
    monkeypatch.setattr(builtins, "input", lambda *_: next(it))


@pytest.fixture
def quiet(monkeypatch):
    """No relay answers, no agents on this machine."""
    monkeypatch.setattr(cli, "_relays_in_use", lambda: {})
    monkeypatch.setattr(cli, "health", lambda timeout=3.0, url="": None)


def test_the_menu_lists_this_machine_and_another_relay_and_recommends_this_machine(quiet, monkeypatch, capsys):
    monkeypatch.setattr(cli, "_start_local_relay", lambda current: cli.LOCAL_RELAY)
    _typing(monkeypatch, "")
    assert cli._choose_relay() == cli.LOCAL_RELAY, "a relay on this machine stays the default"
    out = capsys.readouterr().out
    assert "Emporia's public relay" not in out and "A relay on this machine" in out
    assert "Another relay — type its URL" in out


def test_another_relay_is_typed_in(quiet, monkeypatch, capsys):
    monkeypatch.setattr(cli, "health", lambda timeout=3.0, url="": {"version": "1"} if url == "https://friend.example" else None)
    _typing(monkeypatch, "2", "https://friend.example/")           # local · remote · host
    assert cli._choose_relay() == "https://friend.example"
    assert "Emporia's public relay" not in capsys.readouterr().out


def test_an_agent_already_on_a_relay_keeps_it_by_default(quiet, monkeypatch):
    _typing(monkeypatch, "")
    assert cli._choose_relay("https://old.example") == "https://old.example"


def test_deciding_later_keeps_the_current_relay_and_a_new_agent_has_nothing_to_keep(quiet, monkeypatch, capsys):
    _typing(monkeypatch, "5")                            # one on https://old.example: it · local · remote · host · later
    assert cli._choose_relay("https://old.example") == "https://old.example"
    capsys.readouterr()
    monkeypatch.setattr(cli, "cmd_relay_host", lambda args: 1)
    _typing(monkeypatch, "3")                            # a new agent: local · remote · host (no "later")
    assert cli._choose_relay() == ""
    assert "Decide later" not in capsys.readouterr().out


# -- the whole setup ----------------------------------------------------------------

@pytest.fixture
def profiles(tmp_path, monkeypatch):
    root = tmp_path / "profiles"
    op = root / "op"
    op.mkdir(parents=True)
    (op / "config.yaml").write_text(yaml.dump({"model": {"provider": "openrouter", "default": "x/y"}}))
    (op / ".env").write_text("OPENROUTER_API_KEY=sk-or-x\n")
    monkeypatch.setattr(cli, "this_profile", lambda: op)
    monkeypatch.setattr(cli, "health", lambda timeout=3.0, url="": None)
    monkeypatch.setattr(cli, "_relays_in_use", lambda: {})
    monkeypatch.setattr(cli, "_name_on_relay", lambda relay, agent_id: (True, ""))
    inst = cli.installer()
    monkeypatch.setattr(inst, "_hermes_profile_create", lambda name: (root / name).mkdir())
    monkeypatch.setattr(inst, "install_platform_plugin", lambda *a, **k: None)
    monkeypatch.setattr(inst, "_install_skill_link", lambda *a, **k: None)
    monkeypatch.setattr(inst, "_TOUCHED_PROFILES", [])
    monkeypatch.setattr(rounds, "turn_on", lambda p, every, plugin_dir=None: f"rounds on: {every}")
    monkeypatch.setattr(rounds, "status_line", lambda p: "off")
    return root


def _relay_of(profile: Path) -> str:
    env = dict(line.split("=", 1) for line in (profile / ".env").read_text().splitlines() if "=" in line)
    return env.get("EMPORIA_RELAY_URL", "")


def _setup(argv, capsys):
    with patch.dict(os.environ):
        rc = cli.main(argv)
    return rc, capsys.readouterr().out


def test_relay_flag_skips_the_question(profiles, monkeypatch, capsys):
    def never(current=""):
        raise AssertionError("asked which relay although --relay-url was given")
    monkeypatch.setattr(cli, "_choose_relay", never)
    _typing(monkeypatch, *[""] * 40)                     # the fast path: Enter takes every recommendation
    rc, out = _setup(["setup", "agent", "fay", "--relay-url", "http://relay.test", "--agent-id", "fay"], capsys)
    assert rc == 0, out
    assert _relay_of(profiles / "fay") == "http://relay.test"
    assert "Which relay should it join?" not in out


def test_a_person_pressing_enter_gets_a_relay_on_this_machine(profiles, monkeypatch, capsys):
    monkeypatch.delenv("EMPORIA_RELAY_URL", raising=False)
    monkeypatch.setattr(cli, "_start_local_relay", lambda current: cli.LOCAL_RELAY)
    _typing(monkeypatch, *[""] * 40)
    rc, out = _setup(["setup", "agent", "gus", "--agent-id", "gus"], capsys)
    assert rc == 0, out
    assert "Which relay should it join?" in out and "public relay" not in out
    assert _relay_of(profiles / "gus") == cli.LOCAL_RELAY


def test_there_is_no_default_relay_without_a_terminal(profiles, monkeypatch, capsys):
    """Review: without --relay and without a terminal setup fell back to the author's server. It stops, writing
    nothing, and says to pass --relay-url."""
    monkeypatch.delenv("EMPORIA_RELAY_URL", raising=False)
    monkeypatch.setattr(cli, "_TTY", False)
    assert cli.default_relay() == "" and cli.default_relay("https://current.example/") == "https://current.example"
    rc, out = _setup(["setup", "agent", "nia", "--agent-id", "nia", "--keys", "files"], capsys)
    assert rc == 1 and "--relay-url URL" in out and "no default relay" in out
    assert not (profiles / "nia" / ".env").exists() or _relay_of(profiles / "nia") == ""


def test_without_a_terminal_a_relay_named_in_the_callers_env_is_not_inherited_by_another_profile(profiles, monkeypatch,
                                                                                              capsys):
    """S8: `hermes emporia setup` from profile A hands the runtime A's EMPORIA_RELAY_URL; a new agent in profile
    B is not on A's relay unless told. Only the calling profile's own relay is a fallback."""
    monkeypatch.setenv("EMPORIA_RELAY_URL", "http://relay.test")
    monkeypatch.setattr(cli, "_TTY", False)
    rc, out = _setup(["setup", "agent", "ned", "--agent-id", "ned", "--keys", "files"], capsys)
    assert rc == 0, "a person's own export in a shell is still a relay they named"
    monkeypatch.setenv("EMPORIA_HANDED_NAMES", "EMPORIA_RELAY_URL")      # what the plugin hands over: the caller's
    rc, out = _setup(["setup", "agent", "ned2", "--agent-id", "ned2", "--keys", "files"], capsys)
    assert rc == 1 and "--relay-url URL" in out
    assert cli.default_relay("", use_env=False) == "" and cli.default_relay("") == "http://relay.test"
    assert cli.default_relay("https://current.example", use_env=False) == "https://current.example"
    rc, out = _setup(["setup", "agent", "ned", "--agent-id", "ned", "--keys", "files",
                      "--relay-url", "http://relay.test"], capsys)
    assert rc == 0, out
    assert _relay_of(profiles / "ned") == "http://relay.test"


# -- the closing block ----------------------------------------------------------------

def test_the_closing_block_names_the_relay_without_judging_it():
    text = "\n".join(cli.welcome_lines("shop", "shop", "https://friend.example"))
    assert "Relay  : https://friend.example\n" in text + "\n" and "Emporia project" not in text


# -- `emporia agent init` ---------------------------------------------------------------

def _init_args(**kw):
    base = dict(path="", name="scout", relay="", client="custom", client_version="", model="", no_declare_model=False,
                operator_label="", same_as="", display_name="", no_register=False, recommended=True, json=True,
                features="")
    return argparse.Namespace(**{**base, **kw})


@pytest.mark.parametrize("answers, relay", [
    (("",), cli.LOCAL_RELAY),                                      # Enter: a relay on this machine
    (("2", "https://friend.example"), "https://friend.example"),   # another one, its URL typed
])
def test_agent_init_offers_the_same_choices(monkeypatch, answers, relay):
    monkeypatch.delenv("EMPORIA_RELAY_URL", raising=False)
    _typing(monkeypatch, *answers)
    assert agent_init.ask_missing(_init_args()).relay == relay


def test_agent_init_relay_flag_skips_the_question(monkeypatch):
    _typing(monkeypatch)                                           # any question would run out of answers
    assert agent_init.ask_missing(_init_args(relay="https://mine.example")).relay == "https://mine.example"


def test_agent_init_without_a_terminal_requires_a_relay(monkeypatch, capsys):
    monkeypatch.setattr(agent_init, "_tty", lambda: False)
    seen = []
    monkeypatch.setattr(agent_init, "init_agent", lambda **kw: seen.append(kw["relay"]) or {"registered": True})
    assert agent_init.run(_init_args()) != 0 and seen == []
    assert "--relay URL is needed" in capsys.readouterr().err
    assert agent_init.run(_init_args(relay="https://mine.example")) == 0 and seen == ["https://mine.example"]


# -- `emporia relay seed` makes #relays ---------------------------------------------------

class FakeRelay:
    """The operator's calls and the operator agent's tools, against topics and posts held here."""

    def __init__(self, topics=()):
        self.topics = list(topics)
        self.posts: dict[str, list[str]] = {t: [cli.seed_welcome(t)] for t in topics}
        self.made: list[str] = []

    def call(self, profile, method, path, body=None, params=None):
        return {"/operator/whoami": (200, {"is_operator": True}),
                "/health": (200, {"season": {"season": "season-0", "started_at": "a", "ends_at": "b"}}),
                "/agoras/topics": (200, {"topics": [{"slug": t} for t in self.topics]}),
                "/labs": (404, None),
                "/operator/status": (200, {"house": {"agents": []}})}[path]

    def run(self, profile, code):
        topic = re.search(r"(?:slug|topic)='([a-z]+)'", code).group(1)
        if "create_agora_topic" in code:
            self.topics.append(topic)
            self.made.append(topic)
            return {"success": True}
        if "read_feed" in code:
            return {"posts": [{"text": t} for t in self.posts.get(topic, [])]}
        self.posts.setdefault(topic, []).append(cli.seed_welcome(topic))
        return {"success": True}


@pytest.fixture
def seeding(monkeypatch, tmp_path):
    def go(relay):
        monkeypatch.setattr(cli, "_persons_terminal", lambda: True)
        monkeypatch.setattr(cli, "_agent_profiles", lambda root: [(tmp_path / "ops", {})])
        monkeypatch.setattr(cli, "_agent", lambda name: (tmp_path / "ops", "ops", {}))
        monkeypatch.setattr(cli, "_operator_call", relay.call)
        monkeypatch.setattr(cli, "_seed_run", relay.run)
        return cli.cmd_relay_seed(argparse.Namespace(url="ops", lab_repo=""))
    return go


def test_the_seed_adds_relays_once(seeding, capsys):
    relay = FakeRelay()
    assert seeding(relay) == 0
    out = capsys.readouterr().out
    every = ["ideas", "games", "debriefs", "labs", "help", "relays"]
    assert relay.made == every, "#debriefs is seeded: the debrief quests post there and a newcomer cannot make it"
    assert "✓ #relays: made, welcome post written" in out
    assert relay.posts["relays"] == [cli.seed_welcome("relays")]
    assert seeding(relay) == 0, "run again"
    again = capsys.readouterr().out
    assert relay.made == every, "nothing made twice"
    assert again.count("welcome post already there") == 6 and relay.posts["relays"] == [cli.seed_welcome("relays")]


def test_a_relay_seeded_before_gets_only_the_new_topics(seeding, capsys):
    relay = FakeRelay(["ideas", "games", "labs", "help"])
    assert seeding(relay) == 0
    out = capsys.readouterr().out
    assert relay.made == ["debriefs", "relays"]
    assert out.count("welcome post already there") == 4 and "✓ #relays: made, welcome post written" in out


def test_no_shipped_text_makes_a_relay_of_ours_the_default():
    """The bundled console (docs embedded in its JavaScript) and the docs say it only as information."""
    root = Path(__file__).resolve().parents[1]
    bundle = "".join(f.read_text() for f in (root / "src/emporia/console/ui/assets").glob("*.js"))
    for text in (bundle, (root / "README.md").read_text(), (root / "docs/create-an-agent.md").read_text(),
                 (root / "hermes_plugin/emporia/README.md").read_text()):
        assert "Emporia's public relay — https" not in text
        assert not re.search(r"(joins?|offers?) the public relay by default", text)
    assert "no default relay" in bundle


# -- the relay as typed: made whole, then looked at -----------------------------------

@pytest.mark.parametrize("typed, want", [
    ("emporia.example.net", "https://emporia.example.net"),
    ("emporia.example.net/", "https://emporia.example.net"),
    ("emporia.example.net:8443/prefix/", "https://emporia.example.net:8443/prefix"),
    ("https://emporia.example.net/", "https://emporia.example.net"),
    ("http://emporia.example.net", "http://emporia.example.net"),          # an explicit scheme is kept
    ("localhost:8088", "http://localhost:8088"), ("127.0.0.1:9", "http://127.0.0.1:9"), ("[::1]:8088", "http://[::1]:8088"),
    ("HTTPS://Emporia.Example.NET/Prefix", "https://emporia.example.net/Prefix"), ("Emporia.Example.NET", "https://emporia.example.net"),
    ("emporia.example.net/x?y=1", ""), ("emporia.example.net/#frag", ""), ("0.0.0.0:8088", ""), ("http://0.0.0.0", ""),
    ("user:pw@emporia.example.net", ""), ("https://user@emporia.example.net", ""), ("ftp://emporia.example.net", ""),
    ("", ""), ("not a url", ""), ("https://", ""), ("https://h:notaport", ""),
])
def test_a_relay_is_made_whole(typed, want):
    assert cli.normalize_relay_url(typed) == want
    assert cli.normalize_relay_url("http://remote.example", plain_remote=False) == ""


def test_the_plugin_normalises_the_same_way():
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "sc_norm", Path(__file__).resolve().parents[1] / "hermes_plugin" / "emporia" / "setup_cli.py")
    sc_mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(sc_mod)
    for typed in ("emporia.example.net/", "HTTPS://Emporia.Example.NET/p", "h.example/x?y", "0.0.0.0", "localhost:8088", "https://h.example/", "u:p@h.example", "ftp://h", "a b", "http://h.example"):
        assert sc_mod.normalize_relay(typed) == cli.normalize_relay_url(typed), typed


def _health(monkeypatch, answers):
    monkeypatch.setattr(cli, "relay_probe", cli._relay_probe)
    monkeypatch.setattr(cli, "health", lambda timeout=3.0, url="": answers.get(url.rstrip("/")))
    monkeypatch.setattr(cli, "_TTY", False)


def test_a_relay_flag_is_looked_at_and_only_an_emporia_relay_is_taken(monkeypatch, capsys):
    ok = {"service": "Emporia", "relay_id": "rel_x", "version": "1"}
    _health(monkeypatch, {"https://good.example.net": ok, "https://closed.example.net": {
               "status": "ok", "service": "Emporia", "access": "closed", "relay_url": "https://closed.example.net"},
           "https://web.example.net": {"service": "nginx"}, "https://bare.example.net": {"status": "ok"}})
    assert cli.check_relay_flag("good.example.net") == "https://good.example.net"
    assert "Using https://good.example.net" in capsys.readouterr().out
    assert cli.check_relay_flag("https://closed.example.net") == "https://closed.example.net"
    for bad in ("web.example.net", "bare.example.net", "down.example.net"):
        assert cli.check_relay_flag(bad, "--relay") == ""
        out = capsys.readouterr().out
        assert "is not answering as an Emporia relay; check the address and pass --relay again" in out
        assert "{" not in out, "no response body"
    assert cli.check_relay_flag("user:pw@good.example.net") == ""
    assert "relay URL is its https address" in capsys.readouterr().out


def test_a_local_relay_that_is_not_up_is_accepted_with_a_note_but_not_one_that_is_something_else(monkeypatch, capsys):
    _health(monkeypatch, {"http://127.0.0.1:8099": {"service": "nginx"}})
    assert cli.check_relay_flag("localhost:8088") == "http://localhost:8088"
    assert "emporia relay start" in capsys.readouterr().out
    assert cli.check_relay_flag("127.0.0.1:8099") == ""


def test_with_a_terminal_a_relay_that_is_not_an_emporia_relay_is_asked_about(monkeypatch, capsys):
    _health(monkeypatch, {"https://web.example.net": {"service": "nginx"}})
    monkeypatch.setattr(cli, "_TTY", True)
    asked = []
    monkeypatch.setattr(cli, "ask", lambda q, default=True: asked.append(q) or False)
    assert cli.check_relay_flag("web.example.net") == "" and "not as an Emporia relay" in asked[0]
    monkeypatch.setattr(cli, "ask", lambda q, default=True: asked.append(q) or True)
    assert cli.check_relay_flag("down.example.net") == "https://down.example.net" and "not answering now" in asked[-1]
    assert cli._check_relay_url("web.example.net") == "https://web.example.net"       # the menu's own check asks the same


def test_a_malformed_or_unanswering_flag_stores_nothing(profiles, monkeypatch, capsys):
    monkeypatch.delenv("EMPORIA_RELAY_URL", raising=False)
    monkeypatch.setattr(cli, "_TTY", False)
    monkeypatch.setattr(cli, "relay_probe", cli._relay_probe)
    monkeypatch.setattr(cli, "health", lambda timeout=3.0, url="": None)
    for typed in ("user:pw@relay.example.net", "relay.example.net"):
        rc, out = _setup(["setup", "agent", "ova", "--agent-id", "ova", "--keys", "files", "--relay-url", typed], capsys)
        assert rc == 1 and "nothing was written" in out
        assert not (profiles / "ova").exists() or _relay_of(profiles / "ova") == ""
    monkeypatch.setattr(cli, "health", lambda timeout=3.0, url="": {"service": "Emporia", "relay_id": "r"})
    rc, out = _setup(["setup", "agent", "ova", "--agent-id", "ova", "--keys", "files", "--relay-url", "relay.example.net/"], capsys)
    assert rc == 0, out
    assert _relay_of(profiles / "ova") == "https://relay.example.net" and "Using https://relay.example.net" in out


def test_relay_use_and_agent_init_take_a_bare_host(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(cli, "_TTY", False)
    monkeypatch.setattr(cli, "_agent_profiles", lambda root: [])
    rc = cli.main(["relay", "use", "user:pw@relay.example.net", "x"])
    assert rc == 1 and "relay URL is its https address" in capsys.readouterr().out
    monkeypatch.setattr(agent_init, "_tty", lambda: False)
    seen = []
    monkeypatch.setattr(agent_init, "init_agent", lambda **kw: seen.append(kw["relay"]) or {"registered": True})
    assert agent_init.run(_init_args(relay="relay.example.net/")) == 0 and seen == ["https://relay.example.net"]
    assert agent_init.run(_init_args(relay="user:pw@relay.example.net")) == 2 and seen == ["https://relay.example.net"]
    assert "not accepted" in capsys.readouterr().err
    monkeypatch.setattr(cli, "relay_probe", lambda url: ("down", None))
    monkeypatch.setattr(cli, "health", lambda timeout=3.0, url="": None)
    assert agent_init.run(_init_args(relay="relay.example.net")) == 2
    assert agent_init.run(_init_args(relay="relay.example.net", no_register=True)) == 0


def test_a_relay_the_agent_is_already_on_is_kept_when_it_is_briefly_down(monkeypatch, capsys):
    """N10: without a terminal, a remote relay that is not answering is refused unless the agent already is on it."""
    _health(monkeypatch, {})
    assert cli.check_relay_flag("relay.example.net", "--relay") == ""
    capsys.readouterr()
    assert cli.check_relay_flag("relay.example.net", "--relay", current="https://relay.example.net/") == "https://relay.example.net"
    assert "already on it" in capsys.readouterr().out
    assert cli.check_relay_flag("other.example.net", "--relay", current="https://relay.example.net") == ""


def test_agent_init_says_which_it_is_malformed_or_not_answering(monkeypatch, capsys):
    monkeypatch.setattr(agent_init, "_tty", lambda: False)
    monkeypatch.setattr(cli, "relay_probe", lambda url: ("down", None))
    assert agent_init.run(_init_args(relay="relay.example.net")) == 2
    err = capsys.readouterr().err
    assert "not answering as an Emporia relay" in err and "malformed" not in err and "not accepted" not in err
    assert agent_init.run(_init_args(relay="a.example/x?y")) == 2
    assert "not accepted" in capsys.readouterr().err
