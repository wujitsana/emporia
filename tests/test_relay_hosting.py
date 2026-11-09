"""A relay hosted beside Hermes on one VPS: the compose snippet, who the client is behind
the front door, the limits that keep a flood from starving the host, and the data move."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
import yaml
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from emporia import proxy_ip, relay_host, relay_move

SHA = "0123456789abcdef0123456789abcdef01234567"
REPO = Path(__file__).resolve().parent.parent


def snippet(door: str) -> dict:
    return yaml.safe_load(relay_host.snippet_yaml(door, "${HOME}/.hermes/emporia-relay", relay_host.REPO_URL, SHA,
                                                  "relay.example.com"))


# ─── the snippet ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("door", ["tunnel", "caddy"])
def test_the_relay_is_hardened_and_published_on_no_port(door):
    relay = snippet(door)["services"]["emporia-relay"]
    assert relay["build"] == f"{relay_host.REPO_URL}#{SHA}" and len(SHA) == 40
    assert relay["user"] == "10001:10001"
    assert relay["read_only"] is True and relay["tmpfs"] == ["/tmp"]
    assert relay["cap_drop"] == ["ALL"] and "cap_add" not in relay
    assert relay["security_opt"] == ["no-new-privileges:true"]
    assert relay["mem_limit"] and relay["cpus"] and relay["pids_limit"]
    assert relay["healthcheck"]["test"][-1].count("/health") == 1
    assert "ports" not in relay and relay["expose"] == ["8088"]          # only the compose network reaches it
    assert relay["networks"] == ["emporia-edge"]
    assert relay["logging"]["options"]["max-size"]
    mounts = relay["volumes"]
    assert not any("docker.sock" in m for m in mounts)
    assert any(m.endswith(":/app/relay.env:ro") for m in mounts) and any(m.endswith(":/run/secrets:ro") for m in mounts)
    assert "emporia-data:/data" in mounts                                 # identity, operator key, database
    assert "FORWARDED_ALLOW_IPS=*" not in relay["environment"]


def test_the_tunnel_publishes_nothing_and_the_relay_believes_only_its_address():
    spec = snippet("tunnel")
    door = spec["services"]["emporia-cloudflared"]
    assert "ports" not in door and "emporia-caddy" not in spec["services"]
    assert ":" in door["image"] and "latest" not in door["image"]
    assert door["read_only"] and door["cap_drop"] == ["ALL"] and door["security_opt"] == ["no-new-privileges:true"]
    assert "--token-file" in door["command"] and not any("TUNNEL_TOKEN" in str(v) for v in door.values())
    ip = door["networks"]["emporia-edge"]["ipv4_address"]
    env = spec["services"]["emporia-relay"]["environment"]
    assert f"EMPORIA_TRUSTED_PROXIES={ip}" in env and "EMPORIA_CLIENT_IP_HEADER=cf-connecting-ip" in env
    assert ipaddress_in(ip, spec["networks"]["emporia-edge"]["ipam"]["config"][0]["subnet"])


def test_caddy_takes_80_and_443_and_the_relay_believes_x_forwarded_for_from_it_only():
    spec = snippet("caddy")
    door = spec["services"]["emporia-caddy"]
    assert door["ports"] == ["80:80", "443:443"] and door["cap_drop"] == ["ALL"] and door["cap_add"] == ["NET_BIND_SERVICE"]
    env = spec["services"]["emporia-relay"]["environment"]
    assert "EMPORIA_CLIENT_IP_HEADER=x-forwarded-for" in env
    assert f"EMPORIA_TRUSTED_PROXIES={door['networks']['emporia-edge']['ipv4_address']}" in env
    assert "emporia-caddy-data" in spec["volumes"]
    assert "header_up X-Forwarded-For {remote_host}" in relay_host.into_compose_caddyfile("relay.example.com")


def ipaddress_in(ip: str, net: str) -> bool:
    import ipaddress
    return ipaddress.ip_address(ip) in ipaddress.ip_network(net)


def test_merged_into_a_hermes_compose_file_the_relay_shares_no_network_with_hermes():
    hermes = yaml.safe_load("""
services:
  hermes:
    image: nousresearch/hermes-agent:latest
    volumes: ["${HOME}/.hermes:/opt/data"]
