"""Wake mode: which relay events start a turn (closed, direct, open), enforced in the bridge and the adapter."""
import asyncio
import json
import os
import subprocess
from pathlib import Path

import pytest

from emporia import wake
from emporia.plugins.platforms.emporia import bridge
from emporia.plugins.platforms.emporia.frames import RELAY_SENDER

REPO = Path(__file__).resolve().parents[1]
HERMES_PYTHON = Path("/opt/hermes/.venv/bin/python")

FRAME_TYPES = ["challenge", "counter_offer", "accept", "reject", "session_created", "session_started",
               "session_completed", "session_abandoned", "your_turn", "game_chat", "dm_received",
               "room_message", "room_invite", "agora_invite", "kicked_from_room", "agora_post_created",
               "funding_required", "room_join_unsigned"]
LISTED = frozenset({"friend"})

#: sender kind -> (sender id, expected frame types that wake, per mode)
GAME = {"challenge", "accept", "reject", "game_chat"}
VISITED = {"session_started", "your_turn", "session_completed", "session_abandoned"}


def woken(mode, sender):
    return {t for t in FRAME_TYPES if wake.admits(mode, t, sender, LISTED)}


@pytest.mark.parametrize("mode", wake.MODES)
def test_the_relays_own_reports_wake_in_every_mode(mode):
    assert woken(mode, RELAY_SENDER) == set(FRAME_TYPES)


@pytest.mark.parametrize("mode", wake.MODES)
def test_a_listed_agent_wakes_it_in_every_mode(mode):
    assert woken(mode, "friend") == set(FRAME_TYPES)


def test_closed_wakes_for_nobody_else():
    assert woken("closed", "stranger") == set()
    assert woken("closed", "emporia:relay@other.example") == set()


def test_direct_wakes_for_a_strangers_game_frames_only():
    assert woken("direct", "stranger") == GAME
    for t in ("dm_received", "room_message", "room_invite", "agora_invite", "agora_post_created", "counter_offer"):
        assert t not in woken("direct", "stranger")


def test_direct_hears_a_visited_relays_game_reports_and_nothing_else_of_it():
    assert woken("direct", "emporia:relay@other.example") == VISITED
    assert woken("direct", "stranger@other.example") == GAME


TALK = ["post_reply", "mention", "followed", "followed_activity", "topic_activity", "watched_game"]
RELAY_TALK = {"followed", "followed_activity", "topic_activity", "watched_game"}


def test_direct_admits_what_is_addressed_to_the_agent():
    # replies and mentions are an agent's words: from that agent, anyone
    assert wake.admits("direct", "post_reply", "stranger") and wake.admits("direct", "mention", "stranger")
    # the relay's reports about other agents
    for t in RELAY_TALK:
        assert wake.admits("direct", t, RELAY_SENDER), t
        assert not wake.admits("closed", t, RELAY_SENDER), t
    for t in ("post_reply", "mention"):
        assert not wake.admits("closed", t, "stranger"), t
    # but a visited relay's talk is not home's
    for t in TALK:
        assert not wake.admits("direct", t, "emporia:relay@other.example"), t
    # a listed sender and open are unchanged
    assert wake.admits("closed", "followed_activity", "friend", LISTED)
    for t in TALK:
        assert wake.admits("open", t, "anyone"), t


def test_the_retired_games_mode_is_a_loud_error_that_names_the_fix():
    with pytest.raises(wake.WakeError, match="renamed to direct: run `emporia wake direct`"):
        wake.resolve({"EMPORIA_WAKE": "games"})
    assert wake.current({"EMPORIA_WAKE": "games"}) == "closed", "and it fails closed until the person changes it"
    assert "games" not in wake.MODES


def test_open_wakes_for_everything_from_anyone():
    for sender in ("stranger", "emporia:relay@other.example", "emporia:unknown-sender"):
        assert woken("open", sender) == set(FRAME_TYPES)


def test_a_wildcard_in_the_allowlist_lists_everyone():
    assert wake.admits("closed", "dm_received", "anyone", frozenset({"*"}))


def test_the_mode_defaults_to_direct():
    assert wake.resolve({}) == "direct"
    assert wake.resolve({"EMPORIA_ALLOW_ALL_USERS": "true"}) == "direct"   # no such switch: wake mode is the only one
    with pytest.raises(wake.WakeError):
        wake.resolve({"EMPORIA_WAKE": "sometimes"})
    assert wake.current({"EMPORIA_WAKE": "sometimes"}) == "closed", "unreadable fails closed"


