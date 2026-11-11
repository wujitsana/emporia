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
INSTALL = "/home/op/.emporia"
REPO = Path(__file__).resolve().parent.parent


def snippet(door: str) -> dict:
    return yaml.safe_load(snippet_text(door))


def snippet_text(door: str) -> str:
    """The staged snippet with the install step's substitution done, as the host's compose reads it."""
    staged = relay_host.snippet_yaml(door, relay_host.DIR_PLACEHOLDER, relay_host.REPO_URL, SHA, "relay.example.com")
    return staged.replace(relay_host.DIR_PLACEHOLDER, INSTALL)


# ─── the snippet ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("door", ["tunnel", "caddy"])
def test_the_relay_is_hardened_and_published_on_no_port(door):
    relay = snippet(door)["services"]["emporia-relay"]
    assert relay["build"] == f"{relay_host.REPO_URL}#{SHA}" and len(SHA) == 40
    assert relay["user"] == "10001:10001"
    assert relay["read_only"] is True and relay["tmpfs"] == ["/tmp"]
    assert relay["cap_drop"] == ["ALL"] and "cap_add" not in relay
    assert relay["security_opt"] == ["no-new-privileges:true"]
    assert relay["mem_limit"] == "1536m" and relay["cpus"] == 2.0 and relay["pids_limit"]
    assert relay["container_name"] == "emporia-relay"
    assert relay["healthcheck"]["test"][-1].count("/health") == 1
    assert "ports" not in relay and relay["expose"] == ["8088"]          # only the compose network reaches it
    assert relay["networks"] == ["emporia-edge"]
    assert relay["logging"]["options"]["max-size"]
    mounts = relay["volumes"]
    assert not any("docker.sock" in m for m in mounts)
    assert any(m.endswith(":/app/relay.env:ro") for m in mounts) and any(m.endswith(":/run/secrets:ro") for m in mounts)
    assert f"{INSTALL}/data:/data" in mounts                              # identity, operator key, database
    assert "emporia-data" not in (snippet(door).get("volumes") or {})
    assert "FORWARDED_ALLOW_IPS=*" not in relay["environment"]


def test_the_tunnel_publishes_nothing_and_the_relay_believes_only_its_address():
    spec = snippet("tunnel")
    door = spec["services"]["emporia-cloudflared"]
    assert "ports" not in door and "emporia-caddy" not in spec["services"]
    assert ":" in door["image"] and "latest" not in door["image"]
    assert door["container_name"] == "emporia-cloudflared"
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


@pytest.mark.parametrize("door", ["tunnel", "caddy"])
def test_the_snippet_has_no_home_variable_and_no_hermes_path(door):
    text = snippet_text(door)
    staged = relay_host.snippet_yaml(door, relay_host.DIR_PLACEHOLDER, relay_host.REPO_URL, SHA, "relay.example.com")
    for t in (text, staged):
        assert "${HOME}" not in t and "$HOME" not in t and ".hermes" not in t and "~/" not in t
        assert "sudo HOME" not in t


@pytest.mark.parametrize("door", ["tunnel", "caddy"])
def test_every_mount_is_under_the_install_folder(door):
    spec = snippet(door)
    mounts = [m for svc in spec["services"].values() for m in svc.get("volumes", [])]
    host = [m.split(":")[0] for m in mounts]
    named = {"emporia-caddy-data"}
    assert host and all(h.startswith(INSTALL + "/") or h in named for h in host)
    wanted = {f"{INSTALL}/.env", f"{INSTALL}/secrets", f"{INSTALL}/data",
              f"{INSTALL}/cloudflared" if door == "tunnel" else f"{INSTALL}/Caddyfile"}
    assert wanted <= set(host)
    assert mounts.count(f"{INSTALL}/.env:/app/relay.env:ro") == 1


def test_the_host_sees_the_folder_through_the_data_volume():
    assert relay_host.host_path(Path("/opt/data/profiles/x/emporia-relay")) == "${HOME}/.hermes/profiles/x/emporia-relay"
    assert relay_host.host_path(Path("/opt/data")) == "${HOME}/.hermes"
    assert relay_host.host_path(Path("/srv/relay"), host_data="/h") == "/srv/relay"


STAGING = "$HOME/.hermes/profiles/x/emporia-relay"


def steps_of(door="tunnel", host_dir=relay_host.INSTALL_DIR, needs_key=False, secrets=()):
    return "\n".join(relay_host.into_compose_steps(door, host_dir, "relay.example.com", needs_key, list(secrets),
                                                   STAGING))


