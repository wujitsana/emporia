#!/usr/bin/env python3
"""Tesseract T1, end to end: scripted agents (no model) play a table on a throwaway relay.

    EMPORIA_NO_DOTENV=1 python scripts/e2e_tesseract.py [--players 5] [--seed 1] [--no-bls]

The relay is this repo's own, in process, on a throwaway database and key folder (the test suite's pinned
environment, `tests/conftest.py`), with the test beacon (every drand round a fixed fake signature) and a fake clock
that runs minutes in an instant. The agents use the three tools' logic (`emporia.tesseract_client.Table`, the code
`tesseract`, `tesseract_push` and `tesseract_open` run) over the relay's REST routes; nothing is called around them.
After the table the script runs `scripts/verify_tesseract.py` against the relay and prints whether it agrees. Nothing
leaves this machine.

`World` is also what `tests/test_tesseract_relay.py` plays with.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import os
import random
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
for p in (REPO, REPO / "src", REPO / "tests", REPO / "scripts"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import conftest  # noqa: E402,F401 — pins the suite's environment on a throwaway root, before emporia loads

from emporia import commit_reveal, operators  # noqa: E402
from emporia.modules import tesseract as T  # noqa: E402

WHY = "Playing the network toward my own target"


def fake_beacon_sig(rnd: int) -> str:
    """The test beacon: a stand-in 48-byte signature for a round (as `tests/conftest.fake_beacon_sig`)."""
    return hashlib.sha512(f"test-beacon-{rnd}".encode()).hexdigest()[:96]


class Clock:
    def __init__(self, t: float):
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, s: float) -> None:
        self.t += s


class World:
    """A throwaway relay with Tesseract on, scripted agents, the fake clock and the test beacon."""

    def __init__(self, client):
        from emporia import rand, rand_client, rand_game
        from emporia import relay_server as rs
        from emporia.routes import beacon as beacon_route
        self.client, self.rs = client, rs
        self.clock = Clock(datetime.now(UTC).timestamp())
        self._undo: list = []
        self._env = os.environ.get("EMPORIA_TESSERACT")
        os.environ["EMPORIA_TESSERACT"] = "1"

        def patch(obj, name, value):
            self._undo.append((obj, name, getattr(obj, name)))
            setattr(obj, name, value)
        patch(rand_game, "now", lambda: datetime.fromtimestamp(self.clock.t, UTC))
        patch(rand, "MIN_LEAD_ROUNDS", -10**6)          # as conftest's `rand_testmode`: any round may be named
        patch(rand, "MIN_TURN_LEAD_ROUNDS", -10**6)
        patch(beacon_route, "signature", fake_beacon_sig)
        patch(rs.AGENT_REGISTRY, "check_rate_limit", lambda agent_id, *a: True)
        self._rand_client = rand_client
        self.names: dict[str, str] = {}               # agent -> operator label
        self.sealed: dict[tuple[str, str], dict] = {}
        self.secrets: dict[tuple[str, str], dict] = {}

    def close(self) -> None:
        for obj, name, old in reversed(self._undo):
            setattr(obj, name, old)
        self._undo.clear()
        if self._env is None:
            os.environ.pop("EMPORIA_TESSERACT", None)
        else:
            os.environ["EMPORIA_TESSERACT"] = self._env
        self.rs.forget_who()

    # ── agents and tables ─────────────────────────────────────────────────────────────────────────
    def register(self, name: str, operator: str | None = None) -> str:
        from tests.test_emporia import _register_keyed
        _register_keyed(self.client, name)
        label = operators.new_label()
        if operator is not None:
            label = self.names.get(operator) or label      # a name already seen shares its label
        with self.rs.DB_LOCK, self.rs.get_db() as conn:
            conn.execute("UPDATE authorized_agents SET operator_label = ? WHERE agent_id = ?", (label, name))
            conn.commit()
        self.rs.forget_who()
        self.names[name] = label
        return name

    def hdr(self, agent: str) -> dict[str, str]:
        return {"x-emporia-agent-id": agent}

    def create(self, creator: str, **config: Any) -> str:
        from conftest import make_test_commit, signed_session_body
        from emporia import wire
        cfg = {"players": 4, **config}
        body = signed_session_body(creator, {"module_type": T.GAME_ID, "config": cfg})
        sid = wire.session_id_for(body["nonce"])
        r = self.client.post("/sessions", json={**body, "rand_commit": make_test_commit(sid, creator, 0)},
                             headers=self.hdr(creator))
        assert r.status_code == 200, r.text
        return r.json()["session_id"]

    def join(self, sid: str, agent: str, seat: int):
        from conftest import make_test_commit
        return self.client.post(f"/sessions/{sid}/join",
                                json={"agent_id": agent, "rand_commit": make_test_commit(sid, agent, seat)},
                                headers=self.hdr(agent))

    def table(self, names: list[str], **config: Any) -> str:
        """A started table: the first creates it, the rest join (any refusal is an assert)."""
        sid = self.create(names[0], players=len(names), **config)
        for seat, n in enumerate(names[1:], start=1):
            r = self.join(sid, n, seat)
            assert r.status_code == 200, r.text
        return sid

    def tools(self, agent: str):
        """The three tools' logic as `agent` runs it, over this relay."""
        from conftest import RAND_SECRETS
        from tests.test_emporia import _signed_action_body

        from emporia import tesseract_client as TC

        def get(path: str):
            r = self.client.get(path, headers=self.hdr(agent))
            return r.status_code, (r.json() if r.content else {})

        def act(sid: str, action_type: str, payload: dict, rationale: str, say: str = "") -> dict:
            body = _signed_action_body(self.client, sid, agent, action_type, payload, rationale, say=say)
            r = self.client.post(f"/sessions/{sid}/action", json=body)
            d = r.json()
            return {"success": True, **d} if r.status_code == 200 else {"success": False, "error": d.get("detail"),
                                                                        "detail": d}

        def private(sess: dict) -> dict:
            s, nonce = RAND_SECRETS[(sess["session_id"], agent)]
            rec = {"s": s, "nonce": nonce, "seat": sess["participants"].index(agent)}
            return self._rand_client.private_view(sess, agent, rec, relay_base=None, now=self.clock.t,
                                                  fetch=lambda rnd, base: fake_beacon_sig(rnd))

        def secret(sid: str):
            s, nonce = RAND_SECRETS[(sid, agent)]
            return contextlib.nullcontext({"s": s, "nonce": nonce})

        def sealed(sid: str):
            return contextlib.nullcontext(self.sealed.setdefault((sid, agent), {}))
        return TC.Table(agent, get=get, act=act, private=private, secret=secret, sealed=sealed, now=self.clock,
                        sleep=lambda s: self.clock.advance(s))

    # ── time ─────────────────────────────────────────────────────────────────────────────────────
    def sweep(self) -> None:
        import asyncio

        from emporia.routes.sessions import sweep_service_deadlines
        asyncio.run(sweep_service_deadlines())

    def state(self, sid: str) -> dict:
        return self.rs.load_session(sid)

    def flow(self, sid: str) -> dict:
        return T.flow(self.state(sid)["state"]["data"], self.clock.t)


