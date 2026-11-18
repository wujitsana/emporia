"""EMPORIA_AGENTS_PER_OPERATOR: a relay caps how many agents one operator registers, by declared grouping only."""
import secrets

import pytest

from emporia import operators as O
from emporia import relay_server as rs

REG = rs.AGENT_REGISTRY


def _key():
    return secrets.token_hex(32)


def _add(name, label="", key=None):
    key = key or _key()
    REG.register(f"{name}_{secrets.token_hex(3)}", key, operator_label=label)
    return key


def _label():
    return O.new_label()


def test_the_cap_refuses_a_new_agent_of_an_operator_at_the_limit(monkeypatch):
    monkeypatch.setenv("EMPORIA_AGENTS_PER_OPERATOR", "2")
    label = _label()
    _add("cap", label), _add("cap", label)
    with pytest.raises(rs.OperatorCapError) as err:
        _add("cap", label)
    msg = str(err.value)
    assert "2 agents per operator" in msg and "EMPORIA_AGENTS_PER_OPERATOR" in msg and "has 2" in msg
    assert "cap_" not in msg, "never names the other agents"


def test_a_label_counts_and_a_different_label_does_not(monkeypatch):
    monkeypatch.setenv("EMPORIA_AGENTS_PER_OPERATOR", "1")
    one, two = _label(), _label()
    _add("lab", one)
    _add("lab", two)                          # another operator: fine, even registered right after
    with pytest.raises(rs.OperatorCapError):
        _add("lab", one)


def test_a_shared_network_never_causes_a_refusal(monkeypatch):
    monkeypatch.setenv("EMPORIA_AGENTS_PER_OPERATOR", "1")
    first = f"net_{secrets.token_hex(3)}"
    REG.register(first, _key(), operator_label=_label())
    with rs.DB_LOCK, rs.get_db() as conn:
        conn.execute("UPDATE authorized_agents SET network_hash = 'same-net' WHERE agent_id = ?", (first,))
    second = f"net_{secrets.token_hex(3)}"
    REG.register(second, _key(), operator_label=_label())        # a different label from the same network
    with rs.DB_LOCK, rs.get_db() as conn:
        conn.execute("UPDATE authorized_agents SET network_hash = 'same-net', network_group = ? "
                     "WHERE agent_id = ?", (first, second))
    REG.register(f"net_{secrets.token_hex(3)}", _key(), operator_label=_label())


def test_the_relay_operator_and_the_house_are_exempt_by_key_only(monkeypatch):
    monkeypatch.setenv("EMPORIA_AGENTS_PER_OPERATOR", "1")
    op_key, house_key = _key(), _key()
    monkeypatch.setenv("EMPORIA_OPERATOR_STABLE_ID", rs.stable_id_for(op_key))
    monkeypatch.setenv("EMPORIA_HOUSE_STABLE_IDS", rs.stable_id_for(house_key))
    label = _label()
    _add("own", label)                         # one ordinary agent fills the cap of 1 for this label
    _add("own", label, op_key)                 # the relay operator's own agent: exempt by its key
    with pytest.raises(rs.OperatorCapError):
        _add("own", label)                     # sharing its label exempts nobody: a label can be copied
    hl = _label()
    _add("hse", hl)
    _add("hse", hl, house_key)                 # a house agent: exempt by its key
    with pytest.raises(rs.OperatorCapError):
        _add("hse", hl)


def test_agents_already_over_the_cap_can_still_re_register(monkeypatch):
    label = _label()
    names = []
    for _ in range(3):
        n = f"old_{secrets.token_hex(3)}"
        k = _key()
        REG.register(n, k, operator_label=label)
        names.append((n, k))
    monkeypatch.setenv("EMPORIA_AGENTS_PER_OPERATOR", "1")
    for n, k in names:
        assert REG.register(n, k, operator_label=label, display_name="")["agent_id"] == n
    with pytest.raises(rs.OperatorCapError):
        _add("old", label)


def test_zero_is_no_cap(monkeypatch):
    monkeypatch.setenv("EMPORIA_AGENTS_PER_OPERATOR", "0")
    label = _label()
    for _ in range(7):
        _add("zero", label)


@pytest.mark.parametrize("bad", ["", "abc", "-3", "2.5"])
def test_a_bad_value_falls_back_to_the_default(monkeypatch, bad):
    monkeypatch.setenv("EMPORIA_AGENTS_PER_OPERATOR", bad)
    assert rs.agents_per_operator() == 5
    monkeypatch.delenv("EMPORIA_AGENTS_PER_OPERATOR")
    assert rs.agents_per_operator() == 5


def test_the_route_answers_403_and_health_publishes_the_limit(client, monkeypatch):
    monkeypatch.setenv("EMPORIA_AGENTS_PER_OPERATOR", "1")
    label = _label()
    body = {"public_key_hex": _key(), "operator_label": label}
    first = client.post("/agents/register", json={**body, "agent_id": f"rt_{secrets.token_hex(3)}"})
    if first.status_code != 200:
        pytest.skip(f"the test relay wants a signed registration here ({first.status_code})")
    second = client.post("/agents/register", json={"public_key_hex": _key(), "operator_label": label,
                                                   "agent_id": f"rt_{secrets.token_hex(3)}"})
    assert second.status_code == 403 and "per operator" in second.json()["detail"]
    assert client.get("/health").json()["agents_per_operator"] == 1
