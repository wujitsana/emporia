"""Escrow is off in 2.1: the relay's operator key would decide who is paid at settlement, and the
relay never decides who gets paid. A relay ignores every EMPORIA_ESCROW_* setting (and starts
anyway, with one warning); a payer can still reclaim an older hold."""
import os
from unittest.mock import patch

import pytest

from emporia import cli, escrow_chain as ec, mcp_server, relay_modules as rm
from emporia import relay_server as rs

SETTINGS = {
    "EMPORIA_ESCROW_NETWORK": "eip155:84532",
    "EMPORIA_ESCROW_RPC_URL": "https://rpc.example.test",
    "EMPORIA_ESCROW_CONTRACT": "0xf96815976523E00e65Be8f34cA5e64b4f41EB19c",
    "EMPORIA_ESCROW_COLLECTOR": "0x8612dfdc421f80336cd14E8EF9cb1E765dB5ab88",
    "EMPORIA_FEE_ADDRESS": "0x8d96f65aA58421F976D83743B28dbf4929998209",
    "EMPORIA_X402_ASSET_EIP155_84532": "0x036CbD53842c5426634e7929541eC2318f3dCF7e",
    "EMPORIA_X402_EXTRA_EIP155_84532": '{"name": "USDC", "version": "2"}',
}


def test_the_switch_is_off_and_no_environment_turns_it_on():
    assert ec.RELAY_ESCROW_ENABLED is False
    with patch.dict(os.environ, SETTINGS):
        assert ec.load_config() is None
        with patch.object(ec, "RELAY_ESCROW_ENABLED", True):
            assert ec.load_config() is not None      # the tests' switch still exercises the code


def test_a_relay_with_escrow_settings_starts_and_warns_once(monkeypatch):
    for k, v in SETTINGS.items():
        monkeypatch.setenv(k, v)
    seen = []
    monkeypatch.setattr(rs.logger, "warning", lambda msg, *a, **k: seen.append(msg))
    rs._check_relay_modules()                         # does not refuse to start
    notes = [m for m in seen if "escrow is off in 2.1" in m]
    assert len(notes) == 1
    for name in ("EMPORIA_ESCROW_NETWORK", "EMPORIA_ESCROW_CONTRACT", "EMPORIA_ESCROW_COLLECTOR",
                 "EMPORIA_ESCROW_RPC_URL"):
        assert name in notes[0]
    assert "never decides who gets paid" in notes[0]


def test_no_warning_without_escrow_settings(monkeypatch):
    for k in ec.ESCROW_SETTINGS:
        monkeypatch.delenv(k, raising=False)
    assert ec.off_warning() is None


def test_health_and_the_card_do_not_advertise_escrow(client, monkeypatch):
    for k, v in SETTINGS.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("EMPORIA_X402_ENABLED", "1")
    monkeypatch.setenv("EMPORIA_X402_NETWORKS", "eip155:84532")
    monkeypatch.setenv("EMPORIA_X402_FACILITATOR_URL", "https://facilitator.example.test")
    assert client.get("/health").json()["x402_escrow_network"] is None
    assert "escrow" not in (rm.advertise().get("money") or {}).get("parts", [])
    assert "escrow" not in rm.chosen(SETTINGS).get("money", set())


def test_the_front_page_says_escrow_is_off(client, monkeypatch):
    monkeypatch.setattr(rs, "live_money", lambda: [])
    monkeypatch.setenv("EMPORIA_ESCROW_NETWORK", "eip155:84532")
    page = client.get("/").text
    assert "escrow is off here" in page and "escrow is on here" not in page and "{" not in page.split("<body>")[1]


def test_setup_relay_refuses_escrow_and_accepts_no_escrow(tmp_path, monkeypatch, capsys):
    op = tmp_path / "op"
    op.mkdir()
    (op / ".env").write_text("")
    monkeypatch.setattr(cli, "this_profile", lambda: op)
    monkeypatch.setattr(cli, "health", lambda timeout=3.0, url="": None)
    monkeypatch.setattr(cli, "_TTY", False)
    assert cli.main(["setup", "relay", "--escrow", "--yes"]) == 2
    out = capsys.readouterr().out
    assert "escrow is off in 2.1" in out and "never decides who gets paid" in out
    assert (op / ".env").read_text() == ""
    with patch.dict(os.environ):
        assert cli.main(["setup", "relay", "--no-escrow", "--yes"]) == 0


def test_reclaim_is_a_tool_whatever_the_relay_runs():
    """A relay that stopped running escrow must not hide the tool an older hold is reclaimed with."""
    for advertised in ([{}], [{"money": {"parts": ["x402"]}}], [{"log": {"parts": []}}]):
        assert "reclaim_escrow" not in rm.tools_to_hide(advertised)
    assert "reclaim_escrow" not in rm.all_tools()


def test_funding_against_a_relay_with_no_escrow_offer_is_refused_clearly(monkeypatch):
    class R:
        status_code = 402
        def json(self):
            return {"error": "payment_required", "x402": {}}
    monkeypatch.setattr(mcp_server, "_bearer_request", lambda *a, **k: R())
    monkeypatch.setattr(mcp_server, "_playbook_session_refusal", lambda *a, **k: None)
    out = mcp_server.fund_session("sess_x", agent_id="payer1", pay_with_x402=True)
    assert out["success"] is False and "escrow is off in 2.1" in out["error"]
    assert "reclaim_escrow" in out["error"] and "Nothing was signed" in out["error"]
