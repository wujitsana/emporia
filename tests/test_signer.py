"""P203: wallet keys and limits in a process of their own."""
import json
import os
import re
import time
from unittest.mock import patch

import pytest
from eth_account import Account
from fastapi.testclient import TestClient

from emporia import agent_wallet, identity, signer, signer_client
from tests.test_cli import profiles  # noqa: F401  (fixture)
from tests.test_wallet_setup import BASE, _challenge

AGENT = "signed_agent"


def _challenge_for(amount_cents: int) -> dict:
    body = _challenge(BASE)
    body["x402"]["accepts"][0]["amount"] = str(amount_cents * 10_000)
    return body


@pytest.fixture
def held(tmp_path, monkeypatch):
    """A signer holding AGENT's key and limits; the agent's own process holds neither."""
    monkeypatch.setenv("EMPORIA_SIGNER_DIR", str(tmp_path / "signer"))
    account = Account.create()
    with agent_wallet.scope(agent_wallet.Scope(env={}, key_dir=signer.agent_dir(AGENT) / "keys")):
        path = agent_wallet.wallet_path(AGENT)
    path.parent.mkdir(parents=True)
    identity._write_secret_atomically(path, bytes(account.key))
    signer.save_policy(AGENT, {"EMPORIA_X402_PAY_NETWORKS": BASE, "EMPORIA_X402_MAX_PAYMENT_CENTS": "500",
                               "EMPORIA_X402_MAX_DAILY_CENTS": "2000", "EMPORIA_PAYMENT_APPROVAL_CENTS": "300"})
    token = signer.new_agent_token(AGENT)
    app = TestClient(signer.app, base_url="http://signer")
    monkeypatch.setattr(signer_client.Client, "_http", lambda self: app)
    signer_client._CACHE.clear()
    agent_side = {"EMPORIA_SIGNER_URL": "unix:///x/signer.sock", "EMPORIA_SIGNER_TOKEN": token,
                  "EMPORIA_AGENT_ID": AGENT,
                  # The agent raising its own limits, as one with a terminal could: ignored.
                  "EMPORIA_X402_MAX_PAYMENT_CENTS": "999999", "EMPORIA_PAYMENT_APPROVAL_CENTS": ""}
    with patch.object(identity, "KEY_DIR", tmp_path / "agent-keys"), patch.dict(os.environ, agent_side):
        yield account, app


def test_the_agent_pays_through_the_signer_holding_nothing_itself(held):
    account, _ = held
    assert agent_wallet.address(AGENT) == account.address
    assert agent_wallet.allowed_networks() == [BASE]
    assert agent_wallet.sign_exact(AGENT, _challenge_for(250))
    assert agent_wallet.spent_today_atomic(AGENT) == 250 * 10_000
    assert not (identity.KEY_DIR).exists()                            # no key on the agent's side
    with pytest.raises(agent_wallet.AgentWalletError, match="per-payment cap"):
        agent_wallet.sign_exact(AGENT, _challenge_for(600))           # its own 999999 does not count


def test_above_the_threshold_only_the_operator_approves_and_only_once(held):
    _, app = held
    with pytest.raises(agent_wallet.AgentWalletError, match="emporia signer approve int_") as e:
        agent_wallet.sign_exact(AGENT, _challenge_for(400))
    intent = re.search(r"int_[0-9a-f]+", str(e.value)).group()
    with pytest.raises(agent_wallet.AgentWalletError, match=intent):  # asking again: the same intent
        agent_wallet.sign_exact(AGENT, _challenge_for(400))
    token = os.environ["EMPORIA_SIGNER_TOKEN"]
    assert app.post(f"/admin/intents/{intent}/approve",
                    headers={"authorization": f"Bearer {token}"}).status_code == 401   # the agent's token: no
    admin = signer_client.Admin("unix:///x", signer.admin_token())
    assert admin.decide(intent, True)["status"] == "approved"
    assert agent_wallet.sign_exact(AGENT, _challenge_for(400))        # paid, once
    with pytest.raises(agent_wallet.AgentWalletError, match="approval"):
        agent_wallet.sign_exact(AGENT, _challenge_for(400))


