"""P186: open, read-only and closed relays."""
import pytest
from fastapi.testclient import TestClient

from emporia import relay_server as rs
from emporia.relay_server import app
from tests.test_emporia import _register, _register_keyed, _signed_listing


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
    assert client.post("/listings", json=_signed_listing(client, "ra_open", "x"),
                       headers=_as("ra_open")).status_code == 200


def test_a_closed_relay_serves_members_only(client, remote, monkeypatch):
    _register_keyed(client, "ra_member")
    _register_keyed(client, "ra_stranger")
    monkeypatch.setenv("EMPORIA_RELAY_ACCESS", "closed")
    monkeypatch.setenv("EMPORIA_RELAY_MEMBERS", _key("ra_member"))
    assert client.post("/listings", json=_signed_listing(client, "ra_member", "mine"),
                       headers=_as("ra_member")).status_code == 200
    r = client.post("/listings", json=_signed_listing(client, "ra_stranger", "theirs"), headers=_as("ra_stranger"))
    assert r.status_code == 403 and "closed" in r.json()["detail"]
    assert client.get("/listings").status_code == 403                    # nobody browses
    assert client.get("/gaming/v1/federate/listings").status_code == 403  # nor pulls its feed
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
    assert client.get("/listings").status_code == 200
    assert client.get("/gaming/v1/federate/listings").status_code == 200
    assert client.post("/listings", json=_signed_listing(client, "ra_ro_visitor", "x"),
                       headers=_as("ra_ro_visitor")).status_code == 403
    assert client.post("/listings", json=_signed_listing(client, "ra_ro_member", "x"),
                       headers=_as("ra_ro_member")).status_code == 200


def test_a_mistyped_mode_closes_rather_than_opens(monkeypatch):
    from emporia import relay_access
    monkeypatch.setenv("EMPORIA_RELAY_ACCESS", "opne")
    assert relay_access.mode() == "closed"


def test_the_operator_is_always_a_member(client, monkeypatch):
    _register(client, "ra_operator")
    monkeypatch.setenv("EMPORIA_RELAY_ACCESS", "closed")
    monkeypatch.setenv("EMPORIA_AGENT_ID", "ra_operator")
    monkeypatch.delenv("EMPORIA_RELAY_MEMBERS", raising=False)
    assert client.get("/listings", headers=_as("ra_operator")).status_code == 200


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


def test_an_agents_nous_account_is_shown_to_itself_and_the_operator_only(client, monkeypatch):
    from emporia import relay_server as rs
    _register(client, "ra_nous_owner")
    _register(client, "ra_nous_other")
    with rs.DB_LOCK, rs.get_db() as conn:
        conn.execute("UPDATE authorized_agents SET nous_user_id = 'nous_private_1', trust_level = 'nous_verified' "
                     "WHERE agent_id = 'ra_nous_owner'")
    assert client.get("/agents/ra_nous_owner", headers=_as("ra_nous_owner")).json()["nous_user_id"] == "nous_private_1"
    seen = client.get("/agents/ra_nous_owner", headers=_as("ra_nous_other")).json()
    assert seen["nous_user_id"] is None and seen["trust_level"] == "nous_verified"
    listed = client.get("/agents", params={"limit": 200}, headers=_as("ra_nous_other")).json()["agents"]
    assert "nous_private_1" not in str(listed)
    monkeypatch.setenv("EMPORIA_AGENT_ID", "ra_nous_other")
    assert client.get("/agents/ra_nous_owner", headers=_as("ra_nous_other")).json()["nous_user_id"] == "nous_private_1"


