"""Second-order counterparties (2.1, review proposal E): an operator counts toward Trust and the allowance only if it
has itself finished with at least r operators besides the agent's own and itself, r = min(2, max(0, others - 1)).

Off by default (`EMPORIA_SECOND_ORDER`): the small-relay simulations below show it blocks honest sparse play (a ring
or a chain of operators, a hub with single-contact friends) as surely as it blocks the label crowd it is aimed at, so a
relay's operator opts in once its operators play with several others each."""
import itertools

import pytest

from emporia import allowance as al
from emporia import progression as pg

NOW = 100 * 86_400.0


def run(ops, edges, mode, monkeypatch):
    """Every operator's earned tier on a relay of `ops`, all active and 100 days old, who finished with `edges`."""
    monkeypatch.setenv("EMPORIA_SECOND_ORDER", mode)
    contacts = {op: {} for op in ops}
    for a, b in edges:
        contacts[a][b] = contacts[b][a] = 1.0
    who = {op: pg.Who(op, 0) for op in ops}
    graph = al.operator_graph(contacts, who)
    rule = al.second_order(len(ops) - 1)
    return {op: al.assess(op, weighted=al.weights(al.counted_contacts(contacts[op], op, graph, rule), who, NOW),
                          others=len(ops) - 1, days_here=100, now=NOW)["tier"] for op in ops}


def clique(xs):
    return list(itertools.combinations(xs, 2))


def ring(xs):
    return [(xs[i], xs[(i + 1) % len(xs)]) for i in range(len(xs))]


def star(xs):
    return [(xs[0], x) for x in xs[1:]]


def chain(xs):
    return list(zip(xs, xs[1:], strict=False))


def known(tiers):
    return sum(1 for t in tiers.values() if t >= 2)


def test_the_rule_needs_two_other_operators_before_it_applies_and_is_off_by_default(monkeypatch):
    monkeypatch.delenv("EMPORIA_SECOND_ORDER", raising=False)
    assert [al.second_order_required(n) for n in (0, 1, 2, 3, 4, 20)] == [0, 0, 1, 2, 2, 2]
    assert al.second_order_mode() == "off" and al.second_order(10) is None
    monkeypatch.setenv("EMPORIA_SECOND_ORDER", "on")
    assert al.second_order(1) is None and al.second_order(2) == {"mode": "strict", "required": 1, "fail_weight": 0.0}
    monkeypatch.setenv("EMPORIA_SECOND_ORDER", "soft")
    assert al.second_order(5) == {"mode": "soft", "required": 2, "fail_weight": 0.25}
    monkeypatch.setenv("EMPORIA_SECOND_ORDER", "nonsense")
    assert al.second_order_mode() == "off"


@pytest.mark.parametrize("shape", ["clique", "ring", "star", "chain"])
def test_two_operator_relays_are_unaffected(shape, monkeypatch):
    ops = ["a", "b"]
    assert run(ops, [("a", "b")], "on", monkeypatch) == run(ops, [("a", "b")], "off", monkeypatch) == {"a": 2, "b": 2}


def test_simulation_honest_play_where_operators_have_several_contacts_is_unaffected(monkeypatch):
    for n in (3, 5, 10):
        ops = [f"h{i}" for i in range(n)]
        assert known(run(ops, clique(ops), "on", monkeypatch)) == n, n
    assert known(run(["a", "b", "c"], ring(["a", "b", "c"]), "on", monkeypatch)) == 3     # a ring of three is a triangle


def test_simulation_honest_sparse_play_is_stuck_where_it_is_on_so_it_stays_off_by_default(monkeypatch):
    """The cost, measured: rings, chains and hubs of operators who each know one or two others reach Known when the
    rule is off and nobody does when it is on (soft mode, a quarter weight, does not rescue them either)."""
    for n in (5, 10):
        ops = [f"h{i}" for i in range(n)]
        for shape, off in ((ring, n), (chain, n - 2)):
            assert known(run(ops, shape(ops), "off", monkeypatch)) == off, (n, shape.__name__)
            assert known(run(ops, shape(ops), "on", monkeypatch)) == 0, (n, shape.__name__)
            assert known(run(ops, shape(ops), "soft", monkeypatch)) == 0, (n, shape.__name__)
    hub = [f"h{i}" for i in range(5)]
    assert known(run(hub, star(hub), "off", monkeypatch)) == 1 and known(run(hub, star(hub), "on", monkeypatch)) == 0


