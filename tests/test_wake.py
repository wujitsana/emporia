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
ALL_WAKING = set(FRAME_TYPES) - wake.NEVER_WAKES   # table talk wakes no one

#: sender kind -> (sender id, expected frame types that wake, per mode)
GAME = {"challenge", "accept", "reject"}
VISITED = {"session_started", "your_turn", "session_completed", "session_abandoned"}


def woken(mode, sender):
    return {t for t in FRAME_TYPES if wake.admits(mode, t, sender, LISTED)}


@pytest.mark.parametrize("mode", wake.MODES)
def test_the_relays_own_reports_wake_in_every_mode(mode):
    assert woken(mode, RELAY_SENDER) == ALL_WAKING


@pytest.mark.parametrize("mode", wake.MODES)
def test_a_listed_agent_wakes_it_in_every_mode(mode):
    assert woken(mode, "friend") == ALL_WAKING


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
    with pytest.raises(wake.WakeError, match="renamed to direct: run `emporia wake direct --agent NAME`"):
        wake.resolve({"EMPORIA_WAKE": "games"})
    assert wake.current({"EMPORIA_WAKE": "games"}) == "closed", "and it fails closed until the person changes it"
    assert "games" not in wake.MODES


def test_open_wakes_for_everything_from_anyone():
    for sender in ("stranger", "emporia:relay@other.example", "emporia:unknown-sender"):
        assert woken("open", sender) == ALL_WAKING


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


@pytest.mark.parametrize("command", ["emporia rounds status", "emporia wake", "hermes cron list"])
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
    assert "Wake mode   open" in out and "no restart is needed" in out
    assert cli.cmd_wake(argparse.Namespace(mode=None, agent="shop")) == 0


def test_a_mention_and_a_reply_in_one_thread_are_one_conversation():
    """Keyed by who mentions, a mention then a reply in one thread woke the agent twice 20 s apart (playtest 11-18):
    both are keyed by the thread's root. Mentions in different threads stay different conversations; the
    adapter's hourly limits still cap how many wake the agent."""
    from emporia.plugins.platforms.emporia.frames import frame_topic
    m = frame_topic({"type": "mention", "post_id": "p5", "author_id": "bob", "root_id": "p1", "reply_to": "post:p4"})
    r = frame_topic({"type": "post_reply", "post_id": "p6", "parent_id": "p5", "root_id": "p1", "author_id": "carol"})
    assert m == r == "post:p1"
    assert frame_topic({"type": "mention", "post_id": "p7", "author_id": "bob"}) == "post:p7", "a root mention"
    assert frame_topic({"type": "mention", "post_id": "p8", "author_id": "bob", "reply_to": "post:p1"}) == "post:p1"
    a = frame_topic({"type": "mention", "post_id": "p1", "author_id": "spammer"})
    b = frame_topic({"type": "mention", "post_id": "p2", "author_id": "spammer"})
    assert a != b and a.startswith("post:")
    import pathlib
    src = (pathlib.Path(__file__).parents[1] / "hermes_plugin" / "emporia" / "adapter.py").read_text()
    assert '"session:", "post:"' in src, "the adapter's hourly limit counts post threads too"



# -- the wake brake: a thread that just woke the agent waits; table talk never wakes ---

def test_table_talk_wakes_no_one_in_any_mode_and_waits_in_the_inbox(monkeypatch):
    talk = {"type": "game_chat", "agent_id": "mallory", "session_id": "s1", "content": "gg"}
    for mode in wake.MODES:
        got, lines = _run_bridge(monkeypatch, [talk], {"EMPORIA_WAKE": mode, "EMPORIA_ALLOWED_USERS": "mallory,*"})
        assert got == [False] and lines == [], mode
    # the move that follows still wakes, and says where the talk is
    got, lines = _run_bridge(monkeypatch, [talk, {"type": "your_turn", "session_id": "s1",
                                                 "module_type": "emporia:chess:v1",
                                                 "last_action": {"agent_id": "mallory", "action_type": "move",
                                                                 "say": "gg"}}], {"EMPORIA_WAKE": "direct"})
    assert got == [False, True] and "get_game_chat" in lines[0]["text"]


def test_the_agents_own_game_opening_notice_wakes_no_one_in_any_mode(monkeypatch):
    own = {"type": "session_created", "session_id": "s9", "module_type": "emporia:blotto:v2"}
    for mode in wake.MODES:
        assert not wake.admits(mode, "session_created", RELAY_SENDER, LISTED | {"*"}), mode
        got, lines = _run_bridge(monkeypatch, [own], {"EMPORIA_WAKE": mode, "EMPORIA_ALLOWED_USERS": "*"})
        assert got == [False] and lines == [], mode


def _reply(post_id, root="p0", author="bob"):
    return {"type": "post_reply", "post_id": post_id, "parent_id": root, "root_id": root, "author_id": author,
            "preview": "ok"}


