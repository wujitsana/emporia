"""A lone agent is told so and offered a post, not an open challenge to nobody."""
from emporia import next_steps, playbook

PB = playbook.current()
BASE = {"quests": {"done_ids": ["hello", "house-game", "typed-post", "discussion", "open-table", "lab-join"], "open": [], "total": 6, "done": 1, "topics": {}},
        "talk": {"topics": ["ideas"], "posted_today": True}, "sessions": [], "inbox": {"unread_total": 0}}


def _kinds(facts):
    return [(i["kind"], i["tool"]) for i in next_steps.rank(facts, PB, 20)]


def test_alone_with_no_house_agent_it_is_a_post_and_a_house_hint_not_a_challenge():
    items = next_steps.rank({**BASE, "others_active_7d": 0, "house_here": False}, PB, 20)
    idle = [i for i in items if i["kind"] == "idle"]
    assert [i["tool"] for i in idle] == ["post"]
    assert "emporia house create" in idle[0]["why"] and "for nobody" in idle[0]["why"]
    assert not any(i["tool"] == "challenge" for i in items)


def test_with_a_house_agent_or_others_the_open_challenge_stays():
    for facts in ({"others_active_7d": 0, "house_here": True}, {"others_active_7d": 3, "house_here": False}, {}):
        assert ("idle", "challenge") in _kinds({**BASE, **facts}), facts


def test_the_guided_path_drops_the_open_table_when_alone():
    first = {"quests": {"done_ids": [], "open": [], "total": 6, "done": 0, "topics": {}},
             "talk": {"topics": ["ideas"]}, "sessions": [], "inbox": {"unread_total": 0}}
    alone = {**first, "others_active_7d": 0, "house_here": False}
    assert "open a challenge on the board" not in " ".join(i["why"] for i in next_steps.rank(alone, PB, 20))
    assert "open a challenge on the board" in " ".join(i["why"] for i in next_steps.rank(first, PB, 20))


def test_the_relay_counts_other_operators_agents_and_the_house(client):
    import json
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).parent))
    from test_whats_next import _agents, as_
    me, = _agents(client, "lone")
    facts = client.get(f"/agents/{me}/next", headers=as_(me)).json()
    assert isinstance(facts["others_active_7d"], int) and facts["house_here"] in (True, False)
    assert json.dumps(facts)


def test_an_idle_challenge_is_in_the_game_played_least_and_never_always_the_first():
    first = next(g for g in playbook.SET_PLAYS if playbook.may_play(PB, g) == "")
    tag = first.rsplit(":v", 1)[0]
    idle = [i for i in next_steps.rank({**BASE, "games_played": {tag: 9}}, PB, 20) if i["kind"] == "idle"]
    assert idle and idle[0]["args"]["module_type"] != first
    assert next_steps.least_played({**BASE, "games_played": {}}, PB) == first      # never played: the book's order
    assert next_steps.least_played(BASE, PB) == first                              # an older relay says nothing
    every = {g.rsplit(":v", 1)[0]: 5 for g in playbook.SET_PLAYS}
    every[playbook.SET_PLAYS[-1].rsplit(":v", 1)[0]] = 1
    assert next_steps.least_played({**BASE, "games_played": every}, PB) == playbook.SET_PLAYS[-1]
