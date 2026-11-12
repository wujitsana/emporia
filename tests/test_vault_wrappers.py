"""S4: a wrapped `cmd:` key source keeps its vault family; one that names no vault CLI gets no vault variable
(a person who needs them names the CLI). Every copy: emporia.agent_env, the plugin's setup_cli and: the bridge's adapter.py."""
import ast
import os
import re
from pathlib import Path

from emporia import agent_env

REPO = Path(__file__).resolve().parents[1]


def _adapter_vault_families():
    """The bridge's copy (adapter.py imports Hermes, so lift just the function and its tables)."""
    tree = ast.parse((REPO / "hermes_plugin" / "emporia" / "adapter.py").read_text())
    keep = [n for n in tree.body if (isinstance(n, ast.Assign) and getattr(n.targets[0], "id", "")
                                     in ("_VAULT_FAMILIES", "_SOURCE_VARS"))
            or (isinstance(n, ast.FunctionDef) and n.name == "_vault_families")]
    ns = {"os": os, "re": re}
    exec(compile(ast.Module(keep, []), "adapter", "exec"), ns)
    return ns["_vault_families"]


def test_wrapped_cmd_sources_keep_their_vault_family():
    fams = _adapter_vault_families()
    for src, want in (("cmd:op read op://V/i/f", ("OP_",)),
                      ("cmd:/usr/local/bin/op read op://V/i/f", ("OP_",)),
                      ("cmd:sh -c 'op read op://V/i/f'", ("OP_",)),
                      ("cmd:sh -c 'bw get password x'", ("BW_",)),
                      ("cmd:env X=1 vault kv get -field=k secret/x", ("VAULT_",))):
        vals = {"EMPORIA_IDENTITY_KEY_SOURCE": src}
        assert agent_env.vault_prefixes(vals) == want, src
        assert fams(vals) == want, src


def test_a_cmd_source_naming_no_vault_cli_gets_no_vault_variable():
    import importlib.util
    fams = _adapter_vault_families()
    spec = importlib.util.spec_from_file_location("sc_for_vault", REPO / "hermes_plugin" / "emporia" / "setup_cli.py")
    sc = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(sc)
    vals = {"EMPORIA_IDENTITY_KEY_SOURCE": "cmd:/home/u/bin/key.sh"}
    assert agent_env.vault_prefixes(vals) == () and fams(vals) == () and sc._vault_families(list(vals.values())) == ()
    env = agent_env.names({**vals, "OP_SERVICE_ACCOUNT_TOKEN": "t", "AWS_ACCESS_KEY_ID": "a"})
    assert "OP_SERVICE_ACCOUNT_TOKEN" not in env and "AWS_ACCESS_KEY_ID" not in env
    assert agent_env.vault_prefixes({"EMPORIA_IDENTITY_KEY_SOURCE": "env:X"}) == ()
    assert fams({"EMPORIA_IDENTITY_KEY_SOURCE": "env:X"}) == ()