def test_a_thread_that_just_woke_the_agent_holds_further_replies_unread(monkeypatch):
    got, lines = _run_bridge(monkeypatch, [_reply("p1"), _reply("p2"), _reply("p3", root="other")],
                             {"EMPORIA_WAKE": "direct"})
    assert got == [True, False, True], "the second reply in the same thread waits; another thread is not held"
    assert len(lines) == 2


def test_the_brake_lifts_after_the_cooldown_and_loses_nothing():
    now = [0.0]
    b = wake.ThreadBrake(clock=lambda: now[0])
    assert b.ready("post_reply", "post:p0")
    b.woke("post_reply", "post:p0")
    held = []
    for i in range(1, 4):                        # three replies at 10 s apart arrive during the cool-down
        now[0] = 10.0 * i
        if not b.ready("post_reply", "post:p0"):
            held.append(i)                       # the bridge leaves these unread: the inbox keeps them
    assert held == [1, 2, 3]
    now[0] = wake.THREAD_COOLDOWN_S + 1
    assert b.ready("post_reply", "post:p0"), "the next reply after the cool-down wakes again"
    # not held: other kinds, other threads
    assert b.ready("mention", "post:other") and b.ready("challenge", "post:p0") and b.ready("post_reply", None)


def test_held_replies_are_still_unread_and_listed_for_the_next_round(monkeypatch):
    """The tunnel marks a frame read only when on_frame returns true; a held one returns false."""
    got, _ = _run_bridge(monkeypatch, [_reply("p1"), _reply("p2")], {"EMPORIA_WAKE": "open"})
    assert got == [True, False]


def test_the_adapter_copy_of_never_wakes_is_the_same():
    import pathlib, re
    src = (pathlib.Path(__file__).parents[1] / "hermes_plugin" / "emporia" / "adapter.py").read_text()
    m = re.search(r"_WAKE_NEVER = frozenset\(\{([^}]*)\}\)", src)
    assert {x.strip().strip('"') for x in m.group(1).split(",") if x.strip()} == set(wake.NEVER_WAKES)


# -- a changed wake setting is picked up without a restart ---------------------