# -- the bridge: what is written for the gateway, and what is left unread -------

def _run_bridge(monkeypatch, frames, env):
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("EMPORIA_PLAYBOOK", raising=False)
    got = []

    async def fake_tunnel(url, agent_id, on_frame, profile_id=None):
        got.extend([await on_frame(f) for f in frames])

    monkeypatch.setattr(bridge, "maintain_relay_tunnel", fake_tunnel)
    monkeypatch.setattr(bridge, "_check_local_relay", lambda url: "up")
    import io
    out = io.StringIO()
    asyncio.run(bridge.run("http://relay.test", "alice", None, out=out, poll_seconds=0.01))
    return got, [json.loads(line) for line in out.getvalue().splitlines()]


FRAMES = [
    {"type": "challenge", "from_agent": "mallory", "payload": {"session_id": "s1"}},
    {"type": "dm_received", "sender_id": "mallory", "thread_id": "dm_1", "content": "hello"},
    {"type": "your_turn", "session_id": "s1", "module_type": "emporia:chess:v1"},
    {"type": "dm_received", "sender_id": "friend", "thread_id": "dm_2", "content": "hi"},
]


@pytest.mark.parametrize("mode,expected", [("closed", [False, False, True, True]),
                                           ("direct", [True, False, True, True]),
                                           ("open", [True, True, True, True])])
def test_the_bridge_passes_on_only_what_the_mode_admits(monkeypatch, mode, expected):
    got, lines = _run_bridge(monkeypatch, FRAMES, {"EMPORIA_WAKE": mode, "EMPORIA_ALLOWED_USERS": "emporia:relay,friend"})
    assert got == expected, "a refused frame is not marked read: it waits in the inbox"
    assert len(lines) == sum(expected)


def test_attribution_is_the_same_in_every_mode(monkeypatch):
    got, lines = _run_bridge(monkeypatch, FRAMES[:2], {"EMPORIA_WAKE": "open", "EMPORIA_ALLOWED_USERS": "emporia:relay"})
    assert [line["sender"] for line in lines] == ["mallory", "mallory"]
    assert all(line["text"].startswith("[Emporia]") for line in lines)


def test_the_playbook_still_bounds_what_the_mode_admits(monkeypatch):
    got, _ = _run_bridge(monkeypatch, FRAMES[1:2], {"EMPORIA_WAKE": "open", "EMPORIA_ALLOWED_USERS": "emporia:relay",
                                                    "EMPORIA_PLAYBOOK_ANSWERS": "nobody"})
    assert got == [False]


# -- the adapter keeps its own copy of the table, and tells the gateway who passed ---

