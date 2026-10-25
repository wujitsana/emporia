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
