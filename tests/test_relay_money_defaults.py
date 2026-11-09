"""A relay's money by default: x402 payments only, paid agent to agent through the PayAI
facilitator; the fee address is optional (blank: no fee); escrow, and the operator key
that captures or voids its holds, only on their own opt-in."""
import os
from unittest.mock import patch

import pytest

from emporia import cli, operator_signer, relay_host, setup_choices as sc

FEE = "0x" + "ab" * 20
OPERATOR = "0x" + "cd" * 20
PAYAI = "https://facilitator.payai.network"


def _env(folder):
    return cli.installer().harvest_env(folder)


@pytest.fixture
def relay_profile(tmp_path, monkeypatch):
    """A relay profile with no settings yet, and an operator key that is made only when asked for."""
    op = tmp_path / "op"
    op.mkdir()
    (op / ".env").write_text("")
    monkeypatch.setattr(cli, "this_profile", lambda: op)
    monkeypatch.setattr(cli, "health", lambda timeout=3.0, url="": None)
    made = []

    def no_key(network):
        raise RuntimeError("no operator key")
    monkeypatch.setattr(operator_signer, "load_operator_signer", no_key)
    monkeypatch.setattr(operator_signer, "generate_key", lambda: made.append(1) or OPERATOR)
    monkeypatch.delenv("EMPORIA_FEE_ADDRESS", raising=False)
    monkeypatch.setattr(cli, "_TTY", False)
    return op, made


def _typing(monkeypatch, *answers):
    it = iter(answers)
    monkeypatch.setattr(cli, "_TTY", True)
    monkeypatch.setattr(cli, "_menus", lambda: None)
    monkeypatch.setattr("builtins.input", lambda *_: next(it))


def test_the_preset_is_x402_only_through_payai():
    assert cli._BASE_SEPOLIA["EMPORIA_X402_FACILITATOR_URL"] == PAYAI
    assert not any(k.startswith("EMPORIA_ESCROW_") or k.startswith("EMPORIA_OPERATOR_") for k in cli._BASE_SEPOLIA)
    assert cli._BASE_SEPOLIA_ESCROW["EMPORIA_ESCROW_NETWORK"] == "eip155:84532"


def test_setup_relay_writes_no_escrow_and_makes_no_key_by_default(relay_profile):
    op, made = relay_profile
    with patch.dict(os.environ):
        assert cli.main(["setup", "relay", "--yes"]) == 0
    env = _env(op)
    assert env["EMPORIA_X402_ENABLED"] == "1" and env["EMPORIA_X402_FACILITATOR_URL"] == PAYAI
    assert not any(k.startswith("EMPORIA_ESCROW_") for k in env)
    assert "EMPORIA_OPERATOR_SIGNER" not in env and "EMPORIA_OPERATOR_ADDRESS" not in env and made == []
    assert "EMPORIA_FEE_ADDRESS" not in env, "no fee address: no fee"


def test_setup_relay_takes_a_fee_address_without_escrow(relay_profile):
    op, made = relay_profile
    with patch.dict(os.environ):
        assert cli.main(["setup", "relay", "--fee-receiver", FEE, "--yes"]) == 0
        assert cli.main(["setup", "relay", "--fee-receiver", "0x12", "--yes"]) == 1, "not an address"
    env = _env(op)
    assert env["EMPORIA_FEE_ADDRESS"] == FEE and "EMPORIA_ESCROW_NETWORK" not in env and made == []


def test_escrow_and_its_operator_key_only_with_the_opt_in(relay_profile, capsys):
    op, made = relay_profile
    with patch.dict(os.environ):
        assert cli.main(["setup", "relay", "--escrow", "--yes"]) == 1, "escrow needs a fee address"
        assert made == [] and "fee address" in capsys.readouterr().out
        assert cli.main(["setup", "relay", "--escrow", "--fee-receiver", FEE, "--yes"]) == 0
    env = _env(op)
    for k, v in cli._BASE_SEPOLIA_ESCROW.items():
        assert env[k] == v, k
    assert env["EMPORIA_OPERATOR_ADDRESS"] == OPERATOR and made == [1]
    assert "Fund the operator" in capsys.readouterr().out
    with patch.dict(os.environ):
        assert cli.main(["setup", "relay", "--fee-receiver", FEE, "--yes"]) == 0
    assert _env(op)["EMPORIA_ESCROW_NETWORK"] == "eip155:84532", "no flag, nobody asked: it stays as it was"
    with patch.dict(os.environ):
        assert cli.main(["setup", "relay", "--no-escrow", "--fee-receiver", FEE, "--yes"]) == 0
    assert _env(op)["EMPORIA_ESCROW_NETWORK"] == "", "--no-escrow turns it off"


