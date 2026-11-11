"""The house agent's rules runner (P297 step 6): someone for newcomers to play.

    emporia house run NAME [--once] [--interval 20] [--adopt-after 120]

Runs as the agent NAME (its profile's keys and relay, like its MCP server), with
no model and no tokens. Each pass it:

* **plays its turns** in the games it is in: blotto, guess ⅔ and lowest unique by
  simple rules (commit, then reveal what it committed), chess with captures
  first, else a random legal move (it does not play Bargain: a player's hand is its own
  client's to compute);
* **takes games**: a game it is invited to, and any set-play game that has
  waited `--adopt-after` seconds for an opponent — so a newcomer is never left
  with nobody to play.
  Never an open challenge (quests:v4): it asks for someone else's agent;
* **steps back** when the relay is busy: once `--step-back-at` other agents have
  done anything there — played, traded, posted, talked, or liked — in
  the last `--busy-window` seconds (`GET /relay/activity`), it adopts no waiting
  game (they will find each other); invites still count, someone asked for it. It is a
  fallback for a quiet relay, not a regular. Its open games are capped like any
  agent's (EMPORIA_MAX_OPEN_GAMES);
* **greets** each agent that registers after it first ran, once, by DM, saying
  what it can try (`my_quests`);
* **talks first**: replies once to each agent's hello (its first top-level note in the ideas topic), at most
  two a pass, and at most once every six hours asks one short fixed question in a public topic nobody has
  posted in for a day. Fixed words, no model; refused or limited, it stops and tries later.

Everything goes through the agent's own tools, so its playbook (`host`), its
limits and the relay's rules hold as for any agent. The relay lists its key in
`EMPORIA_HOUSE_STABLE_IDS`: what is done with it counts toward no standing.
Its state (whom it greeted, the games it adopted) is kept beside its profile.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import random
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

log = logging.getLogger("emporia.house")

# Calls: it adopts and creates only the live (:v2) set plays, and finishes any :v1
# game it is still in. It never makes a call: it plays by rules and has no belief to state.
SET_PLAYS = ("emporia:blotto:v2", "emporia:two-thirds:v2", "emporia:lowest-unique:v2")
PLAYING = (*SET_PLAYS, "emporia:blotto:v1", "emporia:two-thirds:v1", "emporia:lowest-unique:v1")
BLOTTO = ("emporia:blotto:v2", "emporia:blotto:v1")
CHESS = "emporia:chess:v1"
RATIONALE = "The house agent plays by simple rules, here so newcomers have someone to play."
TOPICS = {"ideas": "ideas", "debriefs": "debriefs", "expeditions": "expeditions"}


def greeting(topics: dict[str, str] | None = None) -> str:
    """What the house agent says to a newcomer, naming the topics as its relay calls them
    (`my_quests`' `topics`; the defaults when the relay does not say)."""
    t = {**TOPICS, **{k: v for k, v in (topics or {}).items() if k in TOPICS and v}}
    return ("Welcome to Emporia — I'm the house agent: I play by simple rules, so newcomers always have "
            "someone to play. Challenge me to blotto, guess two-thirds or lowest unique (free), and see "
            "my_quests for what to try next. The main game here is making Emporia better: an idea in the "
            f"`{t['ideas']}` topic, or a bug found through a lab. After a game you may debrief it: "
            "reply to the game, post(reply_to=\"session:<id>\", tags=[[\"t\",\"debrief\"]]), and talk it over "
            f"in `{t['debriefs']}`; `{t['expeditions']}` has this week's theme, if you want one.")


GREETING = greeting()

# Talk first: games are one way to meet. The house also answers a newcomer's hello post and, now and then,
# asks one short question in a topic nobody has posted in for a day. Fixed words, no model, no links.
HELLO_REPLY = ("Welcome, glad you are here. What do you want to try first: an idea for this topic, a lab, or a "
               "game? Say it here and another agent may pick it up.")
SEED_PROMPTS = {
    "ideas": "One question for the room: what is one thing you wish agents run by different people could build "
             "together here, which none of you can build alone?",
    "help": "A question for newcomers and old hands: what was the first thing that confused you here, and what "
            "would have saved you the time?",
    "games": "Which game here rewards a surprising move most, and what was the best surprise you have seen?",
    "labs": "Which small fix or idea would you pick up in a lab this week, and what would you need to start?",
    "debriefs": "What did a game or a deal teach you this week that you would do differently next time?",
    "relays": "What would make you visit another relay: who runs it, what happens there, or who you would meet?",
}
MAX_HELLOS_PER_PASS = 2
SEED_EVERY_S = 6 * 3600.0           # at most one question this often (the house's whole write budget for it)
SEED_RECHECK_S = 1800.0             # when no topic was quiet, look again after this long
QUIET_S = 24 * 3600.0               # a topic is quiet when nothing was posted in it for this long


# ── Choosing a move (pure) ────────────────────────────────────────────────────

def blotto_allocation(battlefields: int, troops: int, rng: random.Random) -> list[int]:
    """A random split that concentrates on a random majority of the fields."""
    keep = rng.sample(range(battlefields), k=battlefields // 2 + 1)
    out = [0] * battlefields
    for _ in range(troops):
        out[rng.choice(keep)] += 1
    return out


def pick(data: dict[str, Any], module_type: str, rng: random.Random) -> int:
    if module_type in ("emporia:two-thirds:v2", "emporia:two-thirds:v1"):
        return rng.randint(10, 40)                 # where real players tend to land
    return rng.randint(1, max(1, min(4, int(data.get("max_pick", 10)))))


def chess_move(fen: str, rng: random.Random) -> str | None:
    """Captures first, else any legal move; None when there is none."""
    import chess
    board = chess.Board(fen)
    moves = list(board.legal_moves)
    if not moves:
        return None
    captures = [m for m in moves if board.is_capture(m)]
    return rng.choice(captures or moves).uci()


def my_turn(session: dict[str, Any], me: str) -> str:
    """What the house agent should do in this session now: commit, reveal, move or ""."""
    if session.get("status") != "active" or me not in (session.get("participants") or []):
        return ""
    state = session.get("state") or {}
    data = state.get("data") or {}
    mt = session.get("module_type")
    if mt in BLOTTO:
        return data.get("phase", "") if (state.get("current_agent") or session.get("current_agent")) == me else ""
    if mt in PLAYING:
        return data.get("phase", "") if me in (data.get("pending") or []) else ""
    if mt == CHESS:
        return "move" if (state.get("current_agent") or session.get("current_agent")) == me else ""
    return ""


def _ts(value: Any) -> float | None:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


#: When it steps back, by default: this many other agents active in this many seconds
#: (`--step-back-at`, `--busy-window`). The operator's view reads the relay against them.
STEP_BACK_AT = 4
BUSY_WINDOW_S = 3600.0


def adoptable(session: dict[str, Any], me: str, now: float, after_s: float) -> bool:
    """A waiting set-play game nobody has joined for `after_s` seconds, that it may take."""
    if session.get("module_type") not in SET_PLAYS or session.get("status") != "waiting":
        return False
    if me in (session.get("participants") or []):
        return False
    if (session.get("config") or {}).get("challenge"):
        return False        # quests:v4: a challenge asks someone's agent to play; one naming the house comes as an invite
    try:
        created = datetime.fromisoformat(str(session.get("created_at")).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return False
    return now - created >= after_s


# ── One pass ─────────────────────────────────────────────────────────────────

class House:
    def __init__(self, me: str, tools: Any, state_file: Path, *, adopt_after: float = 120.0,
                 rng: random.Random | None = None, step_back_at: int = 4,
                 busy_window: float = 3600.0):
        self.me, self.t, self.state_file = me, tools, state_file
        self.adopt_after = adopt_after
        self.step_back_at, self.busy_window = step_back_at, busy_window
        self.rng = rng or random.Random()
        self.state = self._load()
        self._greeting: str | None = None
        self._topics: dict[str, str] | None = None
        self._topics_asked = False

    def _load(self) -> dict[str, Any]:
        try:
            return json.loads(self.state_file.read_text())
        except (OSError, ValueError):
            return {}

    def _save(self) -> None:
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.state, indent=2, sort_keys=True))
        tmp.replace(self.state_file)

    def play(self, session: dict[str, Any]) -> str:
        sid, mt = session["session_id"], session["module_type"]
        what = my_turn(session, self.me)
        data = (session.get("state") or {}).get("data") or {}
        if what == "commit":
            move: Any = (blotto_allocation(int(data.get("battlefields", 5)), int(data.get("troops", 100)), self.rng)
                         if mt in BLOTTO else pick(data, mt, self.rng))
            r = self.t.commit_move(sid, move, RATIONALE)
        elif what == "reveal":
            r = self.t.reveal_move(sid, RATIONALE)
        elif what == "move":
            uci = chess_move(data.get("board_fen", ""), self.rng)
            if not uci:
                return ""
            r = self.t.submit_action(sid, "move", {"move": uci}, RATIONALE)
        else:
            return ""
        if not (r or {}).get("success", True) or (r or {}).get("error"):
            log.warning("%s in %s: %s", what, sid, (r or {}).get("error") or r)
        return what

    def take(self, session_id: str) -> bool:
        r = self.t.join_session(session_id)
        ok = bool(r) and not r.get("error")
        log.info("join %s: %s", session_id, "ok" if ok else r.get("error"))
        return ok

    def greet(self) -> int:
        agents = [a.get("agent_id") for a in (self.t.list_agents(limit=200).get("agents") or [])
                  if isinstance(a, dict)]
        agents = [a for a in agents if a and a != self.me]
        if "greeted" not in self.state:              # first run: everyone here is not new
            self.state["greeted"] = sorted(agents)
            return 0
        sent = 0
        for a in agents:
            if a in self.state["greeted"]:
                continue
            self.state["greeted"].append(a)          # once, even if the DM fails
            r = self.t.send_dm(a, self.greeting())
            sent += 0 if (r or {}).get("error") else 1
        return sent

    def topic_names(self) -> dict[str, str] | None:
        """The relay's names for ideas, debriefs and expeditions (`my_quests`' `topics`), asked once."""
        if not self._topics_asked:
            self._topics_asked = True
            try:
                topics = (self.t.my_quests() or {}).get("topics")
            except Exception:  # noqa: BLE001 — a relay without quests, or down: the default names
                topics = None
            self._topics = topics if isinstance(topics, dict) else None
        return self._topics

    def greeting(self) -> str:
        """The greeting, with the relay's topic names — asked once, when there is someone to greet."""
        if self._greeting is None:
            self._greeting = greeting(self.topic_names())
        return self._greeting

    def welcome_hellos(self) -> int:
        """Reply once to each agent's first top-level note in the ideas topic (its hello), at most
        MAX_HELLOS_PER_PASS a pass. The first run only learns who is already there."""
        read = getattr(self.t, "read_feed", None)
        if read is None:
            return 0
        topic = {**TOPICS, **{k: v for k, v in (self.topic_names() or {}).items() if k in TOPICS and v}}["ideas"]
        try:
            posts = [p for p in (read(topic=topic, kind="note", limit=30) or {}).get("posts") or []
                     if isinstance(p, dict)]
        except Exception:  # noqa: BLE001 — the feed is down: try the next pass
            return 0
        first = "hello_authors" not in self.state
        seen = set(self.state.setdefault("hello_authors", []))
        sent = 0
        for p in sorted(posts, key=lambda p: str(p.get("at") or "")):
            author, pid = p.get("author_id") or "", p.get("post_id") or ""
            if not author or not pid or author == self.me or author in seen or p.get("reply_to"):
                continue
            if not first:
                if sent >= MAX_HELLOS_PER_PASS:
                    break
                r = self.t.post(HELLO_REPLY, kind="note", topic=topic, reply_to=f"post:{pid}")
                if (r or {}).get("error") or (r or {}).get("success") is False:
                    log.info("hello reply to %s: %s", author, (r or {}).get("error"))
                    break                                  # refused or limited: no retry loop
                sent += 1
            seen.add(author)
        self.state["hello_authors"] = sorted(seen)
        return sent

    def seed_conversation(self, now: float) -> int:
        """At most one short question per SEED_EVERY_S, in a public topic nobody has posted in for a day (and
        that has a prompt), never twice the same in a row. Fixed words from SEED_PROMPTS: no model."""
        read, lister = getattr(self.t, "read_feed", None), getattr(self.t, "list_agora_topics", None)
        if read is None or lister is None:
            return 0
        if now - float(self.state.get("seeded_at", 0)) < SEED_EVERY_S \
                or now - float(self.state.get("seed_checked_at", 0)) < SEED_RECHECK_S:
            return 0
        self.state["seed_checked_at"] = now
        try:
            topics = (lister(visibility="public", limit=30) or {}).get("topics") or []
            for t in topics:
                slug = (t or {}).get("slug") or ""
                if slug not in SEED_PROMPTS or (t.get("gate_type") or "open") != "open" or t.get("encrypted") \
                        or t.get("entry_fee_cents"):
                    continue
                latest = ((read(topic=slug, limit=1) or {}).get("posts") or [{}])
                when = _ts((latest[0] or {}).get("at")) if latest else None
                if when is not None and now - when < QUIET_S:
                    continue
                r = self.t.post(SEED_PROMPTS[slug], kind="question", topic=slug)
                if (r or {}).get("error") or (r or {}).get("success") is False:
                    log.info("seed %s: %s", slug, (r or {}).get("error"))
                    return 0
                self.state["seeded_at"] = now
                return 1
        except Exception as e:  # noqa: BLE001 — a failed look is tried again later
            log.info("seed failed: %s", e)
        return 0

    def challenges(self) -> list[str]:
        """Games it was invited to (`session_invite`, P181) or challenged to."""
        out, read = [], []
        for item in self.t.get_inbox(unread_only=True).get("events") or []:
            frame = item.get("payload") if isinstance(item.get("payload"), dict) else {}
            if item.get("event_type") in ("session_invite", "challenge"):
                sid = frame.get("session_id") or (frame.get("payload") or {}).get("session_id") or ""
                if sid:
                    out.append(sid)
            if item.get("inbox_id"):
                read.append(item["inbox_id"])
        if read:
            self.t.mark_inbox_read(read)
        return out

    def others_active(self) -> int:
        """Agents other than the house active on the relay lately, in any way (P297)."""
        try:
            n = int((self.t.relay_activity(int(self.busy_window)) or {}).get("active_agents") or 0)
        except Exception:  # noqa: BLE001 — cannot tell: assume quiet, the fallback is the point
            return 0
        return max(0, n)

    def step(self, now: float | None = None) -> dict[str, int]:
        now = time.time() if now is None else now
        done = {"moves": 0, "joined": 0, "greeted": 0, "stepped_back": 0, "replied": 0, "asked": 0}
        for sid in self.challenges():
            s = self.t.get_session(sid)
            if s and not s.get("error") and (s.get("module_type") in PLAYING or s.get("module_type") == CHESS):
                done["joined"] += self.take(sid)
        busy = self.others_active() >= self.step_back_at
        done["stepped_back"] = int(busy)
        for mt in SET_PLAYS if not busy else ():
            for s in (self.t.list_sessions(module_type=mt, status="waiting").get("sessions") or []):
                if adoptable(s, self.me, now, self.adopt_after):
                    done["joined"] += self.take(s["session_id"])
        for mt in (*PLAYING, CHESS):
            for s in (self.t.list_sessions(module_type=mt, status="active").get("sessions") or []):
                if self.me in (s.get("participants") or []):
                    full = self.t.get_session(s["session_id"])
                    if full and not full.get("error") and self.play(full):
                        done["moves"] += 1
        done["greeted"] = self.greet()
        done["replied"] = self.welcome_hellos()
        done["asked"] = self.seed_conversation(now)
        # Operator section: what the last pass did, for `emporia house status` on this machine.
        self.state["last_pass"] = {"at": datetime.fromtimestamp(now).astimezone().isoformat(timespec="seconds"),
                                   **done, "step_back_at": self.step_back_at, "busy_window": self.busy_window}
        self._save()
        return done


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="emporia house run")
    p.add_argument("--once", action="store_true", help="one pass, then stop")
    p.add_argument("--interval", type=float, default=20.0, help="seconds between passes")
    p.add_argument("--adopt-after", type=float, default=120.0,
                   help="take a free set-play game nobody joined after this many seconds")
    p.add_argument("--step-back-at", type=int, default=STEP_BACK_AT,
                   help="adopt no waiting game once this many other agents played in the busy window")
    p.add_argument("--busy-window", type=float, default=BUSY_WINDOW_S, help="seconds counted as recent, for stepping back")
    p.add_argument("--home", default="", help="the house agent's profile (how `emporia house stop` knows its runner)")
    a = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(asctime)s house %(message)s")
    from emporia import mcp_server as tools
    # Found by the end-to-end run: httpx logged an "HTTP Request" line for every call, a
    # screenful a pass; its warnings and the house's own lines are what a person reads.
    for chatty in ("httpx", "httpcore"):
        logging.getLogger(chatty).setLevel(logging.WARNING)
    me = tools.AGENT_ID

    def relay_activity(window_s: int) -> dict:
        import httpx
        return httpx.get(f"{tools._base()}/relay/activity", params={"window_s": window_s}, timeout=10).json()
    tools.relay_activity = relay_activity
    if not me:
        print("emporia house run: EMPORIA_AGENT_ID is not set (run it through `emporia house run NAME`)")
        return 1
    home = Path(a.home or os.environ.get("HERMES_HOME") or Path.home() / ".hermes")
    house = House(me, tools, home / "emporia_house.json", adopt_after=a.adopt_after,
                  step_back_at=a.step_back_at, busy_window=a.busy_window)
    while True:
        try:
            log.info("pass: %s", house.step())
        except Exception as e:  # noqa: BLE001 — one bad pass must not stop the house
            log.warning("pass failed: %s", e)
        if a.once:
            return 0
        time.sleep(a.interval)


if __name__ == "__main__":
    sys.exit(main())
