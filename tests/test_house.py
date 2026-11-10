"""P297 step 6: the house agent's rules runner."""
import random
from datetime import UTC, datetime, timedelta

from emporia import house as H
from tests.ids import house_env  # noqa: E402


def test_its_moves_are_legal():
    rng = random.Random(1)
    for _ in range(20):
        a = H.blotto_allocation(5, 100, rng)
        assert len(a) == 5 and sum(a) == 100 and all(x >= 0 for x in a)
        assert 0 <= H.pick({}, "emporia:two-thirds:v2", rng) <= 100
        assert 1 <= H.pick({"max_pick": 10}, "emporia:lowest-unique:v2", rng) <= 10
    import chess
    assert chess.Move.from_uci(H.chess_move(chess.STARTING_FEN, rng)) in chess.Board().legal_moves
    assert H.chess_move("rnb1kbnr/pppp1ppp/8/4p3/6Pq/5P2/PPPPP2P/RNBQKBNR w KQkq - 1 3", rng) is None   # mated


def _s(mt, status="active", parts=("house", "x"), current="house", **data):
    return {"session_id": "s1", "module_type": mt, "status": status, "participants": list(parts),
            "current_agent": current, "state": {"current_agent": current, "data": data},
            "created_at": (datetime.now(UTC) - timedelta(seconds=300)).isoformat(), "payment_rules": {}}


def test_it_knows_when_it_is_its_turn():
    assert H.my_turn(_s("emporia:blotto:v2", phase="commit"), "house") == "commit"
    assert H.my_turn(_s("emporia:blotto:v2", current="x", phase="commit"), "house") == ""
    assert H.my_turn(_s("emporia:two-thirds:v2", current="x", phase="reveal", pending=["house"]), "house") == "reveal"
    assert H.my_turn(_s("emporia:chess:v1"), "house") == "move"
    assert H.my_turn(_s("emporia:service:v1"), "house") == ""


def test_it_adopts_only_set_plays_left_waiting():
    now = datetime.now(UTC).timestamp()
    waiting = _s("emporia:blotto:v2", status="waiting", parts=("x",))
    assert H.adoptable(waiting, "house", now, 120)
    assert not H.adoptable(waiting, "house", now, 600), "not yet"
    assert not H.adoptable(_s("emporia:chess:v1", status="waiting", parts=("x",)), "house", now, 120)


class Tools:
    """The agent's tools, recorded."""
    def __init__(self, agents=(), invites=(), waiting=(), active=(), others=0):
        self.calls, self.agents, self.invites, self.others = [], list(agents), list(invites), others
        self.waiting, self.active = list(waiting), list(active)

    def get_inbox(self, unread_only=True):
        return {"events": [{"inbox_id": f"i{n}", "event_type": "session_invite", "payload": {"session_id": sid}}
                           for n, sid in enumerate(self.invites)]}

    def mark_inbox_read(self, ids):
        self.calls.append(("read", tuple(ids)))

    def get_session(self, sid):
        for s in self.active:
            if s["session_id"] == sid:
                return s
        return {"session_id": sid, "module_type": "emporia:blotto:v2", "payment_rules": {}}

    def join_session(self, sid):
        self.calls.append(("join", sid))
        return {"success": True}

    def list_sessions(self, module_type="", status="", limit=50):
        pool = self.waiting if status == "waiting" else self.active
        return {"sessions": [s for s in pool if s["module_type"] == module_type]}

    def commit_move(self, sid, move, rationale):
        self.calls.append(("commit", sid))
        return {"success": True}

    def reveal_move(self, sid, rationale):
        self.calls.append(("reveal", sid))
        return {"success": True}

    def submit_action(self, sid, kind, payload, rationale):
        self.calls.append(("move", payload["move"]))
        return {"success": True}

    def relay_activity(self, window_s):
        return {"active_agents": self.others}

    def list_agents(self, limit=200):
        return {"agents": [{"agent_id": a} for a in self.agents]}

    def send_dm(self, to, content):
        self.calls.append(("dm", to))
        return {"success": True}


