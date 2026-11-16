"""The Spillway badges (quest_book FEATS `spillway-*`): earned only from a finished game's stored moves
(`spillway.badge_facts`), against another operator's agent, and never for a win on the clock or against the house."""
import random

from conftest import fake_beacon_sig

from emporia import quest_book as B
from emporia import quest_engine as E
from emporia.modules import spillway as S

ME = "me"
OPS = {"me": "label:ME", "mine2": "label:ME", "b1": "label:B", "c1": "label:C", "d1": "label:D", "house": "label:HOUSE"}
SPILLWAY_IDS = ("spillway-first", "spillway-closed-door", "spillway-last-drop", "spillway-three-ends")


def _sig(rnd):
    return bytes.fromhex(fake_beacon_sig(rnd))


def _gate_round(n, want, free):
    """A round whose signature shuts channel `want` for move n of scope "t"."""
    return next(r for r in range(1, 2000) if S.gate("t", n, _sig(r), free) == want)


def _threat_game(gate_hits: bool):
    """a moves first with 1-3 of the foot row (a win waits in channel 4), drops in 6, b is gated or not, a drops in 4."""
    free = list(range(1, 7))
    r1 = _gate_round(2, 4, free) if gate_hits else next(r for r in range(1, 2000) if S.gate("t", 2, _sig(r), free) != 4)
    data = {"seats": ["a", "b"], "drops": 7, "rand": {"scope": "t", "round": r1, "beacon_sig": _sig(r1).hex(),
                                                       "beacons": {str(r1): _sig(r1).hex()}},
            "opening": {"silt": [], "stones": {"a": [[1, 1], [2, 1], [3, 1]], "b": []}, "first": "a", "attempt": 0},
            "moves": [{"by": "a", "channel": 6, "round": r1}, {"by": "b", "channel": 5, "round": r1},
                      {"by": "a", "channel": 4, "round": r1}]}
    return data, free


def test_a_four_made_after_the_gate_shut_the_only_block_is_a_closed_door():
    data, _ = _threat_game(True)
    facts = S.badge_facts(data)
    assert facts["ending"] == "four" and facts["shut_block"] is True and facts["second_mover"] is False


def test_the_same_four_with_the_block_open_is_not():
    data, _ = _threat_game(False)
    assert S.badge_facts(data)["shut_block"] is False


def test_a_second_win_channel_or_a_win_of_their_own_cancels_the_closed_door():
    data, _ = _threat_game(True)
    two = {**data, "opening": {**data["opening"], "stones": {"a": [[1, 1], [2, 1], [3, 1], [5, 1], [5, 2], [5, 3]],
                                                               "b": []}}}
    assert S.badge_facts(two)["shut_block"] is False, "channel 4 is not the only way to win"
    own = {**data, "opening": {**data["opening"], "stones": {"a": [[1, 1], [2, 1], [3, 1]],
                                                              "b": [[5, 1], [5, 2], [5, 3]]}},
           "moves": [data["moves"][0], {"by": "b", "channel": 1, "round": data["moves"][0]["round"]},
                     data["moves"][2]]}
    assert S.badge_facts(own)["shut_block"] is False, "they could have won themselves"


def test_a_level_game_is_won_by_the_second_mover():
    d = {"seats": ["a", "b"], "drops": 2, "moves": [{"by": "a", "channel": 1}, {"by": "b", "channel": 3},
                                                    {"by": "a", "channel": 5}, {"by": "b", "channel": 6}],
         "opening": {"silt": [], "stones": {"a": [], "b": []}, "first": "a", "attempt": 0}}
    facts = S.badge_facts(d)
    assert facts == {"ending": "level", "second_mover": True, "shut_block": False}
    assert S.badge_facts({**d, "moves": d["moves"][:3]}) == {}, "no result yet"


# ── the quest book ───────────────────────────────────────────────────────────

def ctx():
    return E.Ctx(operator=lambda a: OPS.get(a, a), house=frozenset({"house"}), parts=frozenset())


