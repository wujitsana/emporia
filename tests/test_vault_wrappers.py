"""S4: a wrapped `cmd:` key source keeps its vault family; one that names no vault CLI gets every family
(as before SI-6). Both copies: emporia.agent_env and the bridge's hermes_plugin/emporia/adapter.py."""
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


def test_a_cmd_source_naming_no_vault_cli_gets_every_family():
    fams = _adapter_vault_families()
    every = tuple(f for v in agent_env.VAULT_FAMILIES.values() for f in v)
    vals = {"EMPORIA_IDENTITY_KEY_SOURCE": "cmd:/home/u/bin/key.sh"}
    assert agent_env.vault_prefixes(vals) == every and fams(vals) == every
    env = agent_env.names({**vals, "OP_SERVICE_ACCOUNT_TOKEN": "t", "AWS_ACCESS_KEY_ID": "a"})
    assert "OP_SERVICE_ACCOUNT_TOKEN" in env and "AWS_ACCESS_KEY_ID" in env
    assert agent_env.vault_prefixes({"EMPORIA_IDENTITY_KEY_SOURCE": "env:X"}) == ()
    assert fams({"EMPORIA_IDENTITY_KEY_SOURCE": "env:X"}) == ()