def test_a_pass_joins_invites_plays_and_greets_only_newcomers(tmp_path):
    t = Tools(agents=["old"], invites=["inv1"], active=[_s("emporia:blotto:v2", phase="commit")])
    h = H.House("house", t, tmp_path / "state.json")
    assert h.step() == {"moves": 1, "joined": 1, "greeted": 0, "stepped_back": 0, "replied": 0, "asked": 0}, \
        "first run: those already here are not new"
    assert ("join", "inv1") in t.calls and ("commit", "s1") in t.calls and ("read", ("i0",)) in t.calls
    t.agents.append("new")
    t.invites, t.active = [], []
    h2 = H.House("house", t, tmp_path / "state.json")          # the state survives a restart
    assert h2.step()["greeted"] == 1 and ("dm", "new") in t.calls
    assert h2.step()["greeted"] == 0, "once"


def test_it_steps_back_when_the_relay_is_busy(tmp_path):
    """Busy means agents doing anything on the relay — posting, talking, trading,
    and liking — not only playing (the person, 11-05)."""
    waiting = _s("emporia:blotto:v2", status="waiting", parts=("newcomer",))
    busy = Tools(waiting=[waiting], others=4)
    done = H.House("house", busy, tmp_path / "busy.json").step()
    assert done["stepped_back"] == 1 and ("join", "s1") not in busy.calls, "four others active: it leaves them to it"
    quiet = Tools(waiting=[waiting], others=1)
    assert H.House("house", quiet, tmp_path / "quiet.json").step()["joined"] == 1
    invited = Tools(invites=["inv"], waiting=[waiting], others=10)
    H.House("house", invited, tmp_path / "inv.json").step()
    assert ("join", "inv") in invited.calls, "an invite still counts: someone asked for it"


def test_the_relay_counts_any_interaction_as_activity(monkeypatch):
    import uuid
    from fastapi.testclient import TestClient
    from emporia import relay_server as rs
    client = TestClient(rs.app)
    before = client.get("/relay/activity", params={"window_s": 3600}).json()["active_agents"]
    poster = f"act_{uuid.uuid4().hex[:6]}"
    from tests.test_emporia import _register_keyed
    _register_keyed(client, poster)
    before = client.get("/relay/activity", params={"window_s": 3600}).json()["active_agents"]
    with rs.DB_LOCK, rs.get_db() as conn:
        now = datetime.now(UTC).isoformat()
        conn.execute("INSERT INTO posts (post_id, author_id, author_stable_id, author_public_key_hex, origin_relay, "
                     "nonce, kind, text, at, signature, recorded_at) VALUES (?, ?, '', '', ?, ?, 'note', 'c', ?, 'sig', ?)",
                     (f"pst_{uuid.uuid4().hex[:12]}", poster, rs.RELAY_BASE_URL, uuid.uuid4().hex, now, now))
    from emporia.routes import standing
    standing._ACTIVITY_CACHE.clear()      # the count is kept 30 s (tests/test_relay_activity.py)
    assert client.get("/relay/activity", params={"window_s": 3600}).json()["active_agents"] == before + 1
    house_env(monkeypatch, poster)
    assert client.get("/relay/activity", params={"window_s": 3600}).json()["active_agents"] == before, "house: not counted"


def test_house_run_does_not_log_every_http_request(monkeypatch):
    """Found by the end-to-end run: `emporia house run` printed an httpx "HTTP Request"
    line for every call it made."""
    import logging

    from emporia import house
    from emporia import mcp_server as tools
    monkeypatch.setattr(tools, "AGENT_ID", "")
    levels = {n: logging.getLogger(n).level for n in ("httpx", "httpcore")}
    try:
        assert house.main(["--once"]) == 1                     # no agent: it stops, loggers already set
        assert logging.getLogger("httpx").getEffectiveLevel() == logging.WARNING
        assert logging.getLogger("httpcore").getEffectiveLevel() == logging.WARNING
    finally:
        for n, level in levels.items():
            logging.getLogger(n).setLevel(level)


def test_the_greeting_names_ideas_debriefs_and_expeditions_as_the_relay_calls_them(tmp_path):
    assert all(f"`{t}`" in H.GREETING for t in ("ideas", "debriefs", "expeditions"))
    said = []

    class Renamed(Tools):
        def my_quests(self):
            return {"success": True, "topics": {"ideas": "ideas", "debriefs": "war-stories", "expeditions": "quests"}}

        def send_dm(self, to, content):
            said.append(content)
            return {"success": True}

    t = Renamed(agents=["old"])
    h = H.House("house", t, tmp_path / "state.json")
    h.step()
    t.agents.append("new")
    h.step()
    assert "`war-stories`" in said[0] and "`quests`" in said[0]
    assert H.House("house", Tools(agents=[]), tmp_path / "s2.json").greeting() == H.GREETING, \
        "a relay that does not say: the default names"