# ── scripted agents ───────────────────────────────────────────────────────────────────────────────────────

def lite(view: dict, rng: random.Random) -> dict[str, int]:
    """Reads the table: the two best moves toward the target, four units each (the plan's `lite` bot)."""
    best = (view.get("you") or {}).get("best") or []
    push: dict[str, int] = {}
    for b in best:
        if len(push) >= 2:
            break
        if b["id"] not in push and b["toward_you"] > 0:
            push[b["id"]] = 4 if b["dir"] == "up" else -4
    return push


def idle(view: dict, rng: random.Random) -> dict[str, int]:
    return {}


def wild(view: dict, rng: random.Random) -> dict[str, int]:
    ids = [c["id"] for c in view["channels"]]
    return {c: rng.choice((-4, -2, 2, 4)) for c in rng.sample(ids, 2)}


STRATEGIES = {"lite": lite, "idle": idle, "random": wild}


def play(w: World, sid: str, names: list[str], strategies: dict[str, str] | None = None, *, seed: int = 1,
         sit_out: set[tuple[str, int]] = frozenset(), no_open: set[tuple[str, int]] = frozenset(),
         no_reveal: set[str] = frozenset(), say: bool = False, finish: bool = True) -> dict[str, Any]:
    """Play a started table: five rounds (a seat in `sit_out` seals nothing that round, one in `no_open` seals and
    does not open), then the secrets. Returns what the relay says at the end."""
    rng = random.Random(seed)
    strategies = strategies or {}
    limit = int(w.state(sid)["state"]["data"]["move_time_limit_s"])
    out: set[str] = set()
    for e in range(1, T.EPOCHS + 1):
        w.clock.advance(20)                 # the drand round the last round named is published
        sealed = []
        for n in names:
            if n in out:
                continue
            if (n, e) in sit_out:
                continue
            tools = w.tools(n)
            v = tools.view(sid)
            assert v.get("ready"), v
            push = STRATEGIES[strategies.get(n, "lite")](v, rng)
            r = tools.push(sid, push, say=f"{n} round {e}" if say else "")
            assert r["success"], r
            sealed.append(n)
        if any((n, e) in sit_out for n in names if n not in out):
            w.clock.advance(limit + 1)      # the seal window ends on the clock
        for n in sealed:
            if (n, e) in no_open:
                continue
            r = w.tools(n).open(sid)
            assert r["success"], r
        if any((n, e) in no_open for n in sealed):
            w.clock.advance(limit + 1)
            out |= {n for n in sealed if (n, e) in no_open}
        w.sweep()
    w.clock.advance(20)
    if finish:
        for n in names:
            if n in out or n in no_reveal:
                continue
            r = w.tools(n).open(sid)
            assert r["success"], r
        if no_reveal:
            w.clock.advance(int(w.state(sid)["state"]["data"]["reveal_window_s"]) + 1)
        w.sweep()
    return w.state(sid)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--players", type=int, default=5)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--no-bls", action="store_true", help="the test beacon is not drand's: skip the BLS check")
    args = ap.parse_args()
    from fastapi.testclient import TestClient

    import verify_tesseract as V
    from emporia import relay_server as rs
    os.environ.setdefault("EMPORIA_LOG_LEVEL", "WARNING")
    rs.init_db()
    w = World(TestClient(rs.app, client=("127.0.0.1", 51770)))
    try:
        names = [w.register(f"e2e_{args.seed}_{i}") for i in range(args.players)]
        mix = ["lite", "lite", "random", "idle", "lite", "random"]
        strategies = {n: mix[i % len(mix)] for i, n in enumerate(names)}
        sid = w.table(names)
        print(f"table {sid}: {', '.join(f'{n} ({strategies[n]})' for n in names)}")
        s = play(w, sid, names, strategies, seed=args.seed, say=True)
        data = s["state"]["data"]
        print(f"status {s['status']}")
        for d in (data.get("published") or {}).values():
            if isinstance(d, dict) and "epoch" in d:
                print(f"  round {d['epoch']}: mix {d['mix']}  pushes by {sorted(d['pushes'])}")
        res = w.client.get("/results", params={"agent_id": names[0]}).json()["results"][0]
        print(f"winner {res['winner']}; totals {res['points']}")
        tv = w.client.get("/tesseract/results", params={"limit": 20}).json()["results"]
        print("table index " + ", ".join(f"{r['agent_id']}={r['table_index']}" for r in tv if r["session_id"] == sid))
        rep = V.verify(lambda p: w.client.get(p).json(), bls=not args.no_bls, only=sid)
        for t in rep["tables"]:
            print(f"verify {t['session_id']}: {'ok' if t['ok'] else 'FAIL'} {t['problems'] or ''}")
        return 0 if rep["tables"] and all(t["ok"] for t in rep["tables"]) else 1
    finally:
        w.close()


if __name__ == "__main__":
    sys.exit(main())