def test_the_steps_name_the_commands_the_person_runs():
    steps = steps_of("tunnel", needs_key=True, secrets=["money"])
    assert "Tunnels" in steps and "http://emporia-relay:8088" in steps and "WebSockets" in steps
    assert "operator_signer generate" in steps and "chown -R 10001" in steps
    assert "starts eyJ" in steps and "Ctrl-D" in steps
    assert "sudo docker compose up -d --build emporia-relay emporia-cloudflared" in steps
    assert "sudo HOME=$HOME docker compose up" not in steps and "sudo HOME=\"$HOME\" docker compose up" not in \
        [ln.strip() for ln in steps.splitlines() if ln.strip().startswith("sudo")]
    caddy = steps_of("caddy")
    assert "ufw allow 22,80,443/tcp" in caddy and "Cloudflare" not in caddy
    assert "EMPORIA_CLIENT_IP_HEADER" in caddy and "EMPORIA_TRUSTED_PROXIES" in caddy
    assert "sudo install -o root -g root -m 644 \"$S/Caddyfile\" \"$D/Caddyfile\"" in caddy


def test_the_first_step_fixes_the_folders_once_and_names_a_one_terminal_rule():
    first = relay_host.into_compose_steps("tunnel", relay_host.INSTALL_DIR, "relay.example.com", False, [],
                                          STAGING)[0]
    assert 'D="$HOME/.emporia"' in first and f'S="{STAGING}"' in first
    assert "not inside `sudo -i`" in first and "same" not in first.lower() or "this one terminal" in first
    custom = relay_host.into_compose_steps("tunnel", "/srv/er", "relay.example.com", False, [], STAGING)[0]
    assert 'D="/srv/er"' in custom


def test_the_steps_install_with_owners_and_modes_then_remove_the_staged_copies():
    lines = [ln.strip() for ln in steps_of("tunnel").splitlines()]
    assert 'sudo install -d -o root -g root -m 755 "$D"' in lines
    assert any(ln.startswith('sed "s#__EMPORIA_DIR__#$D#g" "$S/compose.snippet.yaml" | sudo tee "$D/compose.snippet.yaml"')
               and ln.endswith('sudo chmod 644 "$D/compose.snippet.yaml"') for ln in lines)
    assert 'sudo install -o root -g 10001 -m 640 "$S/.env" "$D/.env"' in lines
    assert 'sudo install -d -o 10001 -g 10001 -m 700 "$D/secrets" "$D/data"' in lines
    assert any("-m 600" in ln and '"$S"/secrets/*' in ln and '"$D/secrets/"' in ln for ln in lines)
    assert 'sudo rm -rf "$S/.env" "$S/compose.snippet.yaml" "$S/secrets"' in lines
    assert 'sudo rm -rf "$S/.env" "$S/compose.snippet.yaml" "$S/secrets" "$S/Caddyfile"' in \
        [ln.strip() for ln in steps_of("caddy").splitlines()]
    # install comes before removal
    text = steps_of("tunnel")
    assert text.index('-m 640 "$S/.env"') < text.index("sudo rm -rf")


def test_the_token_goes_straight_to_the_install_folder():
    steps = steps_of("tunnel")
    assert 'sudo install -d -o 65532 -g 65532 -m 700 "$D/cloudflared"' in steps
    assert 'sudo install -m 400 -o 65532 -g 65532 /dev/stdin "$D/cloudflared/token"' in steps
    token_lines = [ln for ln in steps.splitlines() if "token" in ln and "sudo" in ln]
    assert token_lines and not any("$S" in ln or ".hermes" in ln for ln in token_lines)


def test_every_path_in_the_printed_commands_starts_at_D_or_S_or_is_absolute():
    import re
    for door in ("tunnel", "caddy"):
        for step in relay_host.into_compose_steps(door, relay_host.INSTALL_DIR, "relay.example.com", True, ["k"], STAGING)[1:]:
            for ln in step.splitlines():
                ln = ln.strip()
                if ln.startswith("sudo") or ln.startswith("sed") or ln.startswith("for "):
                    assert "${HOME}" not in ln and not re.search(r"(^|\s)~/", ln)
                    assert "$HOME" not in ln


def test_the_include_step_prints_an_absolute_path_command():
    steps = steps_of("tunnel")
    assert 'echo "include: [ $D/compose.snippet.yaml ]"' in steps
    assert "uncomment" not in steps