def test_an_older_database_is_copied_aside_before_it_is_upgraded(tmp_path, monkeypatch):
    """P193: a schema upgrade can drop and alter tables; the operator keeps the old copy."""
    import sqlite3

    from emporia import relay_server as rs
    db = tmp_path / "old.sqlite3"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE challenges (challenge_id TEXT)")
    conn.execute("INSERT INTO challenges VALUES ('kept')")
    conn.execute("PRAGMA user_version = 6")
    conn.commit()
    monkeypatch.setattr(rs, "DATABASE_PATH", db)
    snap = rs._snapshot_before_upgrade(conn)
    assert snap and snap.name.startswith("old.sqlite3.before-v") and oct(snap.stat().st_mode)[-3:] == "600"
    assert sqlite3.connect(snap).execute("SELECT challenge_id FROM challenges").fetchone() == ("kept",)
    conn.execute(f"PRAGMA user_version = {rs.SCHEMA_VERSION}")
    assert rs._snapshot_before_upgrade(conn) is None                # current: nothing to keep


def test_a_hosted_relay_serves_no_dashboard(client, monkeypatch):
    """P196: the dashboard runs next to the agents' keys, never as a public page."""
    from emporia import relay_server as rs
    monkeypatch.setattr(rs, "RELAY_HOST", "0.0.0.0")               # as in the Docker image
    monkeypatch.delenv("EMPORIA_SERVE_UI", raising=False)
    assert not rs.serves_dashboard()
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 200 and "no page to browse" in r.text
    monkeypatch.setenv("EMPORIA_SERVE_UI", "1")
    assert rs.serves_dashboard()


def test_a_relay_taking_real_money_keeps_its_keys_out_of_every_agents_profile(tmp_path, monkeypatch):
    """P202: on testnet a relay may run from a Hermes profile (developing, trying
    Emporia locally); with real money its keys must live apart from every agent."""
    from emporia import relay_server as rs
    profile = tmp_path / "profiles" / "dev"
    (profile / "home" / ".hermes" / "keys").mkdir(parents=True)
    (profile / "config.yaml").write_text("{}")
    in_profile, own = profile / "home" / ".hermes" / "keys", tmp_path / "relay-data" / "keys"
    own.mkdir(parents=True)
    for k in ("STRIPE_SECRET_KEY", "EMPORIA_X402_ENABLED", "EMPORIA_X402_NETWORKS", "EMPORIA_ESCROW_NETWORK",
              "EMPORIA_ALLOW_LIVE_RELAY_IN_PROFILE"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_x")
    monkeypatch.setenv("EMPORIA_ESCROW_NETWORK", "eip155:84532")
    assert rs.live_money_refusal(in_profile) == ""                    # testnet: fine
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_live_x")
    monkeypatch.setenv("EMPORIA_X402_ENABLED", "1")
    monkeypatch.setenv("EMPORIA_X402_NETWORKS", "eip155:8453")
    refusal = rs.live_money_refusal(in_profile)
    assert "a live Stripe key and USDC on a mainnet" in refusal and "emporia relay host" in refusal
    assert "sk_live_x" not in refusal
    assert rs.live_money_refusal(own) == ""                           # its own folder: fine
    monkeypatch.setenv("EMPORIA_ALLOW_LIVE_RELAY_IN_PROFILE", "1")
    assert rs.live_money_refusal(in_profile) == ""


def test_the_operator_removes_an_agent_that_cannot_leave(client, monkeypatch, capsys):
    """P222: two deleted profiles stayed listed — their keys on disk were not the
    registered ones, so they could not leave. The operator removes them."""
    from emporia import cli
    _register(client, "ra_lost_keys")      # its keys on disk are not these, so it cannot sign
    from emporia import relay_server as rs
    with rs.DB_LOCK, rs.get_db() as conn:
        conn.execute("INSERT INTO listings (listing_id, title, agent_id, origin_relay, created_at) "
                     "VALUES ('lst_ra_stale', 'stale', 'ra_lost_keys', ?, '2026-01-01')", (rs.RELAY_BASE_URL,))
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
    assert cli.main(["relay", "remove", "ra_lost_keys"]) == 0 and "already gone" in capsys.readouterr().out
    monkeypatch.setattr(cli, "_persons_terminal", lambda: False)
    assert cli.main(["relay", "remove", "ra_viewer"]) == 1               # an agent's tools cannot
