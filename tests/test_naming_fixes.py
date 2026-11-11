"""Naming review fixes: hex handles, the removal bar across a rename, rename off, display names, affix squats,
`/available`, carried records, invite quoting, commitment files."""

import uuid

import pytest
from fastapi.testclient import TestClient

from emporia import names, operators
from emporia import relay_server as rs
from emporia.relay_server import app
from tests.test_emporia import _test_pubkey


@pytest.fixture
def client():
    return TestClient(app, client=("127.0.0.1", 51218))


def _reg(client, name, display=""):
    return client.post("/agents/register", json={"agent_id": name, "public_key_hex": _test_pubkey(name),
                                                 "display_name": display})


def _u():
    return uuid.uuid4().hex[:6]


def test_long_hex_handle_refused_at_register_and_rename(client):
    r = _reg(client, "a" * 64)
    assert r.status_code == 400 and "hex" in r.text
    assert _reg(client, "0123456789abcdef0123456789abcdef").status_code == 400
    with pytest.raises(ValueError):
        rs._normalize_agent_id("ab" * 20)
    assert _reg(client, f"hexy{_u()}").status_code == 200       # short hex-ish names are fine


def test_rename_keeps_the_operator_removal_bar():
    name = f"bar{_u()}"
    rs.AGENT_REGISTRY.register(name, _test_pubkey(name))
    with rs.DB_LOCK, rs.get_db() as conn:
        conn.execute("INSERT INTO access_removals (removal_id, resource_type, resource_id, agent_id, removed_at, reason) VALUES ('rm_relay','relay','relay',?,'2026-01-01','t')",
                     (name,))
        conn.execute("INSERT INTO access_removals (removal_id, resource_type, resource_id, agent_id, removed_at, reason) VALUES ('rm_room','room','r1',?,'2026-01-01','t')",
                     (name,))
    new = f"{name}x"
    rs.AGENT_REGISTRY.rename(name, new)
    with rs.DB_LOCK, rs.get_db() as conn:
        relay_rows = [r["agent_id"] for r in conn.execute(
            "SELECT agent_id FROM access_removals WHERE resource_type='relay' AND agent_id IN (?,?)",
            (name, new)).fetchall()]
        room_rows = [r["agent_id"] for r in conn.execute(
            "SELECT agent_id FROM access_removals WHERE resource_type='room' AND agent_id IN (?,?)",
            (name, new)).fetchall()]
    assert relay_rows == [name]            # the bar is by stable id: untouched
    assert room_rows == [new]


def test_operators_assign_keys_by_stable_id():
    def row(agent, sid, group=""):
        return {"agent_id": agent, "stable_id": sid, "network_group": group, "network_hash": "",
                "registered_at": None, "operator_label": ""}
    rows = [row("a1", "S1"), row("a2", "S2", "S1"), row("S1", "S3")]
    out = operators.assign(rows)
    assert out["a1"] == out["a2"] and out["S1"] != out["a1"]


def test_rename_route_is_off(client):
    name = f"rn{_u()}"
    assert _reg(client, name).status_code == 200
    r = client.post(f"/agents/{name}/rename", json={"new_agent_id": f"{name}z"}, headers={"x-emporia-agent-id": name})
    assert r.status_code == 403 and "names are fixed" in r.text


def test_a_name_freed_by_leave_is_not_reusable_by_another_key(client):
    name = f"lv{_u()}"
    assert _reg(client, name).status_code == 200
    r = client.post(f"/agents/{name}/leave", headers={"x-emporia-agent-id": name})
    assert r.status_code == 200, r.text
    other = client.post("/agents/register", json={"agent_id": name, "public_key_hex": _test_pubkey(name + "other")})
    assert other.status_code in (400, 409) and "taken" in other.text


@pytest.mark.parametrize("bad", ["a b", "a b", "ㅤ", "⠀⠀", "⠀", "a͸b",
                                 "e" + "́" * 3])
def test_display_name_problem_tightened(bad):
    assert names.display_name_problem(bad)


def test_display_name_cannot_read_as_a_handle_or_reserved_word():
    assert names.display_name_problem("Wuj1tsana", handles=["wujitsana"])
    assert names.display_name_problem("Admin", reserved=["admin"])
    assert not names.display_name_problem("Alice", handles=["alice"], own="alice")
    assert not names.display_name_problem("Alice the Bold", handles=["alice"])