# ── talk first ───────────────────────────────────────────────────────────────

class Talker(Tools):
    """Tools that also read the feed and the topic list, and record posts."""
    def __init__(self, notes=(), topics=(), latest=None, **kw):
        super().__init__(**kw)
        self.notes, self.topics, self.latest, self.posted, self.refuse = list(notes), list(topics), latest or {}, [], False

    def read_feed(self, topic="", kind="", limit=30, **kw):
        if kind == "note":
            return {"posts": self.notes}
        return {"posts": self.latest.get(topic, [])[:limit]}

    def list_agora_topics(self, visibility="", limit=50):
        return {"topics": [{"slug": s, "gate_type": "open"} for s in self.topics]}

    def post(self, text, kind="note", topic="", reply_to="", **kw):
        self.posted.append({"text": text, "kind": kind, "topic": topic, "reply_to": reply_to})
        return {"success": False, "error": "limited"} if self.refuse else {"success": True}


def _note(pid, author, at="2026-11-10T10:00:00+00:00", reply_to=None):
    return {"post_id": pid, "author_id": author, "at": at, "reply_to": reply_to}


def test_it_answers_a_newcomers_hello_once_and_never_the_backlog(tmp_path):
    t = Talker(notes=[_note("p_old", "veteran")])
    h = H.House("house", t, tmp_path / "state.json")
    assert h.step()["replied"] == 0 and not t.posted, "first run: those already here are not new"
    t.notes += [_note("p1", "newbie"), _note("p2", "newbie2", at="2026-11-10T11:00:00+00:00"),
                _note("p3", "newbie3", at="2026-11-10T12:00:00+00:00"), _note("p_reply", "x", reply_to="post:p1"),
                _note("p_own", "house")]
    assert h.step()["replied"] == 2, "at most two a pass"
    assert [(p["reply_to"], p["topic"], p["kind"]) for p in t.posted] == [("post:p1", "ideas", "note"),
                                                                           ("post:p2", "ideas", "note")]
    assert h.step()["replied"] == 1 and t.posted[-1]["reply_to"] == "post:p3"
    assert h.step()["replied"] == 0, "once per agent"
    assert "http" not in H.HELLO_REPLY


def test_a_refused_hello_reply_stops_the_pass_and_is_tried_later(tmp_path):
    t = Talker()
    h = H.House("house", t, tmp_path / "state.json")
    h.step()
    t.notes, t.refuse = [_note("p1", "newbie"), _note("p2", "newbie2")], True
    assert h.step()["replied"] == 0 and len(t.posted) == 1, "no retry loop in a pass"
    t.refuse = False
    assert h.step()["replied"] == 2


def test_it_asks_one_short_question_in_a_quiet_topic_at_most_every_six_hours(tmp_path):
    now = datetime.now(UTC).timestamp()
    recent = {"at": datetime.fromtimestamp(now - 3600, UTC).isoformat()}
    t = Talker(topics=["ideas", "help", "no-prompt-topic"], latest={"ideas": [recent]})
    h = H.House("house", t, tmp_path / "state.json")
    assert h.step(now=now)["asked"] == 1
    q = t.posted[-1]
    assert (q["kind"], q["topic"], q["text"]) == ("question", "help", H.SEED_PROMPTS["help"]) and "http" not in q["text"]
    assert h.step(now=now + 60)["asked"] == 0, "rate limited"
    assert h.step(now=now + H.SEED_EVERY_S + 1)["asked"] == 1, "again after six hours"


def test_nothing_is_asked_when_every_topic_has_talk_or_the_write_is_refused(tmp_path):
    now = datetime.now(UTC).timestamp()
    busy = {"at": datetime.fromtimestamp(now - 60, UTC).isoformat()}
    t = Talker(topics=["ideas", "help"], latest={"ideas": [busy], "help": [busy]})
    assert H.House("house", t, tmp_path / "a.json").step(now=now)["asked"] == 0 and not t.posted
    quiet = Talker(topics=["help"])
    quiet.refuse = True
    h = H.House("house", quiet, tmp_path / "b.json")
    assert h.step(now=now)["asked"] == 0 and len(quiet.posted) == 1, "refused: it stops, no retry"
    assert h.step(now=now + 60)["asked"] == 0 and len(quiet.posted) == 1, "and looks again only after a while"
    quiet.refuse = False
    assert h.step(now=now + H.SEED_RECHECK_S + 1)["asked"] == 1
