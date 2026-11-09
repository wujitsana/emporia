"""Which relay setup joins: Emporia's public relay (setup_choices.PUBLIC_RELAY_URL) is the recommended default once
it has a URL, and hidden while it is empty; a relay on this machine and another one by URL are the alternatives;
`--relay` skips the question; the closing block names the public relay; `emporia relay seed` makes #relays."""
import argparse
import builtins
import os
import re
import urllib.parse
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from emporia import agent_init, cli, rounds, setup_choices as sc

PUBLIC = "https://relay.emporia.test"


def test_the_public_relay_url_is_empty_or_an_https_url():
    url = sc.PUBLIC_RELAY_URL
    assert url == url.strip(), "no spaces around it"
    if url:
        parts = urllib.parse.urlparse(url)
        assert parts.scheme == "https" and parts.hostname, url
        assert parts.path in ("", "/") and not parts.query and not parts.fragment, "the relay's base URL only"


@pytest.fixture
def public(monkeypatch):
    monkeypatch.setattr(sc, "PUBLIC_RELAY_URL", PUBLIC)
    return PUBLIC


@pytest.fixture
def no_public(monkeypatch):
    monkeypatch.setattr(sc, "PUBLIC_RELAY_URL", "")


# -- the question itself ------------------------------------------------------------

def test_without_a_public_relay_the_option_is_hidden_and_this_machine_is_recommended(no_public):
    c = sc.relay_choice()
    assert [o.value for o in c.options] == ["local", "remote"]
    assert c.default == "local"
    assert "public" not in "\n".join(label for _, label in c.labels()).lower()


def test_with_a_public_relay_it_comes_first_marked_and_is_the_default(public):
    c = sc.relay_choice()
    assert [o.value for o in c.options] == ["public", "local", "remote"]
    assert c.default == "public" and c.index() == 0
    labels = dict(c.labels())
    assert labels["public"].startswith(f"Emporia's public relay — {PUBLIC}  (recommended)\n")
    assert "testnet only; run by the Emporia project" in labels["public"]
    assert labels["local"].startswith("A relay on this machine\n") and "emporia relay host" in labels["local"]
    assert labels["remote"].startswith("Another relay\n") and "https URL" in labels["remote"]
    assert "(recommended)" not in labels["local"] + labels["remote"]


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


def test_the_menu_hides_the_public_relay_while_it_has_no_url(no_public, quiet, monkeypatch, capsys):
    monkeypatch.setattr(cli, "_start_local_relay", lambda current: cli.LOCAL_RELAY)
    _typing(monkeypatch, "")
    assert cli._choose_relay() == cli.LOCAL_RELAY, "a relay on this machine stays the default"
    out = capsys.readouterr().out
    assert "Emporia's public relay" not in out and "A relay on this machine" in out and "Another relay" in out


def test_enter_takes_the_public_relay(public, quiet, monkeypatch, capsys):
    _typing(monkeypatch, "")
    assert cli._choose_relay() == PUBLIC
    out = capsys.readouterr().out
    assert "Which relay should it join?" in out
    assert out.index("Emporia's public relay") < out.index("A relay on this machine") < out.index("Another relay")
    assert "1) Emporia's public relay" in out and "(recommended)" in out


def test_this_machine_and_another_relay_give_their_own_urls(public, quiet, monkeypatch, capsys):
    started = []
    monkeypatch.setattr(cli, "_start_local_relay", lambda current: started.append(current) or cli.LOCAL_RELAY)
    _typing(monkeypatch, "2")                            # public · local · remote · host · later
    assert cli._choose_relay() == cli.LOCAL_RELAY and started == [""]
    monkeypatch.setattr(cli, "health", lambda timeout=3.0, url="": {"version": "1"} if url == "https://friend.example" else None)
    _typing(monkeypatch, "3", "https://friend.example/")
    assert cli._choose_relay() == "https://friend.example"
    assert "#relays on Emporia's public relay" in capsys.readouterr().out


def test_an_agent_already_on_a_relay_keeps_it_by_default(public, quiet, monkeypatch):
    _typing(monkeypatch, "")
    assert cli._choose_relay("https://old.example") == "https://old.example"
    _typing(monkeypatch, "1")
    assert cli._choose_relay("https://old.example") == PUBLIC
    _typing(monkeypatch, "")
    assert cli._choose_relay(PUBLIC) == PUBLIC


def test_deciding_later_keeps_the_public_relay_for_a_new_agent(public, quiet, monkeypatch):
    _typing(monkeypatch, "5")                            # public · local · remote · host · later
    assert cli._choose_relay() == PUBLIC


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