def test_the_watch_sees_a_changed_settings_file_by_its_time_and_reads_it_through_profile_env(tmp_path, monkeypatch):
    import os
    profile = tmp_path / "p"
    profile.mkdir()
    (profile / "config.yaml").write_text("{}\n")
    env = profile / ".env"
    env.write_text("EMPORIA_WAKE=direct\n")
    seen = {}

    def fake_read(path, *, fresh=False):
        seen["fresh"] = fresh
        return {"EMPORIA_WAKE": "open", "EMPORIA_ALLOWED_USERS": "friend"}

    from emporia import profile_env
    monkeypatch.setattr(profile_env, "read", fake_read)
    w = wake.Watch(str(env))
    assert not w.changed(), "an untouched file is only stat'ed"
    st = env.stat()
    os.utime(env, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))
    assert w.changed() and not w.changed()
    assert w.read() == ("open", frozenset({"friend"})) and seen["fresh"] is True
    assert not wake.Watch("").changed()
    monkeypatch.setattr(profile_env, "read", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no hermes")))
    assert w.read() == ("closed", frozenset()), "unreadable fails closed"


def test_the_bridge_applies_a_wake_change_to_the_next_frame_and_tells_the_adapter(monkeypatch, tmp_path):
    env = tmp_path / ".env"
    env.write_text("x=1\n")
    states = iter([("open", frozenset())])
    monkeypatch.setattr(wake.Watch, "changed", lambda self: True)
    monkeypatch.setattr(wake.Watch, "read", lambda self: next(states, ("open", frozenset())))
    dm = {"type": "dm_received", "sender_id": "mallory", "thread_id": "d", "content": "hi"}
    got, lines = _run_bridge(monkeypatch, [dm], {"EMPORIA_WAKE": "direct", "EMPORIA_DOTENV": str(env)})
    assert got == [True], "direct would have left the DM in the inbox; the changed setting (open) wakes"
    control = [l for l in lines if l.get("control") == "wake"]
    assert control and control[0]["mode"] == "open"


def test_the_adapter_takes_a_control_line_and_no_text_notice_forges_one():
    import pathlib
    src = (pathlib.Path(__file__).parents[1] / "hermes_plugin" / "emporia" / "adapter.py").read_text()
    assert 'data.get("control") != "wake"' in src and "if mode in WAKE_MODES" in src


# -- a reply the brake held is delivered once after the cool-down ---

def _bridge_with_clock(monkeypatch, script):
    """Run the bridge with a fake tunnel that plays `script(on_frame, now, marked)`; sleeps advance the fake clock."""
    monkeypatch.setenv("EMPORIA_WAKE", "direct")
    monkeypatch.delenv("EMPORIA_PLAYBOOK", raising=False)
    now = [0.0]
    marked = []

    async def fake_sleep(secs):
        await asyncio.sleep(0.1)         # a short real wait, then the fake clock jumps the whole cool-down
        now[0] += secs

    async def fake_mark(url, agent_id, profile_id, inbox_id):
        marked.append(inbox_id)

    async def fake_tunnel(url, agent_id, on_frame, profile_id=None):
        await script(on_frame, now, marked)
        await asyncio.sleep(0.6)         # real time: the timer and the delivery thread

    monkeypatch.setattr(bridge, "maintain_relay_tunnel", fake_tunnel)
    monkeypatch.setattr(bridge, "_check_local_relay", lambda url: "up")
    monkeypatch.setattr(bridge, "_sleep", fake_sleep)
    monkeypatch.setattr(bridge, "_mark_read", fake_mark)
    import io
    out = io.StringIO()
    asyncio.run(bridge.run("http://relay.test", "alice", None, out=out, poll_seconds=0.01, clock=lambda: now[0]))
    return [json.loads(x) for x in out.getvalue().splitlines()], marked


def test_a_held_reply_is_delivered_once_after_the_cooldown(monkeypatch):
    async def script(on_frame, now, marked):
        assert await on_frame(_reply("p1")) is True
        now[0] = 100.0
        assert await on_frame({**_reply("p2"), "inbox_id": "i2"}) is False
        await asyncio.sleep(0.01)
    lines, marked = _bridge_with_clock(monkeypatch, script)
    assert len(lines) == 2, "the held reply goes out as its own wake after the cool-down"
    assert marked == ["i2"], "and is marked read once handed over"


def test_a_mention_then_a_reply_in_one_thread_wake_once_and_the_reply_is_delivered_after_the_cooldown(monkeypatch):
    async def script(on_frame, now, marked):
        mention = {"type": "mention", "post_id": "p5", "author_id": "bob", "root_id": "p0", "preview": "hey @alice"}
        assert await on_frame(mention) is True
        now[0] = 20.0
        assert await on_frame({**_reply("p6", author="carol"), "inbox_id": "i6"}) is False, "held, not a second wake"
        await asyncio.sleep(0.01)
    lines, marked = _bridge_with_clock(monkeypatch, script)
    assert len(lines) == 2, "the mention, then the held reply once after the cool-down"
    assert marked == ["i6"] and "carol" in lines[1]["text"]


def test_two_held_replies_in_a_thread_deliver_one_line(monkeypatch):
    async def script(on_frame, now, marked):
        await on_frame(_reply("p1"))
        now[0] = 50.0
        await on_frame({**_reply("p2"), "inbox_id": "i2"})
        await on_frame({**_reply("p3"), "inbox_id": "i3"})
        await asyncio.sleep(0.01)
    lines, marked = _bridge_with_clock(monkeypatch, script)
    assert len(lines) == 2 and marked == ["i3"], "the newest held frame is the one wake; the other stays unread"


def test_a_held_reply_is_not_delivered_if_a_newer_wake_covered_the_thread(monkeypatch):
    async def script(on_frame, now, marked):
        await on_frame(_reply("p1"))
        now[0] = 50.0
        await on_frame({**_reply("p2"), "inbox_id": "i2"})       # held, timer waits on the fake clock
        await asyncio.sleep(0.01)                                 # the timer is now waiting out the cool-down
        now[0] = 400.0                                            # cool-down over just before it fires
        assert await on_frame(_reply("p3")) is True               # a live reply wakes the thread itself
        await asyncio.sleep(0.01)
    lines, marked = _bridge_with_clock(monkeypatch, script)
    assert len(lines) == 2 and marked == [], "p1 and p3 only: the held p2 is not delivered again"


def test_a_dm_thread_is_braked_like_a_reply_thread(monkeypatch):
    assert "dm_received" in wake.COOLDOWN_FRAMES
    now = [0.0]
    b = wake.ThreadBrake(clock=lambda: now[0])
    b.woke("dm_received", "dm:bob")
    now[0] = 10.0
    assert not b.ready("dm_received", "dm:bob"), "the answer to the answer waits"
    assert b.ready("dm_received", "dm:carol"), "another conversation is not held"
    now[0] = wake.THREAD_COOLDOWN_S + 1
    assert b.ready("dm_received", "dm:bob")


def test_a_held_dm_is_delivered_once_after_the_cooldown(monkeypatch):
    def dm(inbox):
        return {"type": "dm_received", "sender_id": "bob", "thread_id": "t1", "content": "hi", "inbox_id": inbox}

    async def script(on_frame, now, marked):
        assert await on_frame(dm("i1")) is True
        now[0] = 20.0
        assert await on_frame(dm("i2")) is False
        await asyncio.sleep(0.01)
    monkeypatch.setenv("EMPORIA_ALLOWED_USERS", "bob")      # a listed sender wakes in direct mode
    lines, marked = _bridge_with_clock(monkeypatch, script)
    assert len(lines) == 2 and marked == ["i2"]