def test_simulation_a_crowd_of_labels_that_only_play_the_target_is_stopped(monkeypatch):
    for n in (3, 5, 10):
        ops = ["T", *[f"S{i}" for i in range(n - 1)]]
        edges = [("T", x) for x in ops[1:]]
        assert run(ops, edges, "off", monkeypatch)["T"] == 3, n
        assert run(ops, edges, "on", monkeypatch)["T"] == 1, n


def test_simulation_two_colluding_labels_vouching_each_other_are_stopped_on_a_bigger_relay(monkeypatch):
    for n in (5, 10):
        rest = [f"h{i}" for i in range(n - 3)]
        ops = ["T", "S1", "S2", *rest]
        edges = [("T", "S1"), ("T", "S2"), ("S1", "S2"), *clique(rest)]
        assert run(ops, edges, "off", monkeypatch)["T"] == 2, n
        assert run(ops, edges, "on", monkeypatch)["T"] == 1, n
    # on a relay of three the pair is the whole relay: the rule needs one other operator and they provide it
    assert run(["T", "S1", "S2"], [("T", "S1"), ("T", "S2"), ("S1", "S2")], "on", monkeypatch)["T"] == 3


def test_simulation_a_clique_of_labels_that_all_play_each_other_is_not_stopped(monkeypatch):
    """The stated limit: the rule raises the cost from one game per label to a game between every pair of labels,
    it does not make a clique impossible. The 30-day age ramp and the real-operator evidence still apply."""
    ops = ["T", *[f"S{i}" for i in range(4)]]
    assert run(ops, clique(ops), "on", monkeypatch)["T"] == 3


def test_trust_counts_only_counterparties_that_have_finished_with_others(monkeypatch):
    who = {x: pg.Who(x, 0) for x in ("me", "b", "c", "d")}
    games = [{"session_id": "g1", "module_type": "emporia:blotto:v2", "participants": ["me", "b"], "winner": "me",
              "ended_at": 86_400.0}]
    table = pg.craft_ratings(games, who)
    now = 40 * 86_400.0

    def trust(rule):
        return pg.base_sheet("me", table=table, deals=[], abandoned=[], registered_at=0, now=now, who=who,
                             second_order=rule)["trust"]
    assert trust(None)["counterparties"] == 1
    alone = {"graph": {"b": {"me"}}, "required": 2, "fail_weight": 0.0}          # b has finished only with me
    assert trust(alone)["counterparties"] == 0 and trust(alone)["score"] == 0 and trust(alone)["started"] is False
    soft = trust({**alone, "fail_weight": 0.25})
    assert soft["counterparties"] == 1 and 0 < soft["score"] < trust(None)["score"]
    connected = {"graph": {"b": {"me", "c", "d"}}, "required": 2, "fail_weight": 0.0}
    assert trust(connected)["counterparties"] == 1 and trust(connected)["score"] == trust(None)["score"]


def test_the_relay_applies_it_only_when_switched_on(monkeypatch):
    """Through the relay's own helpers: a lone label that played only the target counts for nothing when on."""
    from emporia.routes import standing as st
    ev = {"who": {x: pg.Who(x, 0) for x in ("T", "s1", "s2", "o1", "o2")},
          "operators": {"T", "s1", "s2", "o1", "o2"},
          "contacts": {"T": {"s1": 1.0, "s2": 1.0}, "s1": {"T": 1.0}, "s2": {"T": 1.0}, "o1": {"o2": 1.0}, "o2": {"o1": 1.0}}}
    ev["graph"] = al.operator_graph(ev["contacts"], ev["who"])
    monkeypatch.delenv("EMPORIA_SECOND_ORDER", raising=False)
    assert set(st._counted_contacts("T", ev)) == {"s1", "s2"}
    monkeypatch.setenv("EMPORIA_SECOND_ORDER", "on")
    assert st._counted_contacts("T", ev) == {}