volumes:
  other: {}
""")
    for door in ("tunnel", "caddy"):
        spec = snippet(door)
        merged = {k: {**hermes.get(k, {}), **spec.get(k, {})} for k in ("services", "volumes", "networks")}
        assert not set(merged["services"]["hermes"].get("networks", ["default"])) & set(
            merged["services"]["emporia-relay"]["networks"])
        assert "emporia-edge" in merged["networks"] and "other" in merged["volumes"]
        for name, svc in merged["services"].items():
            if name.startswith("emporia-"):
                assert not any(str(v).startswith(("${HOME}/.hermes:", "/opt/data")) for v in svc.get("volumes", []))


def test_the_snippet_refuses_an_unknown_front_door():
    with pytest.raises(ValueError):
        relay_host.snippet_yaml("nginx", "x", relay_host.REPO_URL, SHA, "relay.example.com")


def test_the_image_builds_from_a_full_pinned_commit(tmp_path):
    (tmp_path / "hermes_plugin" / "emporia").mkdir(parents=True)
    (tmp_path / "hermes_plugin" / "emporia" / "emporia_source.json").write_text(
        '{"repo": "https://github.com/example/emporia", "sha": "%s"}' % SHA)
    assert relay_host.source_pin(tmp_path) == ("https://github.com/example/emporia.git", SHA)
    url, sha = relay_host.source_pin(REPO)                 # the pin is empty here: this checkout's HEAD
    assert len(sha) == 40 and url.endswith(".git")
    with pytest.raises(ValueError):
        relay_host.source_pin(tmp_path / "nowhere")


def test_the_host_sees_the_folder_through_the_data_volume():
    assert relay_host.host_path(Path("/opt/data/profiles/x/emporia-relay")) == "${HOME}/.hermes/profiles/x/emporia-relay"
    assert relay_host.host_path(Path("/opt/data")) == "${HOME}/.hermes"
    assert relay_host.host_path(Path("/srv/relay"), host_data="/h") == "/srv/relay"


def test_the_steps_name_the_commands_the_person_runs(tmp_path):
    steps = "\n".join(relay_host.into_compose_steps("tunnel", "${HOME}/.hermes/er", "relay.example.com", True, ["money"]))
    assert "Tunnels" in steps and "http://emporia-relay:8088" in steps and "WebSockets" in steps
    assert "operator_signer generate" in steps and "chown -R 10001" in steps
    caddy = "\n".join(relay_host.into_compose_steps("caddy", "h", "relay.example.com", False, []))
    assert "ufw allow 22,80,443/tcp" in caddy and "Cloudflare" not in caddy


def test_into_compose_writes_a_snippet_and_the_folders(tmp_path):
    written = relay_host.write_into_compose_files(tmp_path / "er", "tunnel", "h", relay_host.REPO_URL, SHA, "relay.example.com")
    assert [p.name for p in written] == [relay_host.SNIPPET]
    assert (tmp_path / "er" / "secrets").is_dir() and (tmp_path / "er" / "cloudflared").is_dir()
    assert oct((tmp_path / "er" / "cloudflared").stat().st_mode & 0o777) == "0o700"
    with pytest.raises(FileExistsError):                  # a file it did not make is never replaced
        (tmp_path / "er" / relay_host.SNIPPET).write_text("mine")
        relay_host.write_into_compose_files(tmp_path / "er", "tunnel", "h", relay_host.REPO_URL, SHA, "relay.example.com")


def test_the_image_runs_as_a_fixed_non_root_user():
    text = (REPO / "Dockerfile").read_text()
    assert "USER 10001:10001" in text and "useradd --system --uid 10001" in text


# ─── the client's address behind the front door ───────────────────────────────

TRUSTED = proxy_ip.parse_trusted("172.29.231.10")


def test_a_trusted_proxy_names_the_client_by_its_header():
    assert proxy_ip.client_ip("172.29.231.10", {"cf-connecting-ip": "198.51.100.7"}, "cf-connecting-ip", TRUSTED) == "198.51.100.7"
    assert proxy_ip.client_ip("172.29.231.10", {"cf-connecting-ip": "2001:db8::7"}, "cf-connecting-ip", TRUSTED) == "2001:db8::7"


def test_nobody_else_can_name_a_client():
    h = {"cf-connecting-ip": "198.51.100.7", "x-forwarded-for": "198.51.100.7"}
    assert proxy_ip.client_ip("203.0.113.9", h, "cf-connecting-ip", TRUSTED) == "203.0.113.9"
    assert proxy_ip.client_ip("172.29.231.11", h, "x-forwarded-for", TRUSTED) == "172.29.231.11"   # a neighbour, not the proxy


def test_x_forwarded_for_is_read_from_the_right_so_a_forged_entry_counts_for_nothing():
    forged = {"x-forwarded-for": "10.9.9.9, 198.51.100.7"}      # the client wrote the first; the proxy added the last
    assert proxy_ip.client_ip("172.29.231.10", forged, "x-forwarded-for", TRUSTED) == "198.51.100.7"
    chain = {"x-forwarded-for": "1.1.1.1, 198.51.100.7, 172.29.231.10"}
    assert proxy_ip.client_ip("172.29.231.10", chain, "x-forwarded-for", TRUSTED) == "198.51.100.7"


@pytest.mark.parametrize("value", ["127.0.0.1", "::1", "0.0.0.0", "169.254.1.1", "not-an-ip", "", "1.1.1.1,2.2.2.2"])
def test_a_header_that_is_not_a_client_is_ignored(value):
    # loopback would be exempt from limits and may read a closed relay's /health
    assert proxy_ip.client_ip("172.29.231.10", {"cf-connecting-ip": value}, "cf-connecting-ip", TRUSTED) == "172.29.231.10"


def test_unconfigured_it_does_nothing():
    assert proxy_ip.client_ip("172.29.231.10", {"cf-connecting-ip": "198.51.100.7"}, "", TRUSTED) == "172.29.231.10"
    assert proxy_ip.client_ip("172.29.231.10", {"cf-connecting-ip": "198.51.100.7"}, "cf-connecting-ip", []) == "172.29.231.10"


def _clear(rs, *ips):
    for ip in ips:
        for bucket in rs._RATE_LIMITS:
            rs._RATE_WINDOWS.pop((ip, bucket), None)


def test_behind_the_tunnel_each_client_has_its_own_limit_and_a_forged_header_gets_none():
    from emporia import relay_server as rs
    wrapped = proxy_ip.ClientIPFromProxy(rs.app, "cf-connecting-ip", TRUSTED)
    via = TestClient(wrapped, client=("172.29.231.10", 5000))
    direct = TestClient(wrapped, client=("203.0.113.9", 5001))          # not the proxy: reached some other way
    try:
        limit = rs._RATE_LIMITS["auth"][0]
        codes = [via.post("/dashboard/challenge", headers={"CF-Connecting-IP": "198.51.100.7"}).status_code
                 for _ in range(limit + 1)]
        assert codes[-1] == 429 and set(codes[:-1]) == {200}
        assert via.post("/dashboard/challenge", headers={"CF-Connecting-IP": "198.51.100.8"}).status_code == 200
        # a header from a stranger changes nothing: its own address is what is counted
        codes = [direct.post("/dashboard/challenge", headers={"CF-Connecting-IP": f"198.51.100.{100 + i}"}).status_code
                 for i in range(limit + 1)]
        assert codes[-1] == 429
    finally:
        _clear(rs, "198.51.100.7", "198.51.100.8", "172.29.231.10", "203.0.113.9")


def test_behind_caddy_the_real_client_is_the_rightmost_untrusted_forwarded_address():
    from emporia import relay_server as rs
    via = TestClient(proxy_ip.ClientIPFromProxy(rs.app, "x-forwarded-for", TRUSTED), client=("172.29.231.10", 5000))
    try:
        limit = rs._RATE_LIMITS["auth"][0]
        for _ in range(limit):
            assert via.post("/dashboard/challenge", headers={"X-Forwarded-For": "198.51.100.20"}).status_code == 200
        assert via.post("/dashboard/challenge", headers={"X-Forwarded-For": "198.51.100.20"}).status_code == 429
        # padding the front of the list with fresh addresses does not buy a fresh budget
        assert via.post("/dashboard/challenge", headers={"X-Forwarded-For": "9.9.9.9, 198.51.100.20"}).status_code == 429
        assert via.post("/dashboard/challenge", headers={"X-Forwarded-For": "198.51.100.21"}).status_code == 200
    finally:
        _clear(rs, "198.51.100.20", "198.51.100.21", "9.9.9.9", "172.29.231.10")


# ─── limits ───────────────────────────────────────────────────────────────────

def test_unauthenticated_routes_are_limited_per_address(monkeypatch):
    from emporia import relay_server as rs
    monkeypatch.setitem(rs._RATE_LIMITS, "default", (3, 60.0))
    far = TestClient(rs.app, client=("203.0.113.60", 6000))
    other = TestClient(rs.app, client=("203.0.113.61", 6001))
    try:
        assert [far.get("/health").status_code for _ in range(3)] == [200, 200, 200]
        refused = far.get("/health")
        assert refused.status_code == 429 and "Retry-After" in refused.headers
        assert other.get("/health").status_code == 200
        auth = rs._RATE_LIMITS["auth"][0]
        codes = [far.post("/agents/challenge", json={}).status_code for _ in range(auth + 1)]
        assert codes[-1] == 429                       # registration's challenge is throttled as sign-in is
    finally:
        _clear(rs, "203.0.113.60", "203.0.113.61")


def test_the_limits_are_settings_with_defaults():
    from emporia import relay_config as rc
    for name, default in [("EMPORIA_IP_RATE_READ_PER_MINUTE", "120"), ("EMPORIA_IP_RATE_WRITE_PER_MINUTE", "60"),
                          ("EMPORIA_IP_RATE_AUTH_PER_MINUTE", "10"), ("EMPORIA_MAX_WEBSOCKETS_PER_ADDRESS", "64"),
                          ("EMPORIA_MAX_WEBSOCKETS_PER_AGENT", "16"), ("EMPORIA_MAX_WEBSOCKETS", "256"),
                          ("EMPORIA_MAX_CONNECTIONS", "512"), ("EMPORIA_KEEPALIVE_SECONDS", "5"),
                          ("EMPORIA_LISTEN_BACKLOG", "1024"), ("EMPORIA_MAX_REQUEST_BYTES", "1048576")]:
        assert rc.BY_NAME[name].default == default and rc.BY_NAME[name].kind == "int"
        assert rc.check(rc.BY_NAME[name], "7") == "7"
        with pytest.raises(ValueError):
            rc.check(rc.BY_NAME[name], "many")
    assert rc.check(rc.BY_NAME["EMPORIA_CLIENT_IP_HEADER"], "cf-connecting-ip") == "cf-connecting-ip"
    with pytest.raises(ValueError):
        rc.check(rc.BY_NAME["EMPORIA_CLIENT_IP_HEADER"], "x-real-ip")


def test_uvicorn_gets_connection_limits(monkeypatch):
    from emporia import relay_server as rs
    for name in ("EMPORIA_MAX_CONNECTIONS", "EMPORIA_KEEPALIVE_SECONDS", "EMPORIA_LISTEN_BACKLOG",
                 "EMPORIA_WS_MAX_MESSAGE_BYTES"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(rs, "CLIENT_IP_HEADER", "")
    assert rs.uvicorn_limits() == {"limit_concurrency": 512, "timeout_keep_alive": 5, "backlog": 1024,
                                   "ws_max_size": 1048576}
    assert rs._WS_TOTAL < 512                                 # sockets never use up every connection
    monkeypatch.setenv("EMPORIA_MAX_CONNECTIONS", "100")
    monkeypatch.setenv("EMPORIA_LISTEN_BACKLOG", "0")
    limits = rs.uvicorn_limits()
    assert limits["limit_concurrency"] == 100 and "backlog" not in limits
    monkeypatch.setattr(rs, "CLIENT_IP_HEADER", "cf-connecting-ip")
    assert rs.uvicorn_limits()["proxy_headers"] is False      # one place decides whom to believe


def test_one_agent_cannot_hold_more_than_its_share_of_websockets(monkeypatch):
    from emporia import relay_server as rs
    monkeypatch.setattr(rs, "_WS_PER_AGENT", 2)
    monkeypatch.setattr(rs, "_ws_agent_id", lambda ws: "agent-a" if ws.headers.get("x-test") == "a" else None)
    a1 = TestClient(rs.app, client=("203.0.113.70", 7001), headers={"x-test": "a"})
    a2 = TestClient(rs.app, client=("203.0.113.71", 7002), headers={"x-test": "a"})
    a3 = TestClient(rs.app, client=("203.0.113.72", 7003), headers={"x-test": "a"})
    nobody = TestClient(rs.app, client=("203.0.113.73", 7004))
    with a1.websocket_connect("/ws/events"), a2.websocket_connect("/ws/events"):
        with pytest.raises(WebSocketDisconnect) as refused:
            with a3.websocket_connect("/ws/events") as ws:       # a third address, same agent
                ws.receive_json()
        assert refused.value.code == 1013
        with nobody.websocket_connect("/ws/events"):             # another caller is not affected
            pass
    assert rs._WS_OPEN_AGENT.get("agent-a", 0) == 0
    with a3.websocket_connect("/ws/events"):                     # given back on close
        pass


def test_the_relay_as_a_whole_caps_websockets(monkeypatch):
    from emporia import relay_server as rs
    monkeypatch.setattr(rs, "_WS_TOTAL", 2)
    clients = [TestClient(rs.app, client=(f"203.0.113.{80 + i}", 8000 + i)) for i in range(3)]
    with clients[0].websocket_connect("/ws/events"), clients[1].websocket_connect("/ws/events"):
        assert rs._WS_OPEN_TOTAL[0] == 2
        with pytest.raises(WebSocketDisconnect) as refused:
            with clients[2].websocket_connect("/ws/events") as ws:
                ws.receive_json()
        assert refused.value.code == 1013
    assert rs._WS_OPEN_TOTAL[0] == 0
    with clients[2].websocket_connect("/ws/events"):
        pass


def test_the_documented_per_address_websocket_setting_is_the_one_read():
    src = (REPO / "src" / "emporia" / "relay_server.py").read_text()
    assert 'os.getenv("EMPORIA_MAX_WEBSOCKETS_PER_ADDRESS")' in src and "EMPORIA_EMPORIA" not in src


# ─── moving a relay's data ────────────────────────────────────────────────────

def _old_relay(tmp_path):
    keys = tmp_path / "keys"
    keys.mkdir()
    (keys / "_relay_identity.priv").write_bytes(b"k" * 32)
    (keys / "_relay_id.txt").write_text("relay-abc")
    (keys / "relay_operator.secp256k1").write_bytes(b"o" * 32)
    (keys / "agent-one.priv").write_bytes(b"a" * 32)               # an agent's key shares the folder
    db = tmp_path / "emporia.sqlite3"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE t (x TEXT)")
    conn.execute("INSERT INTO t VALUES ('kept')")
    conn.commit()
    conn.close()
    return db, keys


def test_a_relay_moves_with_its_identity_and_without_any_agents_keys(tmp_path):
    db, keys = _old_relay(tmp_path)
    out = tmp_path / "move"
    relay_move.export(db, keys, out)
    assert sorted(p.name for p in (out / "keys").iterdir()) == ["_relay_id.txt", "_relay_identity.priv",
                                                                "relay_operator.secp256k1"]
    new_db, new_keys = tmp_path / "new" / "emporia.sqlite3", tmp_path / "new" / "keys"
    relay_move.import_(out, new_db, new_keys)
    assert sqlite3.connect(new_db).execute("SELECT x FROM t").fetchone() == ("kept",)
    assert (new_keys / "_relay_identity.priv").read_bytes() == b"k" * 32
    assert oct((new_keys / "_relay_identity.priv").stat().st_mode & 0o777) == "0o600"
    assert not (new_keys / "agent-one.priv").exists()
    with pytest.raises(SystemExit, match="already exist"):         # a relay that has run is not overwritten
        relay_move.import_(out, new_db, new_keys)
    relay_move.import_(out, new_db, new_keys, force=True)


def test_an_export_without_the_relay_identity_is_refused(tmp_path):
    db, keys = _old_relay(tmp_path)
    (keys / "_relay_identity.priv").unlink()
    with pytest.raises(SystemExit, match="identity"):
        relay_move.export(db, keys, tmp_path / "move")
