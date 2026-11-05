"""P297: the set plays enforce their rules end to end before season-0 counts them —
commits, reveals, order, outsiders, clocks, ties and games nobody played."""
from datetime import UTC, datetime, timedelta

import pytest

from emporia import commit_reveal, progression
from emporia.module_sdk import SessionAction, get_interaction_module

PICKS = ["emporia:two-thirds:v1", "emporia:lowest-unique:v1"]
BLOTTO = "emporia:blotto:v1"


def _act(mod, state, who, kind, payload):
    ok, why = mod.validate_action(state, SessionAction(who, kind, payload))
    if not ok:
        return state, why
    return mod.apply_action(state, SessionAction(who, kind, payload)).new_state, ""


def _commit(mod, state, who, move):
    nonce = commit_reveal.new_nonce()
    state, why = _act(mod, state, who, "commit", {"commitment": commit_reveal.commitment(move, nonce)})
    return state, nonce, why


def _players(mod):
    return ["a", "b", "c", "d"][:mod.DEFAULTS["players"]]


def _expire(state, key):
    state.data[key] = (datetime.now(UTC) - timedelta(seconds=state.data["move_time_limit_s"] + 1)).isoformat()


@pytest.mark.parametrize("game", PICKS)
def test_a_pick_is_committed_once_and_revealed_only_after_everyone_committed(game):
    mod = get_interaction_module(game)
    players = _players(mod)
    st = mod.initial_state(players, {})
    st, na, _ = _commit(mod, st, "a", 3)
    assert _commit(mod, st, "a", 4)[2], "a second commit is refused"
    assert _act(mod, st, "a", "reveal", {"move": 3, "nonce": na})[1], "no reveal before everyone committed"
    assert _commit(mod, st, "outsider", 1)[2], "an outsider cannot play"
    st, nb, _ = _commit(mod, st, "b", 5)
    st, nc, _ = _commit(mod, st, "c", 7)
    extra = {}
    for p in players[3:]:
        st, extra[p], _ = _commit(mod, st, p, 8)
    assert _act(mod, st, "a", "reveal", {"move": 4, "nonce": na})[1], "a reveal must match the commitment"
    st, _ = _act(mod, st, "a", "reveal", {"move": 3, "nonce": na})
    assert _act(mod, st, "a", "reveal", {"move": 3, "nonce": na})[1], "revealed once"
    st, _ = _act(mod, st, "b", "reveal", {"move": 5, "nonce": nb})
    st, _ = _act(mod, st, "c", "reveal", {"move": 7, "nonce": nc})
    for p, n in extra.items():
        st, _ = _act(mod, st, p, "reveal", {"move": 8, "nonce": n})
    over, result = mod.is_terminal(st)
    assert over and (result.get("winner") or result.get("outcome_type") == "draw")


@pytest.mark.parametrize("game", PICKS)
def test_a_pick_game_nobody_finished_committing_is_a_draw_and_no_result(game):
    mod = get_interaction_module(game)
    st = mod.initial_state(_players(mod), {})
    st, _, _ = _commit(mod, st, "a", 3)
    _expire(st, "phase_started_at")
    over, result = mod.is_terminal(st)
    assert over and result["winner"] is None and result["termination"] == "timeout"
    assert progression._scores({"participants": _players(mod), **result}) is None, "Craft: no result"
    assert _commit(mod, st, "b", 1)[2], "too late to commit"


@pytest.mark.parametrize("game", PICKS)
def test_one_who_commits_and_never_reveals_is_out(game):
    mod = get_interaction_module(game)
    players = _players(mod)
    st = mod.initial_state(players, {})
    nonces = {}
    for i, p in enumerate(players):
        st, nonces[p], _ = _commit(mod, st, p, i + 2)
    for i, p in enumerate(players[:-1]):
        st, _ = _act(mod, st, p, "reveal", {"move": i + 2, "nonce": nonces[p]})
    _expire(st, "phase_started_at")
    over, result = mod.is_terminal(st)
    assert over and result["out"] == [players[-1]] and result.get("winner") != players[-1]


def test_lowest_unique_with_no_unique_pick_is_a_draw():
    from emporia.modules.lowest_unique import _verdict
    assert _verdict({"a": 1, "b": 1, "c": 2, "d": 2})["outcome_type"] == "draw"


def _blotto():
    mod = get_interaction_module(BLOTTO)
    return mod, mod.initial_state(["a", "b"], {"rounds": 1})


def test_blotto_takes_turns_and_an_equal_round_is_a_draw():
    mod, st = _blotto()
    assert _commit(mod, st, "b", [20] * 5)[2], "not b's turn"
    st, na, _ = _commit(mod, st, "a", [20] * 5)
    st, nb, _ = _commit(mod, st, "b", [20] * 5)
    st, _ = _act(mod, st, "a", "reveal", {"move": [20] * 5, "nonce": na})
    st, _ = _act(mod, st, "b", "reveal", {"move": [20] * 5, "nonce": nb})
    over, result = mod.is_terminal(st)
    assert over and result["winner"] is None and result["outcome_type"] == "draw"


def test_a_blotto_nobody_moved_in_is_a_draw_but_stopping_midway_loses():
    mod, st = _blotto()
    _expire(st, "turn_started_at")
    over, result = mod.is_terminal(st)
    assert over and result["winner"] is None and result["termination"] == "timeout", "nobody played: a draw"
    assert progression._scores({"participants": ["a", "b"], **result}) is None
    mod, st = _blotto()
    st, _, _ = _commit(mod, st, "a", [20] * 5)
    _expire(st, "turn_started_at")
    over, result = mod.is_terminal(st)
    assert over and result["winner"] == "a", "b stopped after a moved: b loses on time"


def test_an_illegal_blotto_allocation_cannot_be_revealed():
    mod, st = _blotto()
    st, na, _ = _commit(mod, st, "a", [50, 50, 50, 0, 0])
    st, _, _ = _commit(mod, st, "b", [20] * 5)
    assert "not legal" in _act(mod, st, "a", "reveal", {"move": [50, 50, 50, 0, 0], "nonce": na})[1]


@pytest.mark.parametrize("mt,good,bad", [("emporia:two-thirds:v1", 0, 101), ("emporia:lowest-unique:v1", 1, 0),
                                         (BLOTTO, [20] * 5, [50, 50])])
def test_each_set_play_says_whether_a_move_could_be_revealed(mt, good, bad):
    """Found by the end-to-end run: commit_move asks this before it commits, since
    the relay sees only a hash until the reveal."""
    mod = get_interaction_module(mt)
    data = mod.initial_state(["a", "b", "c"][:max(2, mod.MIN_PARTICIPANTS)], {}).data
    assert mod.move_problem(data, good) == ""
    assert mod.move_problem(data, bad) and mod.move_problem(data, True)
