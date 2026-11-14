#!/usr/bin/env python3
"""Chronicle T1, end to end: six scripted agents (no model) play three chapters on a throwaway relay.

    EMPORIA_NO_DOTENV=1 python scripts/e2e_chronicle.py [--chapters 3] [--no-bls]

The relay is this repo's own, in process, on a throwaway database and key folder (the test suite's pinned
environment, `tests/conftest.py`), with the test beacon (every drand round a fixed fake signature) and a fake clock
that runs days in seconds. The agents use the four real tools (`chronicle`, `chronicle_write`, `chronicle_vote`,
`chronicle_reveal`, as `mcp_server` defines them) over the relay's REST routes: nothing is called around them.
Posts and replies that make the day's record are signed with the fake clock's time. After each chapter the script runs
`scripts/verify_chronicle.py` against the relay and prints whether it agrees. Nothing leaves this machine.

`World` is also what `tests/test_chronicle_relay.py` plays with.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
for p in (REPO / "src", REPO / "tests", REPO / "scripts"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import conftest  # noqa: E402,F401 — pins the suite's environment on a throwaway root, before emporia loads

DAY = 86_400


def fake_beacon_sig(rnd: int) -> str:
    """The test beacon: a stand-in 48-byte signature for a round (as `tests/conftest.fake_beacon_sig`)."""
    return hashlib.sha512(f"test-beacon-{rnd}".encode()).hexdigest()[:96]


class Clock:
    """The fake clock the relay, the tools and the signed posts all read."""

    def __init__(self, t: float):
        self.t = t

    def __call__(self) -> float:
        return self.t

    def at(self, t: float) -> None:
        self.t = t

    def iso(self) -> str:
        return datetime.fromtimestamp(self.t, UTC).isoformat()


class World:
    """A throwaway relay with the Chronicle on, scripted agents, and the fake clock and beacon."""

    def __init__(self, client, *, epoch_s: int = DAY, start: float | None = None, monkeypatch=None):
        from emporia import deletion
        from emporia import relay_server as rs
        from emporia import chronicle_client
        from emporia.routes import beacon as beacon_route
        from emporia.routes import chronicle as route
        self.client, self.rs, self.route, self.epoch = client, rs, route, epoch_s
        real = datetime.now(UTC).timestamp()
        self.start = start if start is not None else (real // epoch_s) * epoch_s - 60 * DAY
        self.clock = Clock(self.start - 3 * 3600)
        with rs.DB_LOCK, rs.get_db() as conn:        # a throwaway book: nothing of an earlier one is on this roster
            for t in ("enrol", "seals", "reveals", "ballots", "verdicts"):
                conn.execute(f"DELETE FROM chronicle_{t}")  # noqa: S608
            conn.commit()
        self._env: dict[str, str | None] = {}
        for k, v in {"EMPORIA_CHRONICLE": "1", "EMPORIA_GAMIFY": "sheets,quests", "EMPORIA_TRANSPARENCY_LOG": "on",
                     "EMPORIA_CHRONICLE_START": datetime.fromtimestamp(self.start, UTC).isoformat(),
                     "EMPORIA_CHRONICLE_EPOCH": str(epoch_s)}.items():
            self._env[k] = os.environ.get(k)
            os.environ[k] = v
        self._undo: list = []

        def patch(obj, name, value):
            self._undo.append((obj, name, getattr(obj, name)))
            setattr(obj, name, value)
        patch(route, "now", self.clock)
        patch(route, "_BEACONS", {})
        patch(route, "_SEQ", {"key": None, "value": None})
        patch(chronicle_client, "clock", lambda: datetime.fromtimestamp(self.clock.t, UTC))
        patch(beacon_route, "signature", fake_beacon_sig)
        patch(deletion, "FRESH", timedelta(days=36500))
        patch(rs.AGENT_REGISTRY, "check_rate_limit", lambda agent_id, *a: True)      # scripted agents write fast      # posts and replies are signed with the fake clock
        self.agents: dict[str, dict] = {}
        self.keeper = ""

    def close(self) -> None:
        for obj, name, old in reversed(self._undo):
            setattr(obj, name, old)
        self._undo.clear()
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self.rs.forget_who()

    # ── agents ──────────────────────────────────────────────────────────────────────────────────
    def token(self, agent: str) -> dict[str, str]:
        rs = self.rs
        now = datetime.now(UTC).timestamp()
        stable = rs.AGENT_REGISTRY.get_stable_id(agent) or ""
        tok = rs._jwt_encode({"agent_id": agent, "stable_id": stable, "relay_id": rs.relay_id(), "iat": int(now),
                              "issued": now, "exp": int(now + 3600)})
        return {"Authorization": f"Bearer {tok}"}

    def register(self, name: str, operator: str, *, age_days: float = 100, house: bool = False) -> str:
        from emporia.identity import get_public_key_hex
        r = self.client.post("/agents/register", json={"agent_id": name, "public_key_hex": get_public_key_hex(name)})
        assert r.status_code == 200, r.text
        registered = datetime.fromtimestamp(self.start - age_days * DAY, UTC).isoformat()
        with self.rs.DB_LOCK, self.rs.get_db() as conn:
            conn.execute("UPDATE authorized_agents SET operator_label = ?, registered_at = ? WHERE agent_id = ?",
                         (operator, registered, name))
            conn.commit()
        self.rs.forget_who()
        self.agents[name] = {"operator": operator, "house": house}
        return name

    def stable(self, agent: str) -> str:
        with self.rs.DB_LOCK, self.rs.get_db() as conn:
            return conn.execute("SELECT stable_id FROM authorized_agents WHERE agent_id = ?", (agent,)).fetchone()[0]

    def tools(self, agent: str):
        """The four tools as `agent` runs them, over this relay: a context manager returning a namespace."""
        return _AsAgent(self, agent)

    # ── the book's topic and prologue ──────────────────────────────────────────────────────────
    def seed(self, keeper: str) -> str:
        """The chronicle topic and the prologue, by `keeper` (the relay operator's role)."""
        from emporia import identity, wire
        self.keeper = keeper
        with self.rs.DB_LOCK, self.rs.get_db() as conn:
            have = self.route.prologue_ref(conn)
        if have:                      # a relay seeds once; a later throwaway book on the same database reuses it
            return have.removeprefix("post:")
        h = self.token(keeper)
        body = {"name": "Chronicle", "description": "The relay's book.", "slug": "chronicle",
                "nonce": wire.new_message_nonce()}
        body["signature"] = identity.sign(wire.signed_agora_topic_payload(
            body["nonce"], "chronicle", "Chronicle", "The relay's book.", "public", "open", 0, [], None, False), keeper)
        r = self.client.post("/agoras/topics", headers=h, json=body)
        assert r.status_code == 200, r.text
        return self.post(keeper, "The Chronicle. This relay keeps a book of its own history.", kind="other",
                         label="prologue", topic="chronicle", tags=[["t", "chronicle"], ["t", "prologue"]])

    def post(self, agent: str, text: str, *, kind: str = "note", label: str = "", topic: str | None = None,
             reply_to: str | None = None, tags=None) -> str:
        """A signed post at the fake clock's time; a reply names the root of its parent's thread."""
        from emporia import identity, refs, wire
        rs = self.rs
        nonce, at = wire.new_message_nonce(), self.clock.iso()
        root = None
        if reply_to:
            with rs.DB_LOCK, rs.get_db() as conn:
                row = conn.execute("SELECT root FROM posts WHERE post_id = ?", (refs.parse(reply_to)["id"],)).fetchone()
            root = wire.root_expected(reply_to, row["root"] if row else None)
        tags = tags or []
        sig = identity.sign(wire.signed_post_payload(self.stable(agent), rs.RELAY_BASE_URL, nonce, kind, label, text,
                                                     tags, topic, reply_to, None, root, at), agent)
        r = self.client.post("/posts", headers=self.token(agent), json={
            "nonce": nonce, "kind": kind, "label": label, "text": text, "tags": tags, "topic": topic,
            "reply_to": reply_to, "quote_of": None, "root": root, "at": at, "signature": sig})
        assert r.status_code == 200, r.text
        return r.json()["post_id"]

    def activity(self, around: float, agents: list[str], *, only: list[str] | None = None) -> None:
        """A day's public record at `around`: each agent posts, and two agents of two other operators reply (where the
        relay has them), so a post is answered by two other operators. `only`: just these agents post."""
        unit = self.epoch / 1000
        each = 2 if len(agents) < 6 else 1        # a small relay needs enough events for a practice page
        ids: dict[tuple[str, int], str] = {}
        for i, a in enumerate(agents):
            if only is not None and a not in only:
                continue
            for j in range(each):
                self.clock.at(around + (i * each + j) * unit)
                ids[(a, j)] = self.post(a, f"{a} reports on the day at the gate, tally {i}.{j}")
        n = len(agents)
        for i, a in enumerate(agents):
            if (a, 0) not in ids:
                continue
            mine = self.agents[a]["operator"]
            answerers: list[str] = []
            for k in range(1, n):
                b = agents[(i + k) % n]
                ob = self.agents[b]["operator"]
                if ob != mine and ob not in {self.agents[x]["operator"] for x in answerers}:
                    answerers.append(b)
                if len(answerers) == 2:
                    break
            if only is not None:               # a day of one operator's agents only
                answerers = [b for b in agents if b != a and self.agents[b]["operator"] == mine][:2]
            for j in range(each):
                for m, b in enumerate(answerers or [agents[(i + 1) % n]]):
                    self.clock.at(around + 20 * unit + ((i * each + j) * 2 + m) * unit)
                    self.post(b, f"{b} answers {a} about the ledger {j}", reply_to=f"post:{ids[(a, j)]}")

    # ── reading ─────────────────────────────────────────────────────────────────────────────────
    def get(self, path: str, agent: str | None = None, **params):
        r = self.client.get(path, headers=self.token(agent) if agent else None, params=params or None)
        return r


class _AsAgent:
    """`tools.chronicle()` etc. run as one agent: the mcp_server door is pointed at the test client."""

    def __init__(self, world: World, agent: str):
        self.w, self.agent = world, agent

    def __enter__(self):
        from emporia import mcp_server
        self.m = mcp_server
        self.saved = (mcp_server.AGENT_ID, mcp_server._base, mcp_server._bearer_request)
        w, agent = self.w, self.agent

        def bearer(method, url, *, agent_id="", **kw):
            return w.client.request(method.upper(), url, headers=w.token(agent_id or agent), json=kw.get("json"),
                                    params=kw.get("params"))
        mcp_server.AGENT_ID = agent
        mcp_server._base = lambda: ""
        mcp_server._bearer_request = bearer
        return self

    def __exit__(self, *exc):
        self.m.AGENT_ID, self.m._base, self.m._bearer_request = self.saved

    def _tool(self, name: str):
        f = getattr(self.m, name)
        return getattr(f, "fn", f)

    def chronicle(self, **kw):
        return self._tool("chronicle")(**kw)

    def write(self, text, cites, **kw):
        # a scripted writer has no judgement to apply to a flag (a tense or opening heuristic that depends on the card
        # drawn): it confirms, as a model would after answering the three questions. Pass confirm=False to see flags.
        kw.setdefault("confirm", True)
        return self._tool("chronicle_write")(text, cites, **kw)

    def vote(self, pick, reason="", **kw):
        return self._tool("chronicle_vote")(pick, reason=reason, **kw)

    def reveal(self):
        return self._tool("chronicle_reveal")()


def scripted_passage(card: dict) -> str:
    import sim_chronicle as sim
    from emporia.modules import chronicle as C
    return sim.valid_passage(C.Card(**{k: card[k] for k in ("word", "limit", "shape", "voice")}))


def pick_cites(world: World, chapter: str) -> list[str]:
    ch = world.get(f"/chronicle/chapters/{chapter}").json()
    anchors = [a["ref"] for a in ch["anchors"]]
    others = [e["ref"] for e in world.get(f"/chronicle/chapters/{chapter}/record").json()["events"]
              if e["ref"] not in anchors]
    return [anchors[0], *(others[:1] or anchors[1:2])]


def play_chapter(world: World, names: list[str], slot: int, *, picker=None, skip_open=(), log=print) -> dict:
    """One chapter, slot `slot`, by every agent in `names`: write, open, pick, open. Returns what was done.
    `skip_open`: agents that seal but never open (they miss)."""
    call = world.start + slot * world.epoch
    E = world.epoch
    did: dict = {"slot": slot, "wrote": [], "picked": [], "opened": []}
    world.clock.at(call + 0.04 * E)
    views = {}
    for a in names:
        with world.tools(a) as t:
            views[a] = t.chronicle()
    cur = next(v for v in views.values() if v.get("chapter"))
    chapter = cur["chapter"]
    did["chapter"] = chapter
    card = world.get(f"/chronicle/chapters/{chapter}").json()["card"]
    cites = pick_cites(world, chapter)
    for a in names:
        v = views[a]
        if v.get("due") == "write":
            with world.tools(a) as t:
                r = t.write(scripted_passage(card), cites)
            did["wrote"].append((a, r))
    world.clock.at(call + 0.40 * E)
    for a in names:
        if a in skip_open:
            continue
        with world.tools(a) as t:
            r = t.reveal()
        if r.get("opened"):
            did["opened"].append((a, r))
    world.clock.at(call + 0.65 * E)
    for i, a in enumerate(names):
        with world.tools(a) as t:
            v = t.chronicle()
            if v.get("due") == "pick":
                cands = world.get(f"/chronicle/chapters/{chapter}").json().get("candidates") or []
                pick = (picker(a, i, cands) if picker else (cands[(i + slot) % len(cands)] if cands else "none"))
                r = t.vote(pick, reason=f"{a} liked it")
                did["picked"].append((a, pick, r))
    world.clock.at(call + 0.90 * E)
    for a in names:
        if a in skip_open:
            continue
        with world.tools(a) as t:
            r = t.reveal()
        if r.get("opened"):
            did["opened"].append((a, r))
    world.clock.at(call + E + 60)
    return did


def fixture_story(world: World, log=print) -> dict:
    """A relay's first days, for the Book page's fixtures: three agents of one operator play two practice pages; three
    more operators join and the call with four operators is Chapter 1 (a thin jury); in Chapter 2 every juror
    picks none (no canon); two more operators join and Chapter 3 has a full jury. Returns the public reads."""
    w = world
    p = ["fx_p1", "fx_p2", "fx_p3"]
    q = [f"fx_q{i}" for i in range(1, 6)]
    for n in p:
        w.register(n, "op_p", age_days=60)
    for i, n in enumerate(q):
        w.register(n, f"op_q{i}", age_days=10 + 30 * (i % 2))
    w.seed(p[0])
    w.clock.at(w.start - 3600)
    for n in p:
        with w.tools(n) as t:
            t.chronicle(enrol=True)
    enrolled = list(p)
    for slot in range(1, 6):
        if slot == 2:
            w.clock.at(w.start + 1.5 * w.epoch)
            for n in q[:3]:
                with w.tools(n) as t:
                    t.chronicle(enrol=True)
            enrolled += q[:3]
        if slot == 4:
            w.clock.at(w.start + 3.5 * w.epoch)
            for n in q[3:]:
                with w.tools(n) as t:
                    t.chronicle(enrol=True)
            enrolled += q[3:]
        w.activity(w.start + (slot - 1) * w.epoch + 0.8 * w.epoch, enrolled)
        none = (lambda a, i, c: "none") if slot == 4 else None
        did = play_chapter(w, enrolled, slot, picker=none)
        log(f"slot {slot}: chapter {did.get('chapter')} wrote {len(did['wrote'])} picked {len(did['picked'])}")
    return {"book": w.get("/chronicle/book").json(), "state": w.get("/chronicle").json()}


def run(chapters: int = 3, bls: bool = False, log=print) -> int:
    from fastapi.testclient import TestClient

    from emporia import relay_server as rs
    from emporia.relay_server import init_db
    import verify_chronicle as V
    init_db()
    client = TestClient(rs.app, client=("127.0.0.1", 51411))
    w = World(client)
    try:
        names = [f"e2e_{c}" for c in "abcdef"]
        for i, n in enumerate(names):
            w.register(n, f"op_{i}", age_days=100 if i % 2 else 10)
        w.seed(names[0])
        for n in names:
            with w.tools(n) as t:
                assert t.chronicle(enrol=True)["success"]
        ok = True
        for ch in range(1, chapters + 1):
            w.activity(w.start + (ch - 1) * DAY + 0.8 * DAY, names)
            did = play_chapter(w, names, ch)
            rep = V.verify(lambda p, _w=w: _w.get(p).json(), bls=bls)
            c = next(x for x in rep["chapters"] if x["id"] == did["chapter"])
            log(f"chapter {did['chapter']}: wrote {len(did['wrote'])}, picked {len(did['picked'])}, "
                f"canon {c['canon']}, reason {c['reason']}; verify {'agrees' if c['ok'] else 'DISAGREES: ' + str(c['problems'])}")
            ok &= c["ok"]
        log("all chapters agree with the independent count" if ok else "DISAGREEMENT")
        return 0 if ok else 1
    finally:
        w.close()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--chapters", type=int, default=3)
    ap.add_argument("--no-bls", action="store_true", help="the test beacon has no BLS signatures to check (default here)")
    ap.add_argument("--dump-book", metavar="PATH", help="instead: play a relay's first days (practice pages, then "
                    "chapters) and write the public reads the Book page shows to PATH as JSON")
    args = ap.parse_args()
    os.environ.setdefault("EMPORIA_LOG_LEVEL", "WARNING")
    if args.dump_book:
        from fastapi.testclient import TestClient

        from emporia import relay_server as rs
        rs.init_db()
        w = World(TestClient(rs.app, client=("127.0.0.1", 51412)))
        try:
            out = fixture_story(w)
        finally:
            w.close()
        Path(args.dump_book).write_text(json.dumps(out, indent=1, sort_keys=True))
        print(f"wrote {args.dump_book}")
        return 0
    return run(args.chapters, bls=False)


if __name__ == "__main__":
    raise SystemExit(main())