def test_register_refuses_display_name_that_wears_a_handle(client):
    victim = f"vic{_u()}"
    assert _reg(client, victim).status_code == 200
    r = _reg(client, f"dn{_u()}", display=victim.upper())
    assert r.status_code == 400 and "may not read as" in r.text


def test_affix_squats_refused_and_ordinary_names_pass(client, monkeypatch):
    monkeypatch.setenv("EMPORIA_RESERVED_NAMES", "wujitsana, Gandalf")
    for bad in ("wujitsana-official", "real-wujitsana", "wujitsana2", "emporia-support",
                "sys-admin", "the-gandalf"):
        assert _reg(client, bad).status_code in (400, 403), bad
    assert _reg(client, f"alpha2{_u()}").status_code == 200


def test_house_name_is_protected_by_token(client, monkeypatch):
    name = f"hs{_u()}"
    assert _reg(client, name).status_code == 200
    monkeypatch.setenv("EMPORIA_HOUSE_STABLE_IDS", rs.stable_id_for(_test_pubkey(name)))
    r = _reg(client, f"real-{name}")
    assert r.status_code == 400 and "protected" in r.text


def test_operator_name_compared_folded(monkeypatch):
    monkeypatch.setenv("EMPORIA_OPERATOR_STABLE_ID", "d" * 64)
    monkeypatch.setenv("EMPORIA_AGENT_ID", "Boss-Op")
    assert rs.reserved_name_problem("BOSS-OP", "e" * 64)
    assert rs.reserved_name_problem("boss-op", "d" * 64) == ""


def test_available_runs_registration_checks(client, monkeypatch):
    monkeypatch.setenv("EMPORIA_RESERVED_NAMES", "gandalf")
    for bad in ("a" * 40, "real-gandalf", "gandalf", "emporia-relay"):
        assert client.get(f"/agents/{bad}/available").json()["available"] is False, bad
    assert client.get(f"/agents/fresh{_u()}/available").json()["available"] is True


def test_carried_record_refused_without_linked_relays(client, monkeypatch):
    name = f"cr{_u()}"
    assert _reg(client, name).status_code == 200
    monkeypatch.setattr(rs, "FEDERATED_RELAYS", [])
    r = client.post(f"/agents/{name}/carried-record", json={"record": {"relay": "https://x.example"}},
                    headers={"x-emporia-agent-id": name})
    assert r.status_code == 403 and "no linked relays" in r.text


def test_carried_record_keeps_only_known_fields():
    from emporia.moves import _known_fields
    out = _known_fields({"public_key_hex": "ab", "wins": 9999, "note": "inject", "games": {"won": 3, "evil": 9},
                         "counted": 4})
    assert out == {"public_key_hex": "ab", "games": {"won": 3}, "counted": 4}


def test_invite_notices_quote_names():
    from emporia.plugins.platforms.emporia import frames
    src = open(frames.__file__).read()
    assert "_quoted(frame.get('room_name'), 80)" in src and "_quoted(frame.get('name'), 80)" in src


def test_commitment_file_carries_the_relay_host(tmp_path, monkeypatch):
    from emporia import identity, mcp_server
    monkeypatch.setattr(identity, "KEY_DIR", tmp_path)
    files = []
    for host in ("https://one.example", "https://two.example"):
        tok = mcp_server._RELAY_CTX.set(host)
        try:
            files.append(mcp_server._commitments_file("bob", "sess_1"))
        finally:
            mcp_server._RELAY_CTX.reset(tok)
    assert files[0] != files[1]



def test_reregister_with_the_same_display_name_is_never_refused_by_a_later_rule(client, monkeypatch):
    """An agent already here keeps signing in with the display name it has, even if a newer rule would refuse
    it; a changed display name still meets the rules."""
    from emporia import names as _names
    from emporia.identity import get_public_key_hex
    pub = get_public_key_hex("nf_keeps_name")
    body = {"agent_id": "nf_keeps_name", "public_key_hex": pub, "display_name": "Keeper"}
    assert client.post("/agents/register", json=body).status_code == 200
    monkeypatch.setattr(_names, "display_name_problem", lambda *a, **k: "refused by a newer rule")
    r = client.post("/agents/register", json=body)
    assert r.status_code == 200, r.text
    r = client.post("/agents/register", json={**body, "display_name": "Keeper Two"})
    assert r.status_code != 200 and "newer rule" in r.text
