"""P167: the Hermes plugin keeps the agent's own tools away from Emporia's keys."""
import importlib.util
from pathlib import Path

import pytest

_spec = importlib.util.spec_from_file_location(
    "emporia_key_guard", Path(__file__).resolve().parents[1] / "hermes_plugin" / "emporia" / "key_guard.py")
guard = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(guard)


@pytest.mark.parametrize("tool,args", [
    ("terminal", {"command": "cat ~/.hermes/keys/alpha.priv | xxd"}),
    ("terminal", {"command": "ls /opt/data/profiles/alpha/home/.hermes/keys"}),
    ("terminal", {"command": "printenv EMPORIA_X402_PHRASE_SECRET"}),
    ("terminal", {"command": "grep -r EMPORIA_IDENTITY_PRIVATE_KEY ."}),
    ("terminal", {"command": "echo $OP_SERVICE_ACCOUNT_TOKEN"}),
    ("read_file", {"path": "/opt/data/profiles/alpha/home/.hermes/keys/alpha.x402.secp256k1"}),
    ("read_file", {"path": "/opt/data/profiles/alpha/.env-backups/20261025T120000/.env"}),
    ("execute_code", {"code": "open('alpha.x25519.priv','rb').read()"}),
    ("search_files", {"pattern": "x402.phrase", "path": "/opt/data/profiles"}),
])
def test_reading_keys_is_blocked(tool, args):
    out = guard.pre_tool_call(tool_name=tool, args=args)
    assert out and out["action"] == "block" and "never read" in out["message"]


@pytest.mark.parametrize("tool,args", [
    ("terminal", {"command": "git status && pytest -q"}),
    ("read_file", {"path": "/opt/data/profiles/hackathon_hermes/emporia/README.md"}),
    # Writing Emporia's own source that mentions key files stays possible.
    ("write_file", {"path": "src/emporia/agent_wallet.py", "content": "return path.with_name('a.x402.phrase')"}),
    ("terminal", {"command": "emporia wallet show alpha"}),
])
def test_ordinary_work_passes(tool, args):
    assert guard.pre_tool_call(tool_name=tool, args=args) is None


def test_a_key_value_that_reaches_a_result_is_hidden():
    out = guard.transform_tool_result(result="ok\nEMPORIA_X402_EVM_PRIVATE_KEY=" + "ab" * 32 + "\nMORE=1\n")
    assert "ab" * 32 not in out and "EMPORIA_X402_EVM_PRIVATE_KEY=[hidden by Emporia]" in out and "MORE=1" in out
    phrase = "EMPORIA_X402_PHRASE_SECRET=test test test test test test test test test test test junk"
    assert "junk" not in guard.transform_tool_result(result=phrase)
    assert guard.transform_tool_result(result="nothing secret") is None


# -- P170: the emporia command, run by an agent --------------------------------

@pytest.mark.parametrize("command", [
    "emporia role alpha both --yes",
    ".venv/bin/emporia setup agent gamma --use-found-keys copy",
    "emporia wallet limits alpha --max-daily-cents 999999",
    "emporia identity store alpha --to 1password",
    "emporia wallet import alpha --link env:X",
    "emporia setup stripe --stripe-key-env K",
])
def test_commands_that_change_keys_wallets_limits_or_roles_need_the_operator(command):
    out = guard.pre_tool_call(tool_name="terminal", args={"command": command})
    assert out and out["action"] == "approve", "Hermes asks the human; the agent cannot answer"


@pytest.mark.parametrize("command", [
    "emporia doctor", "emporia setup", "emporia wallet show alpha", "emporia role alpha",
    "emporia identity show alpha",
])
def test_reading_with_the_emporia_command_stays_free(command):
    assert guard.pre_tool_call(tool_name="terminal", args={"command": command}) is None


def test_a_remote_vault_token_is_never_read():
    for name in ("VAULT_TOKEN", "DOPPLER_TOKEN", "INFISICAL_TOKEN"):
        assert guard.pre_tool_call(tool_name="terminal", args={"command": f"echo ${name}"})["action"] == "block"


# -- P173: review fixes ------------------------------------------------------------

