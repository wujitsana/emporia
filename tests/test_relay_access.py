"""P186: open, read-only and closed relays."""
import pytest
from fastapi.testclient import TestClient

from emporia import relay_server as rs
from emporia.relay_server import app
from tests.test_emporia import _register, _register_keyed
from tests.test_posts_v2 import _body


from tests.ids import stable_of  # noqa: E402


@pytest.fixture
def client():
    return TestClient(app, client=("127.0.0.1", 51250))


@pytest.fixture
def remote():
    """A visitor from elsewhere: no loopback, so no full /health either."""
    return TestClient(app, client=("203.0.113.7", 51251))


def _key(agent_id: str) -> str:
    from emporia import relay_server as rs
    with rs.DB_LOCK, rs.get_db() as conn:
        return conn.execute("SELECT public_key_hex FROM authorized_agents WHERE agent_id = ?",
                            (agent_id,)).fetchone()[0]


def _as(agent_id: str) -> dict:
    return {"x-emporia-agent-id": agent_id}


def test_an_open_relay_is_as_before(client):
    _register_keyed(client, "ra_open")
    assert client.post("/posts", json=_body("ra_open"),
                       headers=_as("ra_open")).status_code == 200


def test_a_closed_relay_serves_members_only(client, remote, monkeypatch):
    _register_keyed(client, "ra_member")
    _register_keyed(client, "ra_stranger")
    monkeypatch.setenv("EMPORIA_RELAY_ACCESS", "closed")
    monkeypatch.setenv("EMPORIA_RELAY_MEMBERS", _key("ra_member"))
    assert client.post("/posts", json=_body("ra_member"),
                       headers=_as("ra_member")).status_code == 200
    r = client.post("/posts", json=_body("ra_stranger"), headers=_as("ra_stranger"))
    assert r.status_code == 403 and "closed" in r.json()["detail"]
    assert client.get("/listings").status_code == 403                    # nobody browses
    assert client.get("/federation/v1/posts").status_code == 403  # nor pulls its feed
    assert remote.get("/health").json() == {"status": "ok", "service": remote.get("/health").json()["service"],
                                            "access": "closed", "relay_url": rs.RELAY_BASE_URL}
    assert "version" in client.get("/health").json()                     # this machine sees it all


def test_joining_a_closed_or_read_only_relay_takes_a_member_key(client, monkeypatch):
    from emporia.identity import get_public_key_hex
    monkeypatch.setenv("EMPORIA_RELAY_ACCESS", "read-only")
    r = client.post("/agents/register", json={"agent_id": "ra_newcomer",
                                              "public_key_hex": get_public_key_hex("ra_newcomer")})
    assert r.status_code == 403 and "members add" in r.json()["detail"]
    monkeypatch.setenv("EMPORIA_RELAY_MEMBERS", get_public_key_hex("ra_newcomer"))
    r = client.post("/agents/register", json={"agent_id": "ra_newcomer",
                                              "public_key_hex": get_public_key_hex("ra_newcomer")})
    assert r.status_code == 200


def test_a_read_only_relay_lets_anyone_browse_and_members_act(client, monkeypatch):
    _register_keyed(client, "ra_ro_member")
    _register_keyed(client, "ra_ro_visitor")
    monkeypatch.setenv("EMPORIA_RELAY_ACCESS", "read-only")
    monkeypatch.setenv("EMPORIA_RELAY_MEMBERS", _key("ra_ro_member"))
    assert client.get("/leaderboard").status_code == 200
    assert client.get("/federation/v1/posts").status_code == 200
    assert client.post("/posts", json=_body("ra_ro_visitor"),
                       headers=_as("ra_ro_visitor")).status_code == 403
    assert client.post("/posts", json=_body("ra_ro_member"),
                       headers=_as("ra_ro_member")).status_code == 200


def test_a_mistyped_mode_closes_rather_than_opens(monkeypatch):
    from emporia import relay_access
    monkeypatch.setenv("EMPORIA_RELAY_ACCESS", "opne")
    assert relay_access.mode() == "closed"


def test_the_operator_is_always_a_member(client, monkeypatch):
    _register(client, "ra_operator")
    monkeypatch.setenv("EMPORIA_RELAY_ACCESS", "closed")
    monkeypatch.setenv("EMPORIA_OPERATOR_STABLE_ID", stable_of("ra_operator"))
    monkeypatch.delenv("EMPORIA_RELAY_MEMBERS", raising=False)
    assert client.get("/leaderboard", headers=_as("ra_operator")).status_code == 200


