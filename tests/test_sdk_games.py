"""The Python SDK plays every game and the Chronicle: randomness commitments, Tesseract, the Chronicle and `open_sealed`.

Two or three `EmporiaAgent`s (no model, no MCP) talk to an in-process relay over the ASGI transport, on the worlds the
Tesseract and Chronicle tests use (a fake clock and the test beacon). The SDK's `_clock` is that fake clock, so a draw
that is 30 rounds off is published a moment later."""
from __future__ import annotations

import asyncio
import sys
import time
import uuid
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import e2e_chronicle as EC
import e2e_tesseract as ET
from conftest import fake_beacon_sig

from emporia import rand, rand_client
from emporia import relay_server as rs
from emporia.agent_sdk import EmporiaAgent
from emporia.identity import KEY_DIR, get_public_key_hex
from emporia.modules import tesseract as T

DAY = 86_400


def sdk(name: str, transport, clock=None) -> EmporiaAgent:
    a = EmporiaAgent(relay_url="http://testserver", agent_id=name, public_key_hex=get_public_key_hex(name),
                     client_name="sdk")
    a._http = httpx.AsyncClient(transport=transport, base_url="http://testserver")
    return a


@pytest.fixture
def tess(monkeypatch):
    world = ET.World(TestClient(rs.app, client=("127.0.0.1", 51781)))

    async def sleep(s):
        world.clock.advance(s)
    monkeypatch.setattr(EmporiaAgent, "_clock", staticmethod(world.clock))
    monkeypatch.setattr(EmporiaAgent, "_sleep", staticmethod(sleep))
    monkeypatch.setattr(EmporiaAgent, "_blocking_sleep", staticmethod(world.clock.advance))
    monkeypatch.setattr(EmporiaAgent, "_rand_fetch", staticmethod(lambda rnd, base=None: fake_beacon_sig(rnd)))
    yield world
    world.close()


def test_an_sdk_table_of_three_plays_tesseract_with_commitments_seals_and_open_sealed(tess, monkeypatch):
    w = tess
    tag = uuid.uuid4().hex[:6]
    names = [w.register(f"sg{tag}_{i}") for i in range(3)]

    async def scenario():
        transport = httpx.ASGITransport(app=rs.app)
        agents = [sdk(n, transport) for n in names]
        # the creator and the others carry a signed rand commitment, kept in the key folder
        made = await agents[0].create_session(T.GAME_ID, config={"players": 3})
        sid = made["session_id"]
        for a in agents[1:]:
            await a.join_session(sid)
        state = w.state(sid)
        assert state["status"] == "active"
        commits = rand_client._commits(state)
        assert set(commits) == set(names)
        for a in agents:
            rec = __import__("json").loads(a._rand_path(sid).read_text())
            assert rec["hash"] == commits[a.agent_id]["hash"] and rec["s"] and rec["nonce"]
            assert rec["beacon_round"] >= rand.round_at(w.clock.t) + rand.CLIENT_LEAD_ROUNDS - 1
        assert (await agents[0].tesseract())["success"]            # the overview
        for e in range(1, T.EPOCHS + 1):
            w.clock.advance(120 if e == 1 else 20)       # the first round waits for the commitments' drand round
            for a in agents:
                v = await a.tesseract(sid)
                assert v.get("ready"), v
                r = await a.tesseract_push(sid, ET.lite(v, None))
                assert r["success"], r
            if e == 1:
                # one agent opens by hand, the others by `open_sealed`
                r = await agents[0].tesseract_open(sid)
                assert r["success"] and r.get("opened"), r
                skipped = await agents[0].open_sealed()
                assert skipped["tesseract"][sid] in ("skipped", "done")
                rest = agents[1:]
            else:
                rest = agents
            for a in rest:
                out = await a.open_sealed()
                assert out["tesseract"][sid] == "done", out
                assert out["done"], out
            await asyncio.to_thread(w.sweep)
        w.clock.advance(20)
        for a in agents:
            out = await a.open_sealed()                 # the secret, after round 5
            assert out["tesseract"][sid] == "done", out
        # nothing is left to do, and doing it again sends nothing
        again = await agents[0].open_sealed()
        assert again["tesseract"][sid] in ("skipped", "done") and not again["done"]
        return sid, agents

    sid, agents = asyncio.run(scenario())
    w.sweep()
    assert w.state(sid)["status"] == "completed"


def test_the_secret_file_is_the_one_the_mcp_tools_read(tess, monkeypatch):
    from emporia import mcp_server
    monkeypatch.setattr(mcp_server, "RELAY_URL", "http://testserver")
    name = f"sgf{uuid.uuid4().hex[:6]}"
    tess.register(name)
    a = sdk(name, httpx.ASGITransport(app=rs.app))
    assert a._rand_path("sess_1") == mcp_server._commitments_file(name, "sess_1").with_suffix(".rand.json")
    assert a._commitments_path("sess_1").with_suffix(".tess.json") == \
        mcp_server._commitments_file(name, "sess_1").with_suffix(".tess.json")
    commit = a._rand_commit("sess_1", 0)
    assert commit["beacon_round"] >= rand.round_at(tess.clock.t) + rand.CLIENT_LEAD_ROUNDS
    # what the MCP tools would write and read is the same dict
    with mcp_server._commitments(mcp_server._rand_file(name, "sess_1")) as saved:
        assert saved["hash"] == commit["hash"] and rand_client.reveal_payload(dict(saved))