def test_a_wrong_token_is_refused(held, monkeypatch):
    monkeypatch.setenv("EMPORIA_SIGNER_TOKEN", "not-the-token")
    signer_client._CACHE.clear()
    with pytest.raises(agent_wallet.AgentWalletError, match="wrong token"):
        agent_wallet.address(AGENT)


def test_the_signer_says_whether_it_runs_apart_from_the_agents(held):
    _, app = held
    from emporia.relay_host import in_container
    assert app.get("/health").json() == {"service": "emporia-signer", "uid": os.getuid(),
                                         "in_container": in_container()}


def test_a_tester_adds_an_agent_to_the_signer_and_removes_it(profiles, tmp_path, monkeypatch, capsys):  # noqa: F811
    """Optional on testnet: add, pay, and remove puts everything back."""
    from emporia import cli
    from tests.test_wallet_setup import _env_of
    root, _ = profiles
    with patch.dict(os.environ):
        assert cli.main(["setup", "agent", "taster", "--keys", "files", "--wallet", "--max-daily-cents", "700"]) == 0
    prof = root / "taster"
    before = _env_of(prof)
    monkeypatch.setattr(cli, "profiles_root", lambda: root)
    monkeypatch.setattr(cli, "_persons_terminal", lambda: True)
    monkeypatch.setenv("EMPORIA_SIGNER_DIR", str(tmp_path / "sig"))
    keys = sorted(p.name for p in (prof / "home" / ".hermes" / "keys").glob("taster.x402.*"))
    assert keys
    ledger = prof / "home" / ".hermes" / "keys" / "taster.x402.spend.json"
    ledger.write_text(json.dumps([[time.time(), 1_230_000]]))          # spent today, before the move
    assert cli.main(["signer", "add", "taster", "--url", "unix:///run/s.sock"]) == 0
    with agent_wallet.scope(signer._scope("taster")):
        assert agent_wallet.spent_today_atomic("taster") == 1_230_000     # P207: the daily cap carries over
    assert "destroyed" in capsys.readouterr().out
    after = _env_of(prof)
    assert after["EMPORIA_SIGNER_URL"] == "unix:///run/s.sock" and after["EMPORIA_SIGNER_TOKEN"]
    assert not list((prof / "home" / ".hermes" / "keys").glob("taster.x402.*"))       # out of the profile
    assert signer.policy("taster")["env"]["EMPORIA_X402_MAX_DAILY_CENTS"] == "700"      # its limits went too
    assert "token" not in capsys.readouterr().out.lower().replace("its own token", "")
    assert cli.main(["signer", "remove", "taster"]) == 0
    assert set(keys) <= {p.name for p in (prof / "home" / ".hermes" / "keys").glob("taster.x402.*")}
    back = _env_of(prof)
    assert "EMPORIA_SIGNER_URL" not in back and back.get("EMPORIA_X402_MAX_DAILY_CENTS") == before.get(
        "EMPORIA_X402_MAX_DAILY_CENTS")
    assert not signer.agent_dir("taster").exists()


def test_the_signer_menu_starts_it_and_adds_an_agent(profiles, tmp_path, monkeypatch):  # noqa: F811
    """`emporia signer` in a terminal: start it, add an agent — each by number."""
    from emporia import cli
    from tests.test_wallet_setup import _typing
    root, _ = profiles
    with patch.dict(os.environ):
        assert cli.main(["setup", "agent", "menuist", "--keys", "files", "--wallet"]) == 0
    monkeypatch.setattr(cli, "profiles_root", lambda: root)
    monkeypatch.setattr(cli, "_persons_terminal", lambda: True)
    monkeypatch.setattr(cli, "_TTY", True)
    monkeypatch.setenv("EMPORIA_SIGNER_DIR", str(tmp_path / "sig"))
    state = {"up": False, "calls": []}
    monkeypatch.setattr(cli, "_signer_up", lambda url: {"uid": os.getuid()} if state["up"] else None)

    def daemon(args, url):
        state["up"] = args.action == "start"
        state["calls"].append(args.action)
        return 0
    monkeypatch.setattr(cli, "_signer_daemon", daemon)
    # start · add · menuist · done (stop, add, remove, pending, audit, limits, done)
    _typing(monkeypatch, "1", "2", "1", "7")
    assert cli.main(["signer"]) == 0
    assert state["calls"] == ["start"] and signer.policy("menuist")["token_sha256"]