@pytest.mark.skipif(not HERMES_PYTHON.exists(), reason="Hermes is not installed here")
def test_the_adapters_copy_of_the_table_equals_the_bridges(tmp_path):
    harness = tmp_path / "h.py"
    harness.write_text(f'''
import importlib.util, json, sys
from pathlib import Path
d = Path({str(REPO / "hermes_plugin" / "emporia")!r})
spec = importlib.util.spec_from_file_location("emporia_platform", d / "__init__.py", submodule_search_locations=[str(d)])
mod = importlib.util.module_from_spec(spec); sys.modules["emporia_platform"] = mod
spec.loader.exec_module(mod)
from gateway.platform_registry import PlatformEntry, platform_registry
class Ctx:
    def register_platform(self, **kw): platform_registry.register(PlatformEntry(**kw))
    def register_hook(self, *a, **kw): pass
    def register_skill(self, *a, **kw): pass
    def register_cli_command(self, *a, **kw): pass
    def register_command(self, *a, **kw): pass
mod.register(Ctx())
from emporia_platform.adapter import wake_admits, EmporiaAdapter
from gateway.config import PlatformConfig
types = {FRAME_TYPES!r}
senders = ["emporia:relay", "friend", "stranger", "stranger@other.example", "emporia:relay@other.example", "emporia:unknown-sender"]
table = {{m: {{s: [t for t in types if wake_admits(m, t, s, frozenset({{"friend"}}))] for s in senders}} for m in ("closed","direct","open")}}
def adapter(extra):
    return EmporiaAdapter(PlatformConfig(enabled=True, extra={{"agent_id": "a", "python": "/x", **extra}}))
modes = [adapter({{}}).wake_mode, adapter({{"wake": "open"}}).wake_mode, adapter({{"wake": "bogus"}}).wake_mode]
a = adapter({{}})
a._admit("stranger", now=-10000.0)
lapsed = sorted(a.resolved_allowlist_user_ids())
a._admit("stranger")
print(json.dumps({{"table": table, "modes": modes, "lapsed": lapsed, "now": sorted(a.resolved_allowlist_user_ids())}}))
''')
    out = subprocess.run([str(HERMES_PYTHON), str(harness)], cwd="/opt/hermes",
                         env={**os.environ, "HERMES_HOME": str(tmp_path / "home"), "PYTHONPATH": "/opt/hermes"},
                         capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr[-2000:]
    r = json.loads(out.stdout.strip().splitlines()[-1])
    for mode, per in r["table"].items():
        for sender, types in per.items():
            assert set(types) == {t for t in FRAME_TYPES if wake.admits(mode, t, sender, LISTED)}, (mode, sender)
    assert r["modes"] == ["direct", "open", "closed"]
    assert r["lapsed"] == [] and r["now"] == ["stranger"], "a sender passes the gateway only just after its notice did"


# -- person-only: the key guard holds wake and rounds from an agent's tools ---------

def _guard():
    import importlib.util
    spec = importlib.util.spec_from_file_location("emporia_key_guard_w", REPO / "hermes_plugin" / "emporia" / "key_guard.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.parametrize("command", [
    "emporia wake open", "emporia wake closed --agent shop", "hermes -p shop emporia wake direct",
    "emporia rounds on --every 2h", "emporia rounds off", "hermes emporia rounds pause", "emporia rounds resume",
    "hermes -p shop cron remove abc123def456 # Emporia rounds",
    "hermes cron edit 123 --schedule 'every 1h' --prompt x  # emporia",
])
def test_an_agent_cannot_change_wake_or_rounds(command):
    out = _guard().pre_tool_call(tool_name="terminal", args={"command": command})
    assert out and out["action"] == "block", command


@pytest.mark.parametrize("args", [
    {"command": "echo EMPORIA_WAKE=open >> /opt/profiles/shop/.env"},
    {"command": "hermes config set EMPORIA_WAKE open"},
    {"path": "/home/x/.hermes/.env", "content": "EMPORIA_WAKE=open"},
])
def test_an_agent_cannot_write_the_wake_settings(args):
    out = _guard().pre_tool_call(tool_name="terminal" if "command" in args else "write_file", args=args)
    assert out and out["action"] == "block", args


@pytest.mark.parametrize("command", ["emporia rounds status", "emporia wake", "hermes cron list",
                                     "hermes cron create 'every 1h' 'water the plants'"])
def test_reading_the_state_and_other_cron_jobs_pass(command):
    assert _guard().pre_tool_call(tool_name="terminal", args={"command": command}) is None


def test_the_wake_cli_writes_the_setting_and_shows_it(tmp_path, monkeypatch, capsys):
    import argparse

    from emporia import cli
    shop = tmp_path / "profiles" / "shop"
    shop.mkdir(parents=True)
    (shop / "config.yaml").write_text("{}")
    written = {}
    monkeypatch.setattr(cli, "profile_dir", lambda name: shop)
    monkeypatch.setattr(cli.installer(), "harvest_env", lambda p: dict(written))
    monkeypatch.setattr(cli, "merge_env_file", lambda path, updates: written.update(updates))
    assert cli.cmd_wake(argparse.Namespace(mode="open", agent="shop")) == 0
    assert written == {"EMPORIA_WAKE": "open"}
    out = capsys.readouterr().out
    assert "Wake mode   open" in out and "gateway restart" in out
    assert cli.cmd_wake(argparse.Namespace(mode=None, agent="shop")) == 0


def test_mentions_by_one_author_are_one_conversation_for_the_hourly_limit():
    """Forty posts that name the agent must not be forty fresh conversations (each a turn)."""
    from emporia.plugins.platforms.emporia.frames import frame_topic
    a = frame_topic({"type": "mention", "post_id": "p1", "author_id": "spammer"})
    b = frame_topic({"type": "mention", "post_id": "p2", "author_id": "spammer"})
    assert a == b and a.startswith("post:")
    import importlib.util, pathlib
    src = (pathlib.Path(__file__).parents[1] / "hermes_plugin" / "emporia" / "adapter.py").read_text()
    assert '"session:", "post:"' in src, "the adapter's hourly limit counts post threads too"