def test_the_steps_say_how_to_change_settings_later():
    steps = steps_of("tunnel")
    assert 'sudo nano "$D/.env"' in steps and "sudo docker compose restart emporia-relay" in steps
    assert "relay config set" in steps and "--dir" in steps


def test_into_compose_writes_only_the_staged_snippet(tmp_path):
    written = relay_host.write_into_compose_files(tmp_path / "er", "tunnel", relay_host.DIR_PLACEHOLDER,
                                                  relay_host.REPO_URL, SHA, "relay.example.com")
    assert [p.name for p in written] == [relay_host.SNIPPET]
    assert not (tmp_path / "er" / "secrets").exists() and not (tmp_path / "er" / "cloudflared").exists()
    caddy = relay_host.write_into_compose_files(tmp_path / "c", "caddy", relay_host.DIR_PLACEHOLDER,
                                                relay_host.REPO_URL, SHA, "relay.example.com")
    assert sorted(p.name for p in caddy) == ["Caddyfile", relay_host.SNIPPET]
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
                          ("EMPORIA_IP_RATE_AUTH_PER_MINUTE", "30"), ("EMPORIA_MAX_WEBSOCKETS_PER_ADDRESS", "64"),
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


# ─── the install folder is outside the Hermes data folder ─────────────────────

@pytest.mark.parametrize("bad", ["${HOME}/.hermes/emporia-relay", "/home/me/.hermes/profiles/x/emporia-relay",
                                 "/home/me/.hermes", "/opt/data/emporia-relay", "emporia-relay", "~/relay", "$HOME/other"])
def test_an_install_folder_inside_hermes_or_not_absolute_is_refused(bad):
    assert relay_host.install_dir_problem(bad)


@pytest.mark.parametrize("good", [relay_host.INSTALL_DIR, "/srv/emporia-relay", "/srv/other", "/home/me/emporia-relay", "/opt/emporia"])
def test_an_absolute_folder_outside_hermes_is_accepted(good):
    assert relay_host.install_dir_problem(good) == ""


FEE = "0x" + "ab" * 20


def run_wizard(monkeypatch, folder, *extra):
    from emporia import cli
    monkeypatch.setattr(cli, "_persons_terminal", lambda: True)
    monkeypatch.setattr(cli, "_TTY", False)
    monkeypatch.setattr("shutil.which", lambda _: None)
    return cli.main(["relay", "host", "--into-compose", "--dir", str(folder), "--domain", "relay.example.org",
                     "--admin", "", "--access", "open", "--transparency-log", "off", "--modules", "money",
                     "--fee-receiver", FEE, *extra])


def test_the_wizard_refuses_a_host_dir_inside_hermes(tmp_path, capsys, monkeypatch):
    rc = run_wizard(monkeypatch, tmp_path / "er", "--front-door", "tunnel",
                    "--host-dir", "/home/me/.hermes/profiles/x/emporia-relay")
    out = capsys.readouterr().out
    assert rc == 1 and "inside the Hermes data folder" in out and "$HOME/.emporia" in out
    assert not (tmp_path / "er").exists()


def test_the_wizard_stages_new_files_and_prints_install_steps(tmp_path, capsys, monkeypatch):
    assert run_wizard(monkeypatch, tmp_path / "er", "--front-door", "tunnel") == 0
    out = capsys.readouterr().out
    assert 'D="$HOME/.emporia"' in out and "sed \"s#__EMPORIA_DIR__#$D#g\"" in out
    assert sorted(p.name for p in (tmp_path / "er").iterdir()) == [".env", relay_host.SNIPPET]
    assert oct((tmp_path / "er" / ".env").stat().st_mode & 0o777) == "0o600"


# ─── moving an existing relay out of the Hermes folder ────────────────────────

OLD_SHA = "fedcba9876543210fedcba9876543210fedcba98"
OLD_URL = "https://github.com/example/emporia.git"


def old_snippet(door: str = "tunnel") -> str:
    text = relay_host.snippet_yaml(door, "${HOME}/.hermes/profiles/x/emporia-relay", OLD_URL, OLD_SHA,
                                   "relay.example.org")
    text = text.replace("      - ${HOME}/.hermes/profiles/x/emporia-relay/data:/data", "      - emporia-data:/data")
    text = text.replace("networks:\n  emporia-edge:\n    driver", "volumes:\n  emporia-data:\n\nnetworks:\n  emporia-edge:\n    driver")
    text = text.replace("container_name: emporia-relay", "container_name: my-relay").replace("mem_limit: 1536m", "mem_limit: 3g")
    return text


