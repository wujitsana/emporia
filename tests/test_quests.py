"""P297: welcome quests — leading to the relay's main game, making Emporia better."""
import uuid

import pytest
from fastapi.testclient import TestClient

from emporia import quests as Q
from emporia import relay_server as rs
from tests.test_progression import _register, _session


def test_the_quests_come_from_the_records():
    me = "q_me"
    out = Q.progress(me, posts=[{"author_id": me, "topic_slug": "general", "visibility": "public",
                                 "created_at": "2026-11-05T01:00:00"}],
                     games=[{"module_type": "emporia:blotto:v1", "participants": [me, "house"],
                             "ended_at": "2026-11-05T02:00:00"},
                            {"module_type": "emporia:chess:v1", "participants": [me, "x"], "ended_at": "2026-11-05"}],
                     deals=[], reports=[{"reporter_id": me, "status": "invalid", "created_at": "2026-11-05"}])
    done = {q["id"] for q in out["quests"] if q["done"]}
    assert done == {"hello", "first-set-play"}, "chess is no set play; an invalid report is no quest"
    assert out["xp"] == 20 and "welcomed" not in out["badges"]
    assert out["next"] == Q.BY_ID["first-deal"].how


def test_all_four_welcome_quests_give_the_badge_and_reports_lead_on():
    me = "q_me"
    out = Q.progress(me, posts=[{"author_id": me, "topic_slug": "ideas", "visibility": "public", "created_at": "t1"}],
                     games=[{"module_type": "emporia:two-thirds:v1", "participants": [me], "ended_at": "t2"}],
                     deals=[{"buyer": me, "seller": "house", "status": "confirmed", "ended_at": "t3"}],
                     reports=[{"reporter_id": me, "status": "accepted", "created_at": "t4", "decided_at": "t5"}])
    assert all(q["done"] for q in out["quests"]) and "welcomed" in out["badges"]
    assert out["xp"] == sum(q.xp for q in Q.QUESTS) and out["next"] == ""


def test_private_or_deleted_posts_and_others_records_count_for_nothing():
    me = "q_me"
    out = Q.progress(me, posts=[{"author_id": me, "topic_slug": "ideas", "visibility": "private", "created_at": "t"},
                                {"author_id": me, "topic_slug": "ideas", "visibility": "public", "deleted": True,
                                 "created_at": "t"},
                                {"author_id": "someone", "topic_slug": "ideas", "visibility": "public",
                                 "created_at": "t"}],
                     games=[], deals=[{"buyer": me, "seller": "s", "status": "disputed", "ended_at": "t"}], reports=[])
    assert out["xp"] == 0


def test_the_route_is_off_unless_the_operator_runs_quests(monkeypatch):
    client = TestClient(rs.app)
    me = f"q_{uuid.uuid4().hex[:6]}"
    _register(me)
    monkeypatch.setenv("EMPORIA_GAMIFY", "sheets")
    assert client.get(f"/agents/{me}/quests").status_code == 404
    monkeypatch.setenv("EMPORIA_GAMIFY", "sheets,quests")
    _session("emporia:blotto:v1", [me, "q_house"], {"wins": {me: 2, "q_house": 0}, "history": [{}, {}], "rounds": 3})
    _session("emporia:service:v1", [me, "q_house"], {"buyer": me, "seller": "q_house", "status": "confirmed"})
    body = client.get(f"/agents/{me}/quests").json()
    done = {q["id"] for q in body["quests"] if q["done"]}
    assert {"first-set-play", "first-deal"} <= done and body["signature"] and body["formula"] == Q.FORMULA
    card = client.get("/relay/card").json()["relay_modules"]["gamify"]
    assert "quests" in card["parts"] and Q.FORMULA in card["formulas"]
    assert client.get("/agents/nobody_here_q/quests").status_code == 404
