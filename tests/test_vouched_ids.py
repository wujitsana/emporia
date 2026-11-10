"""EMPORIA_VOUCHED_STABLE_IDS: the relay operator's resident agents count as established counterparties."""
from datetime import datetime

from emporia import quest_engine as E
from emporia import relay_config as rc

HOUR = 3600.0
GAME_AT = "2026-11-10T12:00:00+00:00"
T = datetime.fromisoformat(GAME_AT).timestamp()
OPS = {"newbie": "label:NEW", "res1": "label:HOST", "res2": "label:HOST", "other": "label:OTHER",
       "house": "label:HOST"}


def ctx(vouched=(), registered=None):
    return E.Ctx(operator=lambda a: OPS.get(a, a), house=frozenset({"house"}), parts=frozenset(),
                 registered=registered if registered is not None else {"res1": T - 60, "res2": T - 60, "other": T - 60},
                 contacts={}, new_agent_s=24 * HOUR, vouched_ids=frozenset(vouched))


def test_a_newcomer_playing_a_vouched_agent_at_minute_one_gets_credit():
    assert E.vs("newbie", "res1", ctx(vouched={"res1"}), GAME_AT) == "other"


def test_a_new_agent_that_is_not_vouched_still_gives_none():
    assert E.vs("newbie", "other", ctx(vouched={"res1"}), GAME_AT) == "new"
    assert E.vs("newbie", "res1", ctx(vouched={"other"}), GAME_AT) == "new"


def test_vouched_agents_earn_nothing_from_each_other_or_the_house():
    c = ctx(vouched={"res1", "res2"})
    assert E.vs("res1", "res2", c, GAME_AT) == "self"
    assert E.vs("res2", "res1", c, GAME_AT) == "self"
    assert E.vs("res1", "house", c, GAME_AT) == "house"


def test_a_vouched_agent_is_not_the_house_and_earns_from_others():
    c = ctx(vouched={"res1"})
    assert E.vs("res1", "newbie", ctx(vouched={"res1"}, registered={"newbie": T - 60}), GAME_AT) == "new", \
        "being vouched helps only as the counterparty"
    assert c.house == frozenset({"house"}) and "res1" not in c.house


def test_an_empty_setting_changes_nothing():
    assert E.vs("newbie", "res1", ctx(), GAME_AT) == "new"


def test_the_setting_is_listed_and_the_relay_reads_it(monkeypatch):
    from emporia import progression
    assert rc.BY_NAME["EMPORIA_VOUCHED_STABLE_IDS"].kind == "stable_ids"
    monkeypatch.delenv("EMPORIA_VOUCHED_STABLE_IDS", raising=False)
    assert progression.vouched_agents() == frozenset()
    from emporia import relay_server as rs
    monkeypatch.setenv("EMPORIA_VOUCHED_STABLE_IDS", f" {'AB' * 32} , ")
    assert rs.vouched_stable_ids() == frozenset({"ab" * 32})