def test_every_beacon_game_is_created_with_a_commitment_and_a_reveal_carries_the_kept_secret(tess):
    from emporia.module_sdk import MODULE_REGISTRY, load_bundled_modules
    load_bundled_modules()
    games = sorted(t for t, c in MODULE_REGISTRY.items() if getattr(c, "USES_RAND", False) and t != "emporia:sluice:v1")   # (sluice is the old name of spillway)
    assert len(games) >= 5, games
    name = f"sgr{uuid.uuid4().hex[:6]}"
    tess.register(name)

    async def scenario():
        a = sdk(name, httpx.ASGITransport(app=rs.app))
        made = {}
        for g in games:
            cfg = {"players": 3} if g == T.GAME_ID else {}
            r = await a.create_session(g, config=cfg)
            made[g] = r["session_id"]
        for g, sid in made.items():
            assert rand_client._commits(tess.state(sid)).get(name), g        # the relay holds this agent's commitment
            payload = await a._rand_fill(sid, tess.state(sid), "reveal", {})
            assert set(payload) == {"s", "nonce"}, g
            assert rand.commit_hash(bytes.fromhex(payload["s"]), bytes.fromhex(payload["nonce"])) == \
                rand_client._commits(tess.state(sid))[name]["hash"]
        return made
    assert len(asyncio.run(scenario())) == len(games)


def test_a_bargain_v2_joiner_commits_and_reads_its_own_hand(tess):
    names = [tess.register(f"sgb{uuid.uuid4().hex[:5]}_{i}") for i in range(2)]

    async def scenario():
        transport = httpx.ASGITransport(app=rs.app)
        a, b = sdk(names[0], transport), sdk(names[1], transport)
        sid = (await a.create_session("emporia:bargain:v2"))["session_id"]
        await b.join_session(sid)
        assert set(rand_client._commits(tess.state(sid))) == set(names)
        tess.clock.advance(120)                                    # the draw's round is published
        mine, theirs = await a.game_private(sid), await b.game_private(sid)
        return mine, theirs
    mine, theirs = asyncio.run(scenario())
    assert mine.get("values") and theirs.get("values") and mine["values"] != {}, (mine, theirs)


# ── the Chronicle ──────────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def book(monkeypatch):
    base = int(time.time() // DAY) * DAY
    world = EC.World(TestClient(rs.app, client=("127.0.0.1", 51782)), epoch_s=DAY, start=base - 400 * DAY)
    yield world
    world.close()


def test_an_sdk_agent_seals_a_chronicle_passage_and_open_sealed_opens_it(book):
    w = book
    tag = uuid.uuid4().hex[:5]
    names = [w.register(f"sgc{tag}_{i}", f"opsg{tag}", age_days=60) for i in range(3)]
    w.seed(names[0])

    async def scenario():
        transport = httpx.ASGITransport(app=rs.app)
        agents = {n: sdk(n, transport) for n in names}
        w.clock.at(w.start - 3600)
        for a in agents.values():
            assert (await a.chronicle(join=True))["success"]
        w.activity(w.start + 0.8 * DAY, names)            # the day's record the passage cites
        call = w.start + DAY
        w.clock.at(call + 0.04 * DAY)
        views = {n: await a.chronicle() for n, a in agents.items()}
        chapter = next(v["chapter"] for v in views.values() if v.get("chapter"))
        card = w.get(f"/chronicle/chapters/{chapter}").json()["card"]
        cites = EC.pick_cites(w, chapter)
        writers = [n for n, v in views.items() if v.get("due") == "write"]
        assert writers, views
        for n in writers:
            r = await agents[n].chronicle_write(EC.scripted_passage(card), cites, confirm=True)
            assert r.get("success"), r
        assert (await agents[writers[0]].open_sealed())["chronicle"] == "skipped"      # sealed, not yet in the Open window
        w.clock.at(call + 0.40 * DAY)
        first = await agents[writers[0]].chronicle_reveal()                              # by hand
        assert first.get("opened"), first
        outs = [await agents[n].open_sealed() for n in writers[1:]]                      # by the loop's helper
        assert all(o["chronicle"] == "done" for o in outs), outs
        assert all(o["done"] for o in outs)
        # the pick: a juror seals one and `open_sealed` opens it in the Count window
        w.clock.at(call + 0.65 * DAY)
        picks = []
        for n, a in agents.items():
            v = await a.chronicle()
            if v.get("due") == "pick":
                cands = w.get(f"/chronicle/chapters/{chapter}").json().get("candidates") or []
                r = await a.chronicle_vote(cands[0] if cands else "none", reason="it follows")
                assert r.get("success"), r
                picks.append(n)
        w.clock.at(call + 0.90 * DAY)
        for n in picks:
            out = await agents[n].open_sealed()
            assert out["chronicle"] == "done", out
        return writers, picks
    writers, picks = asyncio.run(scenario())
    assert writers


def test_a_relay_without_the_chronicle_is_said_to_be_older(monkeypatch):
    from emporia.agent_sdk import _not_on_this_relay
    out = _not_on_this_relay({"success": False, "error": "Relay 404", "detail": "Not Found"}, "the Chronicle")
    assert "does not have the Chronicle" in out["error"]
    assert KEY_DIR  # the key folder the sealed files live in is the suite's throwaway one
