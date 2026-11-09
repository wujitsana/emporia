"""Anti-farming for cross-operator credit: agents that registered from one network count as one operator for a
week (a salted hash, never the address), and a new agent, or one nobody else has dealt with, gives no credit."""
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from emporia import operators as O
from emporia import quest_engine as E
from emporia import relay_server as rs
from tests.test_progression import _register, _session

NOW = datetime(2026, 11, 20, 12, tzinfo=UTC)


def row(agent, label="", net="", days_ago=1.0, stable=None):
    return {"agent_id": agent, "operator_label": label, "stable_id": stable or f"stable-{agent}",
            "network_hash": net, "registered_at": (NOW - timedelta(days=days_ago)).isoformat()}


# ── the network ──────────────────────────────────────────────────────────────

def test_the_same_slash_24_is_one_network_and_another_is_not():
    h = lambda ip: O.network_hash(ip, "salt")   # noqa: E731
    assert h("203.0.113.7") == h("203.0.113.200") != ""
    assert h("203.0.113.7") != h("203.0.114.7")
    assert h("2001:db8:1234:1::1") == h("2001:db8:1234:ffff::9") != ""        # an IPv6 /48
    assert h("2001:db8:1234::1") != h("2001:db8:1235::1")
    assert h("::ffff:203.0.113.9") == h("203.0.113.7"), "an IPv4-mapped address is the IPv4 one"


def test_a_hash_is_salted_and_holds_no_address():
    a, b = O.network_hash("203.0.113.7", "salt-one"), O.network_hash("203.0.113.7", "salt-two")
    assert a != b and len(a) == 32 and "203.0.113" not in a
    assert O.network_hash("203.0.113.7", "") == "", "no salt, no hash"


@pytest.mark.parametrize("ip", ["127.0.0.1", "127.9.9.9", "::1", "10.1.2.3", "172.16.0.5", "172.31.255.1",
                                "192.168.1.20", "fc00::1", "fd12:3456::1", "169.254.1.1", "testclient", "", None])
def test_loopback_private_and_unreadable_addresses_are_never_grouped(ip):
    assert O.network_of(ip) == "" and O.network_hash(ip, "salt") == ""


def test_a_public_address_next_to_a_private_range_is_grouped():
    assert O.network_of("172.32.0.1") != "" and O.network_of("11.0.0.1") != ""


# ── the grouping ─────────────────────────────────────────────────────────────

def test_agents_registered_from_one_network_are_one_operator():
    ops = O.assign([row("a", net="n1"), row("b", net="n1"), row("c", net="n2"), row("d")], NOW.timestamp())
    assert ops["a"] == ops["b"] and ops["c"] != ops["a"] and ops["d"] != ops["a"]
    assert len({ops["c"], ops["d"], ops["a"]}) == 3


def test_a_shared_network_merges_labelled_operators_and_does_not_depend_on_order():
    rows = [row("a", label="op_a", net="n1"), row("b", label="op_b", net="n1"), row("c", label="op_a")]
    ops = O.assign(rows, NOW.timestamp())
    assert ops["a"] == ops["b"] == ops["c"], "the label rule still holds, and the network joins the two labels"
    assert O.assign(list(reversed(rows)), NOW.timestamp()) == ops


def test_a_network_hash_is_ignored_after_seven_days():
    old = [row("a", net="n1", days_ago=8), row("b", net="n1", days_ago=1)]
    ops = O.assign(old, NOW.timestamp())
    assert ops["a"] != ops["b"], "a's hash has expired: it matches nobody"
    assert O.assign([row("a", net="n1", days_ago=6.9), row("b", net="n1", days_ago=1)], NOW.timestamp())["a"] == \
        O.assign([row("a", net="n1", days_ago=6.9), row("b", net="n1", days_ago=1)], NOW.timestamp())["b"]


def test_network_grouping_can_be_switched_off(monkeypatch):
    rows = [row("a", net="n1"), row("b", net="n1")]
    assert O.assign(rows, NOW.timestamp())["a"] == O.assign(rows, NOW.timestamp())["b"]
    monkeypatch.setenv("EMPORIA_NETWORK_GROUPING", "0")
    ops = O.assign(rows, NOW.timestamp())
    assert ops["a"] != ops["b"] and not O.grouping_on()


def test_the_relay_groups_agents_registered_from_one_network_and_stores_no_address(monkeypatch):
    from emporia.routes import agents, standing
    tag = uuid.uuid4().hex[:6]
    a, b, c, p = (f"ng_{x}_{tag}" for x in "abcp")
    for x in (a, b, c, p):
        _register(x)
    with rs.DB_LOCK, rs.get_db() as conn:          # registered just now (the test relay says 2026-01-01)
        conn.execute("UPDATE authorized_agents SET registered_at = ? WHERE agent_id IN (?,?,?,?)",
                     (datetime.now(UTC).isoformat(), a, b, c, p))
    rs.forget_who()
    agents.record_network(a, "198.51.100.10")
    agents.record_network(b, "198.51.100.77")
    agents.record_network(c, "198.51.101.10")
    agents.record_network(p, "10.0.0.5")
    who = standing._who()
    assert who[a].operator == who[b].operator
    assert who[c].operator != who[a].operator and who[p].operator != who[a].operator
    with rs.DB_LOCK, rs.get_db() as conn:
        stored = [r[0] for r in conn.execute("SELECT network_hash FROM authorized_agents WHERE agent_id IN (?,?,?,?)",
                                             (a, b, c, p))]
        salt = conn.execute("SELECT value FROM relay_secrets WHERE name = 'network_salt'").fetchone()[0]
    assert sorted(s for s in stored if s) and "198.51" not in "".join(stored) and len(salt) == 64
    assert stored[3] == "", "a private address is not kept as a group"
    monkeypatch.setenv("EMPORIA_NETWORK_GROUPING", "0")
    rs.forget_who()
    who = standing._who()
    assert who[a].operator != who[b].operator
    rs.forget_who()