@pytest.mark.parametrize("command", [
    "emporia role --yes alpha both",
    "emporia wallet --max-daily-cents 999999 limits alpha",
    "emporia identity --to env store alpha",
    "python -m emporia.cli role alpha both",
    ".venv/bin/python src/emporia/cli.py wallet import alpha",
])
def test_reordered_or_indirect_commands_still_need_the_operator(command):
    out = guard.pre_tool_call(tool_name="terminal", args={"command": command})
    assert out and out["action"] == "approve"


def test_a_code_tool_running_the_command_needs_the_operator_too():
    code = "import subprocess; subprocess.run(['emporia', 'role', 'alpha', 'both', '--yes'])"
    assert guard.pre_tool_call(tool_name="execute_code", args={"code": code})["action"] == "approve"


def test_always_covers_one_exact_command():
    a = guard.pre_tool_call(tool_name="terminal", args={"command": "emporia wallet register alpha"})
    b = guard.pre_tool_call(tool_name="terminal", args={"command": "emporia role alpha both"})
    assert a["rule_key"] != b["rule_key"]


@pytest.mark.parametrize("printed", [
    "{'EMPORIA_X402_EVM_PRIVATE_KEY': '" + "ab" * 32 + "', 'HOME': '/x'}",
    '{"EMPORIA_X402_PHRASE_SECRET": "test test test test test test test test test test test junk"}',
    "VAULT_TOKEN=hvs." + "z" * 30,
])
def test_quoted_and_json_values_are_hidden(printed):
    out = guard.transform_tool_result(result=printed)
    assert out and "hidden by Emporia" in out
    assert "ab" * 32 not in out and "junk" not in out and "z" * 30 not in out


def test_only_a_person_runs_wallet_backup():
    for cmd in ("emporia wallet backup alpha", "python -m emporia.cli wallet alpha backup",
                "cd x && emporia   wallet backup beta"):
        r = guard.pre_tool_call("terminal", {"command": cmd})
        assert r and r["action"] == "block", cmd
    assert guard.pre_tool_call("terminal", {"command": "emporia wallet show alpha"}) is None
    r = guard.pre_tool_call("terminal", {"command": "emporia wallet mainnet alpha"})
    assert r and r["action"] == "block", "P177: real money is a person's decision"
    assert guard.pre_tool_call("terminal", {"command": "emporia relay use https://x alpha"})["action"] == "approve"



def test_the_signer_is_a_persons_alone():
    """P203: its keys, limits and approvals are what the agent must not reach."""
    for cmd in ("emporia signer approve int_ab12", "emporia signer policy alpha set EMPORIA_X402_MAX_DAILY_CENTS 9",
                "emporia signer add alpha", "cat ~/.emporia-signer/admin.token", "ls $EMPORIA_SIGNER_DIR",
                "echo $EMPORIA_SIGNER_TOKEN", "cat /opt/data/profiles/.signer/agents/alpha/policy.json"):
        r = guard.pre_tool_call("terminal", {"command": cmd})
        assert r and r["action"] == "block", cmd
    assert guard.pre_tool_call("terminal", {"command": "emporia signer status"}) is None


def test_a_shown_phrase_is_hidden_from_tool_results():
    out = ("setup…\n  ──── RECOVERY PHRASE ────\n    1. abandon      2. ability\n"
           "  ──── END OF RECOVERY PHRASE ────\nPress Enter")
    hidden = guard.transform_tool_result(out)
    assert "abandon" not in hidden and "Press Enter" in hidden and "setup…" in hidden
    cut = guard.transform_tool_result("──── RECOVERY PHRASE ────\n 1. abandon")
    assert "abandon" not in cut, "a cut-off block is hidden too"
    assert guard.transform_tool_result("nothing here") is None


@pytest.mark.parametrize("tool,args", [
    ("terminal", {"command": "emporia relay host --domain relay.example.com"}),
    ("terminal", {"command": "EMPORIA_RELAY_HOST=0.0.0.0 emporia relay start"}),
    ("terminal", {"command": "uvicorn emporia.relay_server:app --host 0.0.0.0 --port 8088"}),
    ("terminal", {"command": "cd ~/emporia-relay && docker compose up -d"}),
    ("terminal", {"command": "cat /opt/data/profiles/x/emporia-relay/.env"}),
    ("write_file", {"path": "/opt/data/profiles/x/.env", "content": "A=1\nEMPORIA_RELAY_HOST=0.0.0.0\n"}),
    ("patch", {"path": "/opt/data/profiles/x/emporia-relay/compose.yaml", "new_string": "ports: [8088:8088]"}),
])
def test_an_agent_cannot_open_the_relay_to_other_machines(tool, args):
    """P183: only a person opens a relay beyond this machine."""
    r = guard.pre_tool_call(tool, args)
    assert r and r["action"] == "block" and "relay" in r["message"]