def spillway_game(opp, ending="four", winner=ME, **facts):
    return {"session_id": f"s_{random.randrange(10**9)}", "module_type": B.SPILLWAY, "participants": [ME, opp],
            "ended_at": f"2026-11-1{random.randrange(10)}T10:00:00+00:00", "winner": winner, "termination": ending,
            "spillway": {"ending": ending, "second_mover": False, "shut_block": False, **facts}}


def earned(*games):
    c = ctx()
    out = E.evaluate(ME, E.events(ME, games=list(games), ctx=c), c)
    return {f["id"] for f in out["feats"] if f["done"]}, out


def test_the_book_names_the_four_feats_with_no_xp():
    ids = [f["id"] for f in B.FEATS if f["id"].startswith("spillway-")]
    assert tuple(ids) == SPILLWAY_IDS
    assert all("when" in f for f in B.FEATS if f["id"] in ids)
    assert not [f for f in E.events(ME, games=[], ctx=ctx())]


def test_a_win_against_another_operator_is_the_first_badge_and_only_that():
    got, out = earned(spillway_game("b1", "threes"))
    assert got == {"spillway-first"} and out["xp"] >= 0
    assert "spillway-first" in out["badges"]


def test_a_loss_a_draw_or_a_win_on_the_clock_earns_nothing():
    assert earned(spillway_game("b1", winner="b1"))[0] == set()
    assert earned(spillway_game("b1", ending="timeout"))[0] == set()
    assert earned(spillway_game("b1", ending="forfeit"))[0] == set()


def test_the_house_your_own_operator_and_a_game_nobody_moved_count_for_nothing():
    assert earned(spillway_game("house"))[0] == set()
    assert earned(spillway_game("mine2"))[0] == set()
    assert earned({**spillway_game("b1"), "played": False})[0] == set()


def test_a_new_agent_counts_for_nothing_like_every_other_cross_operator_step():
    c = E.Ctx(operator=lambda a: OPS.get(a, a), house=frozenset({"house"}), parts=frozenset(),
              registered={"b1": 1_000_000_000.0}, contacts={}, new_agent_s=3600.0)
    g = spillway_game("b1")
    g["ended_at"] = "2001-09-09T01:46:50+00:00"
    out = E.evaluate(ME, E.events(ME, games=[g], ctx=c), c)
    assert not [f for f in out["feats"] if f["done"] and f["id"].startswith("spillway-")]


def test_the_closed_door_and_the_last_drop_read_the_replayed_facts():
    got, _ = earned(spillway_game("b1", shut_block=True))
    assert got == {"spillway-first", "spillway-closed-door"}
    got, _ = earned(spillway_game("b1", ending="level", second_mover=True))
    assert got == {"spillway-first", "spillway-last-drop"}
    # the facts are the relay's: a game record that does not carry them earns neither
    bare = spillway_game("b1", ending="level")
    bare.pop("spillway")
    assert earned(bare)[0] == {"spillway-first"}
    # a closed-door claim on a lost game is nothing
    assert earned(spillway_game("b1", winner="b1", shut_block=True))[0] == set()


def test_every_ending_needs_four_threes_and_level_wins_and_three_of_one_is_not_enough():
    assert "spillway-three-ends" not in earned(*[spillway_game("b1", "four") for _ in range(5)])[0]
    got, _ = earned(spillway_game("b1", "four"), spillway_game("c1", "threes"))
    assert "spillway-three-ends" not in got
    got, _ = earned(spillway_game("b1", "four"), spillway_game("c1", "threes"), spillway_game("d1", "level", second_mover=True))
    assert "spillway-three-ends" in got


def test_farming_a_friend_gives_one_badge_per_rule_and_the_badges_pay_no_xp(monkeypatch):
    games = [spillway_game("b1", "four") for _ in range(30)]
    got, out = earned(*games)
    assert got == {"spillway-first"}
    monkeypatch.setattr(B, "FEATS", tuple(f for f in B.FEATS if not f["id"].startswith("spillway-")))
    assert earned(*games)[1]["xp"] == out["xp"], "a feat adds no XP; the play caps are play_xp's own"