def test_setup_offers_to_start_the_signer_and_keep_the_new_wallet_there(profiles, tmp_path, monkeypatch):  # noqa: F811
    from emporia import cli
    root, _ = profiles
    monkeypatch.setattr(cli, "profiles_root", lambda: root)
    monkeypatch.setattr(cli, "_persons_terminal", lambda: True)
    monkeypatch.setenv("EMPORIA_SIGNER_DIR", str(tmp_path / "sig"))
    state = {"up": False}
    monkeypatch.setattr(cli, "_signer_up", lambda url: {"uid": os.getuid()} if state["up"] else None)
    monkeypatch.setattr(cli, "_signer_daemon", lambda args, url: state.update(up=True) or 0)
    asked = []
    monkeypatch.setattr(cli, "ask", lambda q, default=False: asked.append((q, default)) or ("signer" in q or default))
    with patch.dict(os.environ):
        assert cli.main(["setup", "agent", "offered", "--keys", "files", "--wallet"]) == 0
    question = next((q, d) for q, d in asked if "signer" in q)
    assert "this starts the signer" in question[0] and question[1] is False      # optional: no by default
    assert state["up"] and signer.policy("offered")["token_sha256"]


def test_an_approval_is_for_that_payee_only_and_every_outcome_is_audited(held):
    """P206: an address sharing the approved one's first characters is not approved."""
    import json
    with pytest.raises(agent_wallet.AgentWalletError) as e:
        agent_wallet.sign_exact(AGENT, _challenge_for(400))
    intent = re.search(r"int_[0-9a-f]+", str(e.value)).group()
    signer_client.Admin("unix:///x", signer.admin_token()).decide(intent, True)
    lookalike = _challenge_for(400)
    lookalike["x402"]["accepts"][0]["payTo"] = "0x" + "11" * 10 + "99" * 10      # same first 12 characters
    with pytest.raises(agent_wallet.AgentWalletError, match="int_"):
        agent_wallet.sign_exact(AGENT, lookalike)
    assert agent_wallet.sign_exact(AGENT, _challenge_for(400))                    # the approved one pays
    with pytest.raises(agent_wallet.AgentWalletError, match="per-payment cap"):
        agent_wallet.sign_exact(AGENT, _challenge_for(600))
    rows = [json.loads(ln) for ln in (signer.signer_dir() / "audit.jsonl").read_text().splitlines()]
    assert [r["result"] for r in rows] == ["waiting", "waiting", "signed", "refused"]
    assert rows[2]["amount_atomic"] == 400 * 10_000 and oct((signer.signer_dir() / "audit.jsonl").stat().st_mode)[-3:] == "600"


def test_the_signer_takes_the_agents_network_settings(tmp_path, monkeypatch):
    monkeypatch.setenv("EMPORIA_SIGNER_DIR", str(tmp_path / "s"))
    monkeypatch.delenv("EMPORIA_X402_SOLANA_RPC_URL", raising=False)
    assert signer.add_settings({"EMPORIA_X402_SOLANA_RPC_URL": "https://rpc.example/key", "OPENROUTER_API_KEY": "x"}) \
        == ["EMPORIA_X402_SOLANA_RPC_URL"]
    assert signer.add_settings({"EMPORIA_X402_SOLANA_RPC_URL": "https://other.example"}) == []   # the first stays
    signer.load_settings()
    assert os.environ["EMPORIA_X402_SOLANA_RPC_URL"] == "https://rpc.example/key"
    assert oct(signer.settings_path().stat().st_mode)[-3:] == "600"
    monkeypatch.delenv("EMPORIA_X402_SOLANA_RPC_URL")