@pytest.mark.parametrize("command", [
    "emporia relay start", "EMPORIA_RELAY_HOST=127.0.0.1 emporia relay start",
    "uvicorn emporia.relay_server:app --host 127.0.0.1",
    "grep -n 'os.getenv(\"EMPORIA_RELAY_HOST\", \"127.0.0.1\")' src/emporia/relay_server.py",
])
def test_a_relay_on_this_machine_only_is_fine(command):
    assert guard.pre_tool_call("terminal", {"command": command}) is None


def test_an_agent_cannot_start_the_relay_service():
    r = guard.pre_tool_call("terminal", {"command": "sudo systemctl enable --now emporia-relay"})
    assert r and r["action"] == "block"


def test_linking_relays_needs_the_operators_yes():
    for cmd in ("emporia relay link https://x.example", "emporia relay unlink https://x.example"):
        assert guard.pre_tool_call("terminal", {"command": cmd})["action"] == "approve"
    assert guard.pre_tool_call("terminal", {"command": "emporia relay links"}) is None


def test_an_agent_cannot_open_its_relay_or_add_itself_as_a_member():
    for args in ({"command": "echo EMPORIA_RELAY_ACCESS=open >> .env"},
                 {"path": "/opt/data/profiles/x/.env", "content": "EMPORIA_RELAY_MEMBERS=" + "ab" * 32},
                 {"command": "emporia relay access open"}):
        r = guard.pre_tool_call("terminal", args)
        assert r and r["action"] in ("block", "approve"), args


def test_where_an_agent_may_go_is_its_operators_call():
    assert guard.pre_tool_call("terminal", {"command": "emporia visits alpha --allow https://x.example"})["action"] == "approve"
    assert guard.pre_tool_call("terminal", {"command": "emporia visits alpha"}) is None
    r = guard.pre_tool_call("write_file", {"path": "/opt/data/profiles/alpha/.env",
                                           "content": "EMPORIA_VISIT_POLICY=linked\n"})
    assert r and r["action"] == "block"


@pytest.mark.parametrize("tool,args", [
    ("write_file", {"path": "/opt/data/profiles/alpha/config.yaml",
                    "content": "mcp_servers:\n  emporia:\n    env:\n      EMPORIA_RELAY_URL: https://evil.example\n"}),
    ("patch", {"path": "/opt/data/profiles/alpha/.env", "old_string": "x", "new_string": "EMPORIA_RELAY_URL=https://e.x"}),
    ("terminal", {"command": "sed -i 's#EMPORIA_RELAY_URL=.*#EMPORIA_RELAY_URL=https://e.x#' .env"}),
    ("terminal", {"command": "hermes config set mcp_servers.emporia.env.EMPORIA_RELAY_URL https://e.x"}),
    ("terminal", {"command": "cat ~/.hermes/keys/visits.json"}),
])
def test_an_agent_cannot_move_itself_to_another_relay(tool, args):
    """P188: home is the operator's choice; visits go through visit_relay, which asks."""
    r = guard.pre_tool_call(tool, args)
    assert r and r["action"] == "block"


def test_editing_emporia_code_that_names_the_relay_setting_is_fine():
    assert guard.pre_tool_call("patch", {"path": "src/emporia/cli.py",
                                         "new_string": 'os.getenv("EMPORIA_RELAY_URL", "")'}) is None


def test_the_signers_code_called_directly_is_blocked_too():
    """P206: run as the agents' user, a python one-liner could approve its own payment."""
    for cmd in ('python -c "from emporia import signer; signer.decide(\'int_1\', True)"',
                'python3 -c "import emporia.signer as s; s.save_policy(\'alpha\', {})"',
                "python -c 'from emporia import agent_wallet, signer'",
                "python -c 'from emporia.signer import decide'"):
        r = guard.pre_tool_call("terminal", {"command": cmd})
        assert r and r["action"] == "block", cmd
    for fine in ("python -c 'from emporia import networks'", "ruff check src/emporia/signer.py",
                 "pytest tests/test_signer.py -q"):
        assert guard.pre_tool_call("terminal", {"command": fine}) is None, fine   # its source is no secret