def test_in_a_terminal_enter_means_no_fee_and_no_escrow(relay_profile, monkeypatch, capsys):
    op, made = relay_profile
    _typing(monkeypatch, "", "", "", "")      # networks: none · fee: none · escrow: no · write: yes
    with patch.dict(os.environ):
        assert cli.main(["setup", "relay"]) == 0
    out = capsys.readouterr().out
    assert "Should the relay take a fee on payments?" in out and "Hold purchases in escrow?" in out
    assert "no fee  (recommended)" in out and "no  (recommended)" in out
    assert "has a say in who gets paid" in out
    env = _env(op)
    assert env["EMPORIA_X402_ENABLED"] == "1" and "EMPORIA_FEE_ADDRESS" not in env
    assert "EMPORIA_ESCROW_NETWORK" not in env and made == []


def test_the_money_questions_recommend_no_fee_and_no_escrow():
    assert sc.default("relay_fee") == "none" and sc.default("relay_escrow") == "no"
    assert sc.default("relay_money") == "yes"
    assert "can capture or void a held payment" in sc.CHOICES["relay_escrow"].matters
    fee = dict(sc.CHOICES["relay_fee"].labels())
    assert "testnet" in fee["none"] and "separate payment" in fee["address"]


# -- a hosted relay's money module ---------------------------------------------

def test_a_blank_fee_address_keeps_money_on_with_no_fee(tmp_path, monkeypatch):
    folder = tmp_path / "hosted"
    folder.mkdir()
    (folder / ".env").write_text("")
    monkeypatch.setattr(cli, "health", lambda timeout=3.0, url="": None)
    _typing(monkeypatch, "", "")              # fee: none (Enter) · escrow: no (Enter)
    assert cli.main(["relay", "modules", "--dir", str(folder), "--modules", "money", "--yes"]) == 0
    env = _env(folder)
    assert env["EMPORIA_X402_ENABLED"] == "1" and env["EMPORIA_X402_FACILITATOR_URL"] == PAYAI
    assert "EMPORIA_FEE_ADDRESS" not in env and not any(k.startswith("EMPORIA_ESCROW_") for k in env)


def test_a_hosted_relays_escrow_is_its_own_flag(tmp_path, monkeypatch):
    folder = tmp_path / "hosted"
    folder.mkdir()
    (folder / ".env").write_text("")
    monkeypatch.setattr(cli, "health", lambda timeout=3.0, url="": None)
    monkeypatch.setattr(cli, "_TTY", False)
    assert cli.main(["relay", "modules", "--dir", str(folder), "--modules", "money", "--fee-receiver", FEE,
                     "--yes"]) == 0
    assert "EMPORIA_ESCROW_NETWORK" not in _env(folder)
    assert cli.main(["relay", "modules", "--dir", str(folder), "--modules", "money", "--escrow", "--yes"]) == 0
    env = _env(folder)
    assert env["EMPORIA_ESCROW_NETWORK"] == "eip155:84532" and env["EMPORIA_OPERATOR_SIGNER"] == "env"
    assert cli.main(["relay", "modules", "--dir", str(folder), "--modules", "money", "--no-escrow", "--yes"]) == 0
    assert _env(folder)["EMPORIA_ESCROW_NETWORK"] == ""


# -- relay host: the operator-key step follows escrow, not money -------------------

def _host(folder, monkeypatch, *extra, typed=()):
    monkeypatch.setattr("shutil.which", lambda _: None)            # no Docker here: the steps are shown
    _typing(monkeypatch, *typed)
    with patch.dict(os.environ):
        return cli.main(["relay", "host", "--run", "docker", "--dir", str(folder), "--domain", "relay.example.com",
                         "--admin", "", "--access", "open", "--transparency-log", "off", "--modules", "money",
                         "--fee-receiver", FEE, *extra])


def test_relay_host_shows_the_operator_key_step_only_with_escrow(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli, "health", lambda timeout=3.0, url="": None)
    assert _host(tmp_path / "plain", monkeypatch, typed=[""]) == 0      # escrow: Enter (no, recommended)
    out = capsys.readouterr().out
    assert "Hold purchases in escrow?" in out
    assert "operator_signer" not in out and "EMPORIA_OPERATOR_ADDRESS" not in out
    assert not any(k.startswith("EMPORIA_ESCROW_") for k in _env(tmp_path / "plain"))
    assert _host(tmp_path / "held", monkeypatch, "--escrow") == 0
    out = capsys.readouterr().out
    assert "operator_signer generate" in out and "EMPORIA_OPERATOR_ADDRESS" in out
    assert _env(tmp_path / "held")["EMPORIA_ESCROW_NETWORK"] == "eip155:84532"


def test_into_compose_steps_name_the_operator_key_only_when_it_is_needed():
    plain = "\n".join(relay_host.into_compose_steps("tunnel", "/srv/er", "relay.example.com", False, []))
    assert "operator_signer" not in plain and "secrets" not in plain
    held = "\n".join(relay_host.into_compose_steps("tunnel", "/srv/er", "relay.example.com", True, ["operator key"]))
    assert "operator_signer generate" in held