def test_access_changes_apply_from_the_env_file_without_a_restart(tmp_path, monkeypatch):
    from emporia import relay_access
    env = tmp_path / ".env"
    env.write_text("EMPORIA_RELAY_ACCESS=open\n")
    monkeypatch.setenv("EMPORIA_RELAY_ENV_FILE", str(env))
    assert relay_access.mode() == "open"
    import os
    import time
    env.write_text("EMPORIA_RELAY_ACCESS=closed\nEMPORIA_RELAY_MEMBERS=" + "ab" * 32 + "\n")
    os.utime(env, (time.time() + 5, time.time() + 5))
    assert relay_access.mode() == "closed" and relay_access.is_member_key("AB" * 32)


def test_an_agents_operator_label_is_shown_to_itself_and_the_operator_only(client, monkeypatch):
    from emporia import relay_server as rs
    _register(client, "ra_nous_owner")
    _register(client, "ra_nous_other")
    with rs.DB_LOCK, rs.get_db() as conn:
        conn.execute("UPDATE authorized_agents SET operator_label = 'op_privatelabel1' "
                     "WHERE agent_id = 'ra_nous_owner'")
    assert client.get("/agents/ra_nous_owner", headers=_as("ra_nous_owner")).json()["operator_label"] == "op_privatelabel1"
    seen = client.get("/agents/ra_nous_owner", headers=_as("ra_nous_other")).json()
    assert seen["operator_label"] is None and seen["has_operator_label"] is True
    # said plainly for another reader, never as "none", and without the label itself
    assert seen["operator"] == "has an operator label (shared by its operator's agents; shown only to them)"
    assert "op_privatelabel1" not in seen["operator"]
    assert "op_privatelabel1" in client.get("/agents/ra_nous_owner", headers=_as("ra_nous_owner")).json()["operator"]
    assert client.get("/agents/ra_nous_other", headers=_as("ra_nous_owner")).json()["operator"] == "no operator label declared"
    listed = client.get("/agents", params={"limit": 200}, headers=_as("ra_nous_other")).json()["agents"]
    assert "op_privatelabel1" not in str(listed)
    monkeypatch.setenv("EMPORIA_OPERATOR_STABLE_ID", stable_of("ra_nous_other"))
    assert client.get("/agents/ra_nous_owner", headers=_as("ra_nous_other")).json()["operator_label"] == "op_privatelabel1"




def test_a_relay_serves_no_dashboard(client):
    """P196/P261: the dashboard runs next to the agents' keys -- the Hermes tab or
    the standalone console -- never from a relay, on any address."""
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 200 and "No screens here" in r.text
    assert client.get("/ui/").status_code == 404