def test_real_money_needs_the_signer_to_run_as_another_user(profiles, tmp_path, monkeypatch, capsys):  # noqa: F811
    """P206: as the agents' user, its folder is in their reach — testnet only."""
    from emporia import cli
    root, _ = profiles
    with patch.dict(os.environ):
        assert cli.main(["setup", "agent", "richer", "--keys", "files", "--wallet"]) == 0
    monkeypatch.setattr(cli, "profiles_root", lambda: root)
    monkeypatch.setattr(cli, "_persons_terminal", lambda: True)
    monkeypatch.setenv("EMPORIA_SIGNER_DIR", str(tmp_path / "same-user"))
    assert cli.main(["signer", "add", "richer", "--url", "unix:///run/s.sock"]) == 0
    monkeypatch.delenv("EMPORIA_ALLOW_HOT_AGENT_WALLET", raising=False)
    assert cli.main(["signer", "policy", "richer", "mainnet"]) == 1
    assert "own user or container" in capsys.readouterr().out
    assert signer.policy("richer")["env"].get("EMPORIA_X402_MAINNET") != "1"


def test_an_agent_setting_up_a_wallet_is_not_offered_the_signer(profiles, monkeypatch, capsys):  # noqa: F811
    from emporia import cli
    monkeypatch.setattr(cli, "_persons_terminal", lambda: False)
    with patch.dict(os.environ):
        assert cli.main(["setup", "agent", "helped", "--keys", "files", "--wallet"]) == 0
    out = capsys.readouterr().out
    assert "the operator runs `emporia signer add helped`" in out
    assert not signer.agent_dir("helped").exists()


def test_signer_host_writes_its_own_container_or_a_service(profiles, tmp_path, monkeypatch, capsys):  # noqa: F811
    """P207: apart from the agents — a container beside Hermes's, or a system user."""
    import yaml

    from emporia import cli
    from emporia import relay_host as rh
    monkeypatch.setattr(cli, "_persons_terminal", lambda: True)
    monkeypatch.setattr(rh, "in_container", lambda: True)            # Hermes in Docker, no Docker here
    monkeypatch.setattr("shutil.which", lambda name: None)
    assert cli.main(["signer", "host", "--dir", str(tmp_path / "box")]) == 0
    compose = yaml.safe_load((tmp_path / "box" / "compose.yaml").read_text())
    signer_svc = compose["services"]["signer"]
    assert signer_svc["user"] == "10001" and "emporia-signer-socket:/run/emporia-signer" in signer_svc["volumes"]
    assert not any("/opt/data" in v for v in signer_svc["volumes"])          # Hermes's data never in the signer
    assert compose["volumes"]["emporia-signer-socket"]["name"] == "emporia-signer-socket"
    out = capsys.readouterr().out
    assert "external: true" in out and "docker compose run --rm admin add NAME" in out
    assert cli.main(["signer", "host", "--run", "service", "--dir", str(tmp_path / "box")]) == 1   # not in a container
    monkeypatch.setattr(rh, "in_container", lambda: False)
    assert cli.main(["signer", "host", "--run", "service", "--dir", str(tmp_path / "svc")]) == 0
    unit = (tmp_path / "svc" / "emporia-signer.service").read_text()
    assert "User=emporia-signer" in unit and "StateDirectoryMode=0700" in unit and "RuntimeDirectoryMode=0755" in unit
    assert "sudo env HERMES_HOME=" in capsys.readouterr().out


def test_a_developer_may_hold_a_wallet_behind_a_signer_running_apart(profiles, monkeypatch, capsys):  # noqa: F811
    from emporia import cli
    added = []
    monkeypatch.setattr(cli, "_persons_terminal", lambda: True)
    monkeypatch.setattr(cli, "_signer_apart", lambda: True)
    monkeypatch.setattr(cli, "_signer_add", lambda args, url: added.append(args.name) or 0)
    with patch.dict(os.environ):
        assert cli.main(["setup", "agent", "builder", "--role", "developer", "--keys", "files", "--wallet"]) == 0
    assert added == ["builder"] and "out of its tools' reach" in capsys.readouterr().out
    monkeypatch.setattr(cli, "_signer_apart", lambda: False)       # practice mode, or none: no wallet
    with patch.dict(os.environ):
        assert cli.main(["setup", "agent", "builder2", "--role", "developer", "--keys", "files", "--wallet"]) == 0
    assert added == ["builder"] and "Wallet flags ignored" in capsys.readouterr().out