def make_old_relay(folder: Path, door: str = "tunnel") -> None:
    folder.mkdir(parents=True)
    (folder / relay_host.SNIPPET).write_text(old_snippet(door))
    (folder / ".env").write_text("EMPORIA_RELAY_ACCESS=open\n")
    (folder / "secrets").mkdir()
    (folder / "secrets" / "operator_key").write_text("SECRET-KEY-VALUE")
    if door == "tunnel":
        (folder / "cloudflared").mkdir()
        (folder / "cloudflared" / "token").write_text("eyJSECRET-TOKEN")
    else:
        (folder / "Caddyfile").write_text(relay_host.into_compose_caddyfile("relay.example.org"))


def test_an_old_snippet_is_recognised_and_a_new_one_is_not():
    old = relay_host.old_layout(old_snippet("tunnel"))
    assert old["sha"] == OLD_SHA and old["url"] == OLD_URL and old["front_door"] == "tunnel"
    assert old["domain"] == "relay.example.org" and old["keep"]["emporia-relay"]["container_name"] == "my-relay"
    assert relay_host.old_layout(snippet_text("tunnel")) is None
    assert relay_host.old_layout("services: {}") is None


@pytest.mark.parametrize("door", ["tunnel", "caddy"])
def test_move_out_prints_the_move_steps_and_keeps_the_old_pin_names_and_limits(tmp_path, capsys, monkeypatch, door):
    import yaml
    folder = tmp_path / "old"
    make_old_relay(folder, door)
    from emporia import cli
    monkeypatch.setattr(cli, "_persons_terminal", lambda: True)
    monkeypatch.setattr(cli, "_TTY", False)
    assert cli.main(["relay", "host", "--move-out", "--dir", str(folder)]) == 0
    out = capsys.readouterr().out
    assert "SECRET-KEY-VALUE" not in out and "eyJSECRET-TOKEN" not in out
    for needle in ("Moving this relay out of the Hermes folder", "docker volume ls | grep emporia-data",
                   "docker run --rm -v <project>_emporia-data:/from", "sudo docker compose stop emporia-relay",
                   "same relay_id", "docker volume rm <project>_emporia-data", 'D="$HOME/.emporia"',
                   'echo "include: [ $D/compose.snippet.yaml ]"'):
        assert needle in out
    assert "Do these on the host" in out and "Cloudflare: Zero Trust" not in out
    staged = folder / relay_host.STAGED_SNIPPET
    spec = yaml.safe_load(staged.read_text().replace(relay_host.DIR_PLACEHOLDER, INSTALL))
    relay = spec["services"]["emporia-relay"]
    assert relay["build"] == f"{OLD_URL}#{OLD_SHA}" and relay["container_name"] == "my-relay"
    assert relay["mem_limit"] == "3g" and f"{INSTALL}/data:/data" in relay["volumes"]
    assert "emporia-data" not in (spec.get("volumes") or {})
    assert (folder / relay_host.SNIPPET).read_text() == old_snippet(door)       # the live include is untouched
    assert (folder / "secrets" / "operator_key").read_text() == "SECRET-KEY-VALUE"
    if door == "tunnel":
        assert 'sudo cp -a "$S/cloudflared" "$D/"' in out and "sudo chmod 400" in out


def test_the_wizard_on_an_old_relay_prints_move_steps_not_fresh_install_steps(tmp_path, capsys, monkeypatch):
    folder = tmp_path / "old"
    make_old_relay(folder)
    assert run_wizard(monkeypatch, folder) == 0
    out = capsys.readouterr().out
    assert "Found a relay with the old layout" in out and "Moving this relay out of the Hermes folder" in out
    assert "Save the token on the host" not in out and "SECRET-KEY-VALUE" not in out and "eyJSECRET" not in out
    assert f"{OLD_SHA}" in (folder / relay_host.STAGED_SNIPPET).read_text()


def test_move_out_refuses_a_folder_without_an_old_relay(tmp_path, capsys, monkeypatch):
    from emporia import cli
    monkeypatch.setattr(cli, "_persons_terminal", lambda: True)
    (tmp_path / "x").mkdir()
    assert cli.main(["relay", "host", "--move-out", "--dir", str(tmp_path / "x")]) == 1
    assert "no relay with the old layout" in capsys.readouterr().out