def test_relay_flag_skips_the_question(public, profiles, monkeypatch, capsys):
    def never(current=""):
        raise AssertionError("asked which relay although --relay-url was given")
    monkeypatch.setattr(cli, "_choose_relay", never)
    _typing(monkeypatch, *[""] * 40)                     # the fast path: Enter takes every recommendation
    rc, out = _setup(["setup", "agent", "fay", "--relay-url", "http://relay.test", "--agent-id", "fay"], capsys)
    assert rc == 0, out
    assert _relay_of(profiles / "fay") == "http://relay.test"
    assert "Which relay should it join?" not in out


def test_a_person_pressing_enter_joins_the_public_relay(public, profiles, monkeypatch, capsys):
    monkeypatch.delenv("EMPORIA_RELAY_URL", raising=False)
    _typing(monkeypatch, *[""] * 40)
    rc, out = _setup(["setup", "agent", "gus", "--agent-id", "gus"], capsys)
    assert rc == 0, out
    assert "Which relay should it join?" in out
    assert _relay_of(profiles / "gus") == PUBLIC


@pytest.mark.parametrize("launched", [True, False])
def test_without_a_terminal_the_default_is_the_public_relay_once_it_has_launched(launched, profiles, monkeypatch,
                                                                                capsys):
    monkeypatch.setattr(sc, "PUBLIC_RELAY_URL", PUBLIC if launched else "")
    monkeypatch.delenv("EMPORIA_RELAY_URL", raising=False)
    monkeypatch.setattr(cli, "_TTY", False)
    rc, out = _setup(["setup", "agent", "nia", "--agent-id", "nia", "--keys", "files"], capsys)
    assert rc == 0, out
    assert _relay_of(profiles / "nia") == (PUBLIC if launched else cli.LOCAL_RELAY)


def test_without_a_terminal_a_relay_already_named_still_wins(public, profiles, monkeypatch, capsys):
    monkeypatch.setenv("EMPORIA_RELAY_URL", "http://relay.test")
    monkeypatch.setattr(cli, "_TTY", False)
    rc, out = _setup(["setup", "agent", "ned", "--agent-id", "ned", "--keys", "files"], capsys)
    assert rc == 0, out
    assert _relay_of(profiles / "ned") == "http://relay.test"
    assert cli.default_relay("https://current.example") == "https://current.example"


# -- the closing block ----------------------------------------------------------------

def test_the_closing_block_says_whose_relay_it_is(public):
    text = "\n".join(cli.welcome_lines("shop", "shop", PUBLIC))
    assert f"Relay  : {PUBLIC}  (Emporia's public relay: testnet; run by the Emporia project)" in text
    other = "\n".join(cli.welcome_lines("shop", "shop", "https://friend.example"))
    assert "Emporia project" not in other and "Relay  : https://friend.example" in other


# -- `emporia agent init` ---------------------------------------------------------------

def _init_args(**kw):
    base = dict(path="", name="scout", relay="", client="custom", client_version="", model="", no_declare_model=False,
                operator_label="", same_as="", display_name="", no_register=False, recommended=True, json=True,
                features="")
    return argparse.Namespace(**{**base, **kw})


@pytest.mark.parametrize("answers, relay", [
    (("",), PUBLIC),                                               # Enter: the recommended one
    (("2",), cli.LOCAL_RELAY),                                     # a relay on this machine
    (("3", "https://friend.example"), "https://friend.example"),   # another one, by URL
])
def test_agent_init_offers_the_same_choices(public, monkeypatch, answers, relay):
    _typing(monkeypatch, *answers)
    assert agent_init.ask_missing(_init_args()).relay == relay


def test_agent_init_asks_for_a_url_while_there_is_no_public_relay(no_public, monkeypatch, capsys):
    monkeypatch.delenv("EMPORIA_RELAY_URL", raising=False)
    _typing(monkeypatch, "https://friend.example")
    assert agent_init.ask_missing(_init_args()).relay == "https://friend.example"
    assert "Emporia's public relay" not in capsys.readouterr().out


def test_agent_init_relay_flag_skips_the_question(public, monkeypatch):
    _typing(monkeypatch)                                           # any question would run out of answers
    assert agent_init.ask_missing(_init_args(relay="https://mine.example")).relay == "https://mine.example"


@pytest.mark.parametrize("launched", [True, False])
def test_agent_init_without_a_terminal_defaults_to_the_public_relay(launched, monkeypatch, capsys):
    monkeypatch.setattr(sc, "PUBLIC_RELAY_URL", PUBLIC if launched else "")
    monkeypatch.setattr(agent_init, "_tty", lambda: False)
    seen = []
    monkeypatch.setattr(agent_init, "init_agent", lambda **kw: seen.append(kw["relay"]) or {"registered": True})
    assert agent_init.run(_init_args()) == 0
    assert seen == [PUBLIC if launched else ""]


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