def test_the_salt_never_appears_in_health_or_the_card():
    client = TestClient(rs.app)
    from emporia.routes import agents
    with rs.DB_LOCK, rs.get_db() as conn:
        salt = agents._network_salt(conn)
    for path in ("/health", "/relay/card", "/relay/summary"):
        assert salt not in client.get(path).text


# ── the new-agent delay ──────────────────────────────────────────────────────

OPS = {"me": "label:ME", "x": "label:X", "y": "label:Y", "z": "label:Z", "house": "label:HOUSE"}
HOUR = 3600.0
GAME_AT = "2026-11-10T12:00:00+00:00"
T = datetime.fromisoformat(GAME_AT).timestamp()


def ctx(registered, contacts, hours=24):
    return E.Ctx(operator=lambda a: OPS.get(a, a), house=frozenset({"house"}), parts=frozenset(),
                 registered=registered, contacts=contacts, new_agent_s=hours * HOUR)


def first_contact(c):
    g = {"session_id": "s1", "module_type": "emporia:blotto:v2", "participants": ["me", "x"], "ended_at": GAME_AT,
         "winner": None}
    evs = E.events("me", games=[g], ctx=c)
    return "first-contact" in {q["id"] for q in E.evaluate("me", evs, c, now=T + 5 * 86400)["quests"] if q["done"]}


def test_a_counterparty_registered_less_than_a_day_before_gives_no_credit():
    contacts = {"x": {"y"}}
    assert not first_contact(ctx({"x": T - 23 * HOUR}, contacts))
    assert first_contact(ctx({"x": T - 25 * HOUR}, contacts))
    assert not first_contact(ctx({"x": T - 1 * HOUR}, contacts, hours=24))
    assert first_contact(ctx({"x": T - 1 * HOUR}, contacts, hours=0)), "the delay is a setting; 0 is off"


def test_a_counterparty_nobody_else_has_dealt_with_gives_no_credit():
    reg = {"x": T - 30 * HOUR}
    assert not first_contact(ctx(reg, {}))
    assert not first_contact(ctx(reg, {"x": {"me"}})), "the agent being credited is not a third operator"
    assert not first_contact(ctx(reg, {"x": {"house"}})), "nor is the house"
    assert not first_contact(ctx(reg, {"x": {"x"}})), "nor itself"
    assert first_contact(ctx(reg, {"x": {"y"}}))
    assert first_contact(ctx(reg, {"x": {"z", "house"}}))


def test_the_third_operator_must_not_be_the_counterpartys_own_operator_or_the_credited_ones():
    reg = {"x": T - 30 * HOUR}
    ops = dict(OPS, x2="label:X", me2="label:ME")
    c = E.Ctx(operator=lambda a: ops.get(a, a), house=frozenset({"house"}), parts=frozenset(), registered=reg,
              contacts={"x": {"x2", "me2"}}, new_agent_s=24 * HOUR)
    assert not E.vouched("me", "x", GAME_AT, c), "x's other agent and my other agent are not a third operator"


def test_credit_becomes_valid_when_the_counterparty_later_meets_a_third_operator():
    reg = {"x": T - 30 * HOUR}
    assert not first_contact(ctx(reg, {}))
    assert first_contact(ctx(reg, {"x": {"y"}})), "recomputed with the new contact, the old game now counts"


def test_no_delay_outside_a_relay_that_gives_the_engine_registrations():
    c = E.Ctx(operator=lambda a: OPS.get(a, a), house=frozenset({"house"}))
    assert first_contact(c)


def test_the_delay_applies_to_play_xp_too():
    g = {"session_id": "s1", "module_type": "emporia:blotto:v2", "participants": ["me", "x"], "ended_at": GAME_AT,
         "winner": "me", "termination": "rounds"}
    paid = lambda c: E.play_xp("me", [g], c, now=T + 3600)["xp"]   # noqa: E731
    assert paid(ctx({"x": T - 30 * HOUR}, {"x": {"y"}})) == 2
    assert paid(ctx({"x": T - 30 * HOUR}, {})) == 0
    assert paid(ctx({"x": T - 2 * HOUR}, {"x": {"y"}})) == 0


def test_the_relay_gives_credit_only_after_the_counterparty_has_met_a_third_operator(monkeypatch):
    monkeypatch.setenv("EMPORIA_GAMIFY", "sheets,quests")
    monkeypatch.setenv("EMPORIA_NEW_AGENT_HOURS", "24")
    tag = uuid.uuid4().hex[:6]
    a, b, c = (f"nd_{x}_{tag}" for x in "abc")
    for x in (a, b, c):
        _register(x)          # registered long ago (2026-01-01) in the test relay
    data = {"wins": {a: 2, b: 0}, "history": [{}, {}], "rounds": 3}
    _session("emporia:blotto:v2", [a, b], data)
    client = TestClient(rs.app)
    done = lambda x: {q["id"] for q in client.get(f"/agents/{x}/quests").json()["quests"] if q["done"]}   # noqa: E731
    assert "first-contact" not in done(a) and "first-set-play" in done(a), "b has met no third operator yet"
    _session("emporia:blotto:v2", [b, c], {"wins": {b: 2, c: 0}, "history": [{}, {}], "rounds": 3})
    assert "first-contact" in done(a), "b has now played c: the earlier game counts, retroactively"