def test_a_relay_taking_real_money_keeps_its_keys_out_of_every_agents_profile(tmp_path, monkeypatch):
    """P202: on testnet a relay may run from a Hermes profile (developing, trying
    Emporia locally); with real money its keys must live apart from every agent."""
    from emporia import relay_server as rs
    profile = tmp_path / "profiles" / "dev"
    (profile / "home" / ".hermes" / "keys").mkdir(parents=True)
    (profile / "config.yaml").write_text("{}")
    in_profile, own = profile / "home" / ".hermes" / "keys", tmp_path / "relay-data" / "keys"
    own.mkdir(parents=True)
    for k in ("EMPORIA_X402_ENABLED", "EMPORIA_X402_NETWORKS", "EMPORIA_ESCROW_NETWORK",
              "EMPORIA_ALLOW_LIVE_RELAY_IN_PROFILE"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("EMPORIA_ESCROW_NETWORK", "eip155:84532")
    assert rs.live_money_refusal(in_profile) == ""                    # testnet: fine
    monkeypatch.setenv("EMPORIA_X402_ENABLED", "1")
    monkeypatch.setenv("EMPORIA_X402_NETWORKS", "eip155:8453")
    refusal = rs.live_money_refusal(in_profile)
    assert "USDC on a mainnet" in refusal and "emporia relay host" in refusal
    assert rs.live_money_refusal(own) == ""                           # its own folder: fine
    monkeypatch.setenv("EMPORIA_ALLOW_LIVE_RELAY_IN_PROFILE", "1")
    assert rs.live_money_refusal(in_profile) == ""


def test_the_operator_removes_an_agent_that_cannot_leave(client, monkeypatch, capsys):
    """P222: two deleted profiles stayed listed — their keys on disk were not the
    registered ones, so they could not leave. The operator removes them."""
    from emporia import cli
    _register(client, "ra_lost_keys")      # its keys on disk are not these, so it cannot sign
    _register(client, "ra_viewer")
    listed = lambda: [a["agent_id"] for a in client.get("/agents", params={"limit": 200},   # noqa: E731
                                                          headers=_as("ra_viewer")).json()["agents"]]
    assert "ra_lost_keys" in listed()
    monkeypatch.setattr(cli, "_persons_terminal", lambda: True)
    monkeypatch.setattr(cli, "prompt", lambda q, default="": "ra_lost_keys")
    assert cli.main(["relay", "remove", "ra_lost_keys"]) == 0
    assert "removed from this relay" in capsys.readouterr().out
    assert "ra_lost_keys" not in listed()                                  # gone from the list
    every = client.get("/agents", params={"limit": 200, "include_inactive": "true"}, headers=_as("ra_viewer"))
    assert "ra_lost_keys" in [a["agent_id"] for a in every.json()["agents"]]
    assert cli.main(["relay", "remove", "ra_lost_keys"]) == 0 and "barred from registering" in capsys.readouterr().out
    monkeypatch.setattr(cli, "_persons_terminal", lambda: False)
    assert cli.main(["relay", "remove", "ra_viewer"]) == 1               # an agent's tools cannot


def test_the_cli_sets_the_mode_and_removes_a_member_in_the_relays_env_file(tmp_path, monkeypatch, capsys):
    """P186: `emporia relay access MODE` and `relay members add|remove KEY` write the relay's
    .env, and the relay applies it from there without a restart."""
    from emporia import cli, relay_access
    env = tmp_path / ".env"
    env.write_text("EMPORIA_FEE_BPS=300\n")
    monkeypatch.setenv("EMPORIA_RELAY_ENV_FILE", str(env))
    monkeypatch.setattr(cli, "_agent_profiles", lambda _root: [])
    alpha, beta = "a1" * 32, "b2" * 32
    dirs = ["--dir", str(tmp_path)]
    assert cli.main(["relay", "members", "add", alpha, beta, *dirs]) == 0
    assert cli.main(["relay", "access", "closed", *dirs]) == 0
    out = capsys.readouterr().out
    assert "This relay is now closed" in out and "emporia relay restart" in out
    assert relay_access.mode() == "closed" and relay_access.is_member_key(alpha) and relay_access.is_member_key(beta)
    assert "EMPORIA_FEE_BPS=300" in env.read_text(), "the other settings stay"

    assert cli.main(["relay", "members", "remove", beta, *dirs]) == 0
    assert "removed" in capsys.readouterr().out
    import os
    import time
    os.utime(env, (time.time() + 10, time.time() + 10))
    assert relay_access.is_member_key(alpha) and not relay_access.is_member_key(beta), "no restart needed"

    assert cli.main(["relay", "access", "read-only", *dirs]) == 0
    os.utime(env, (time.time() + 20, time.time() + 20))
    assert relay_access.mode() == "read-only"
    assert cli.main(["relay", "access", "wide", *dirs]) == 1, "an unknown mode is refused"
    assert "one of open, read-only, closed" in capsys.readouterr().out
    assert cli.main(["relay", "access", "open", *dirs]) == 0
    os.utime(env, (time.time() + 30, time.time() + 30))
    assert relay_access.mode() == "open"


def test_setup_can_ask_whether_a_name_is_free(client, monkeypatch):
    """P296: before setup writes a profile, it asks the relay whether its name is
    taken. The answer is yes or no only, and a closed relay gives it to members only."""
    _register_keyed(client, "ra_taken")
    assert client.get("/agents/ra_taken/available").json() == {
        "agent_id": "ra_taken", "available": False, "reason": "taken"}
    assert client.get("/agents/RA_Free/available").json() == {
        "agent_id": "ra_free", "available": True, "reason": ""}
    assert client.get("/agents/admin/available").json()["available"] is False
    assert client.get("/agents/x!/available").json()["available"] is False
    monkeypatch.setenv("EMPORIA_RELAY_ACCESS", "closed")
    assert client.get("/agents/ra_free/available").status_code == 403
