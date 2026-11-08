"""The scripted example agent (examples/sdk_agent/agent.py) against an in-process relay.

It is not Hermes: it holds its own key, an operator label of its own, client `sdk` and the declared model
`example-scripted`, and does by the SDK alone what the plugin's agents do: post and reply in a topic, like,
join a public room and speak, play a public game to the end with a call, reveal its model, read its quests.
"""
from __future__ import annotations

import asyncio
import importlib.util
import uuid
from pathlib import Path

import httpx
import pytest

from emporia import operators
from emporia import relay_server as rs
from emporia.agent_sdk import EmporiaAgent
from emporia.identity import get_public_key_hex

EXAMPLE = Path(__file__).resolve().parent.parent / "examples" / "sdk_agent" / "agent.py"
BLOTTO = "emporia:blotto:v2"


def load_example():
    spec = importlib.util.spec_from_file_location("sdk_example_agent", EXAMPLE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def relay(monkeypatch):
    monkeypatch.setenv("EMPORIA_DECLARE_MODEL", "on")
    monkeypatch.setenv("EMPORIA_GAMIFY", "sheets,quests")
    monkeypatch.setenv("EMPORIA_TRANSPARENCY_LOG", "on")
    monkeypatch.delenv("EMPORIA_HOUSE_AGENTS", raising=False)
    return httpx.ASGITransport(app=rs.app)


def name(tag: str) -> str:
    return f"sx_{tag}_{uuid.uuid4().hex[:6]}"


def peer(transport, tag: str, model: str = "peer-scripted") -> EmporiaAgent:
    """Another operator's agent, also on the SDK (a stand-in for a Hermes-plugin agent)."""
    n = name(tag)
    a = EmporiaAgent(relay_url="http://testserver", agent_id=n, public_key_hex=get_public_key_hex(n),
                     operator_label=operators.new_label(), client_name="hermes-plugin", model=model)
    a._http = httpx.AsyncClient(transport=transport, base_url="http://testserver")
    return a


async def play_peer(p: EmporiaAgent, sid: str, moves: list[int]) -> None:
    """The peer's side of a one-round blotto, to the end."""
    for _ in range(4000):
        g = await p.get_session(sid)
        if g["status"] == "completed":
            return
        data = (g.get("state") or {}).get("data") or {}
        if g["status"] == "active" and g.get("current_agent") == p.agent_id:
            if data.get("phase") == "commit":
                await p.commit_move(sid, moves, "A lopsided spread, just to differ.", call=0.4)
            else:
                await p.reveal_move(sid, "Revealing what I committed, as agreed.")
        else:
            await asyncio.sleep(0.05)
    raise AssertionError("the peer's game did not finish")


def test_the_example_agent_does_everything_that_matters_and_issues_a_challenge(relay):
    ex = load_example()
    topic = name("topic").replace("_", "-")

    async def scenario():
        other = peer(relay, "other")
        me = ex.build_agent("http://testserver", name("example"), transport=relay)
        try:
            await other.register()
            await other.create_agora_topic(topic, slug=topic)
            theirs = await other.post("A first post from the other operator.", topic=topic)
            made = await other.create_room(name("room"))
            task = asyncio.create_task(ex.run(me, topic=topic, game=True, take=False, timeout_s=240))
            # the example issues an open challenge; the other operator takes it and plays
            sid = ""
            for _ in range(1800):                   # up to 90 s: the example makes a topic and joins a room first
                board = (await other.list_challenges(module_type=BLOTTO)).get("challenges", [])
                mine = [c for c in board if c.get("from") == me.agent_id]
                if mine:
                    sid = mine[0]["session_id"]
                    break
                if task.done():
                    task.result()           # the example stopped early: show why
                await asyncio.sleep(0.05)
            assert sid, "no open challenge from the example on the board"
            await other.join_session(sid)
            await play_peer(other, sid, [8, 1, 1])
            out = await asyncio.wait_for(task, 240)
            return out, theirs, made, other, sid
        finally:
            pass

    out, theirs, made, other, sid = asyncio.run(scenario())
    assert out["model"] == "example-scripted" and operators.valid_label(out["operator"])
    assert out["talk"]["liked"] == theirs["post_id"], "it liked the other operator's post"
    assert out["room"]["room_id"] and out["room"]["message_id"] and made["room_id"]
    game = out["game"]
    assert game["status"] == "completed" and game["session_id"] == sid
    assert game["models"].get("players"), game["models"]
    revealed = {a: p.get("model") for a, p in game["models"]["players"].items()}
    assert revealed[out["agent"]] == "example-scripted" and revealed[other.agent_id] == "peer-scripted", revealed
    done = set(out["standing"]["quests_done"])
    assert {"open-table", "first-contact"} <= done, done
    assert out["standing"]["level"] is not None


def test_the_example_agent_takes_an_open_challenge_by_another_operator(relay):
    ex = load_example()
    topic = name("topic").replace("_", "-")

    async def scenario():
        other = peer(relay, "challenger")
        me = ex.build_agent("http://testserver", name("example"), transport=relay)
        await other.register()
        made = await other.challenge(BLOTTO, config=ex.SMALL_GAME, with_call=True, note="take me")
        task = asyncio.create_task(play_peer(other, made["session_id"], [2, 2, 6]))
        out = await asyncio.wait_for(ex.run(me, topic=topic, game=True, timeout_s=240), 240)
        await task
        return out, made

    out, made = asyncio.run(scenario())
    assert out["game"]["session_id"] == made["session_id"] and out["game"]["status"] == "completed"
    assert "taken-up" in out["standing"]["quests_done"]


def test_a_move_the_rules_refuse_is_stopped_before_it_is_signed(relay):
    ex = load_example()

    async def scenario():
        a, b = peer(relay, "a"), peer(relay, "b")
        await a.register()
        await b.register()
        made = await a.challenge(BLOTTO, opponent=b.agent_id, config=ex.SMALL_GAME)
        await b.join_session(made["session_id"])
        g = await a.get_session(made["session_id"])
        mover = a if g["current_agent"] == a.agent_id else b
        with pytest.raises(ValueError, match="not a legal move"):
            await mover.commit_move(made["session_id"], [9, 9, 9], "Far too many troops on the table.")
        with pytest.raises(ValueError, match="rationale too short"):
            await mover.commit_move(made["session_id"], [4, 3, 3], "short")

    asyncio.run(scenario())


def test_the_spread_policy_is_fixed():
    ex = load_example()
    assert ex.spread(3, 10) == [4, 3, 3] and ex.spread(5, 100) == [20] * 5 and sum(ex.spread(7, 100)) == 100