def test_a_way_round_the_wall_means_it_is_not_apart(monkeypatch, capsys):
    """P207: Docker or passwordless sudo for the agents' user undoes any separation."""
    from emporia import cli
    monkeypatch.setattr(cli, "_signer_up", lambda url: {"uid": os.getuid() + 1})
    monkeypatch.setattr(cli, "_escalations", lambda: [])
    assert cli._signer_apart()
    monkeypatch.setattr(cli, "_escalations", lambda: ["this user can control Docker (/var/run/docker.sock)"])
    assert not cli._signer_apart()
    monkeypatch.setattr(signer_client.Client, "health", lambda self: {"uid": os.getuid() + 1})
    assert cli.main(["signer", "status", "--url", "unix:///x.sock"]) == 0
    assert "BUT this user can control Docker" in capsys.readouterr().out


def test_hermes_on_the_host_reaches_its_signer_container_on_a_loopback_port(tmp_path, monkeypatch, capsys):
    """P207: a host process cannot reach a socket in a Docker volume (Docker Desktop passes none)."""
    import yaml

    from emporia import cli
    from emporia import relay_host as rh
    monkeypatch.setattr(cli, "_persons_terminal", lambda: True)
    monkeypatch.setattr(rh, "in_container", lambda: False)
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/docker" if name == "docker" else None)
    monkeypatch.setattr(cli.subprocess, "run", lambda *a, **k: type("R", (), {"returncode": 1})())
    monkeypatch.setattr(cli, "_escalations", lambda: ["this user can control Docker (/var/run/docker.sock)"])
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    assert cli.main(["signer", "host", "--run", "docker", "--dir", str(tmp_path / "box")]) == 0
    compose = yaml.safe_load((tmp_path / "box" / "compose.yaml").read_text().replace("${HERMES_DATA}", "/h"))
    assert compose["services"]["signer"]["ports"] == ["127.0.0.1:8790:8790"]
    assert "/h:/h" in compose["services"]["admin"]["volumes"]                  # profiles' paths match
    assert f"HERMES_DATA={tmp_path / 'hermes'}" in (tmp_path / "box" / ".env").read_text()
    out = capsys.readouterr().out
    assert "--url http://127.0.0.1:8790" in out and "Not a wall yet" in out


def test_what_gets_round_a_separation_depends_on_the_os_and_the_container(monkeypatch):
    """P208: Docker access is root on Linux; on macOS Docker Desktop reaches only the user's files."""
    from emporia import cli
    monkeypatch.setattr(cli, "_escalations", lambda: ["this user can control Docker (/var/run/docker.sock)"])
    monkeypatch.setattr(cli.sys, "platform", "linux")
    assert cli._reachable_anyway(False) and cli._reachable_anyway(True)
    monkeypatch.setattr(cli.sys, "platform", "darwin")
    assert cli._reachable_anyway(False) == []          # a separate macOS account stays apart
    assert cli._reachable_anyway(True)                  # a container in Docker Desktop does not


def test_doctor_fails_a_live_relay_whose_keys_the_agents_user_could_reach(monkeypatch, capsys):
    from emporia import cli
    monkeypatch.setattr(cli, "_reachable_anyway", lambda in_container: ["this user has sudo without a password"])
    monkeypatch.setattr(cli, "health", lambda timeout=3.0, url="": {
        "version": "x", "live_money": ["a live Stripe key"], "in_container": True})
    monkeypatch.setattr(cli, "doctor_federation", lambda rep, h: None)
    rep = cli.Report()
    cli.doctor_relay(rep)
    assert rep.failed and "takes real money (a live Stripe key)" in capsys.readouterr().out
