"""Catalog rule 11: the MCP server reads no .env; Hermes hands it every setting and key by
`${NAME}`. The list of names must therefore be complete: a name the code reads that is not
on it would silently not reach the server (a spending limit, say)."""
import re
from pathlib import Path

from emporia import agent_env

REPO = Path(__file__).resolve().parents[1]
_READ = re.compile(
    r'''(?:os\.)?getenv\(\s*["']([A-Z][A-Z0-9_]*)["']'''
    r'''|environ\[\s*["']([A-Z][A-Z0-9_]*)["']'''
    r'''|environ\.get\(\s*["']([A-Z][A-Z0-9_]*)["']'''
    r'''|\benv\.get\(\s*["']([A-Z][A-Z0-9_]*)["']'''
    r'''|\b_getenv\(\s*["']([A-Z][A-Z0-9_]*)["']'''
    r'''|environ\.(?:setdefault|pop)\(\s*["']([A-Z][A-Z0-9_]*)["']'''
    r'''|["']([A-Z][A-Z0-9_]*)["']\s+(?:not\s+)?in\s+os\.environ\b''')
_ENVF = re.compile(r'''\benv(?:_int|_float)?\(\s*["']([A-Z][A-Z0-9_]*)["']''')
# Process-wide, set by the OS, the terminal or Hermes itself; never a profile setting.
_GLOBAL = {"HERMES_HOME", "TERM", "NO_COLOR", "PATH", "HOME", "USER", "LANG", "TZ"}


# The bridge (a gateway-side process, not the MCP server) reads the wake mode and the allowlist; the
# setup text names the gateway's settings to say what they do. Neither is the MCP server's reading.
_GATEWAY_SIDE = {"wake.py", "setup_choices.py"}


def _named_in_source(skip: frozenset[str] = frozenset()) -> set[str]:
    names = set()
    for path in sorted((REPO / "src" / "emporia").rglob("*.py")):
        if path.name == "agent_env.py" or path.name in skip:
            continue
        text = path.read_text()
        names |= {next(g for g in m.groups() if g) for m in _READ.finditer(text)}
        names |= {"EMPORIA_" + m.group(1) for m in _ENVF.finditer(text)}
        names |= set(re.findall(r"\bEMPORIA_[A-Z0-9][A-Z0-9_]*[A-Z0-9]\b", text))
    return {n for n in names - _GLOBAL if not n.endswith("_")}


def test_every_name_the_runtime_reads_is_handed_to_its_mcp_server():
    missing = sorted(_named_in_source(frozenset(_GATEWAY_SIDE)) - agent_env.known())
    assert not missing, ("names the runtime reads that Hermes would not hand its MCP server; add them to "
                         f"agent_env.LITERAL_NAMES: {missing}")


def test_the_gateways_own_settings_are_not_the_servers_and_the_runtime_reads_none():
    """Found by the end-to-end run: setup writes the adapter's settings (its python, allowed
    senders) after the MCP entry, and doctor said the server was "not handed" them. They are
    the gateway's: off the server's list — safe only while no runtime code reads one."""
    assert not set(agent_env.GATEWAY_ONLY) & _named_in_source(frozenset(_GATEWAY_SIDE))
    values = {"EMPORIA_PYTHON": "/rt/.venv/bin/python", "EMPORIA_ALLOWED_USERS": "emporia:relay",
              "EMPORIA_X402_MAX_DAILY_CENTS": "300"}
    env = agent_env.mcp_env(values)
    assert not set(agent_env.GATEWAY_ONLY) & set(env)
    assert agent_env.unreferenced(values, {}) == ["EMPORIA_X402_MAX_DAILY_CENTS"]


def test_names_built_from_a_network_id_are_there_for_every_network():
    from emporia.networks import NETWORKS
    built = set(agent_env.network_names())
    for net in NETWORKS:
        suffix = re.sub(r"[:-]", "_", net.upper())
        assert {f"EMPORIA_X402_ASSET_{suffix}", f"EMPORIA_X402_EXTRA_{suffix}", f"EMPORIA_X402_RPC_{suffix}"} <= built


def test_the_references_cover_the_keys_the_profile_holds_and_its_linked_names():
    values = {"EMPORIA_IDENTITY_KEY_SOURCE": "env:MY_OWN_IDENTITY", "EMPORIA_SOMETHING_NEW": "1",
              "VAULT_ADDR": "https://v", "OPENROUTER_API_KEY": "not Emporia's"}
    env = agent_env.mcp_env(values)
    assert env["EMPORIA_NO_DOTENV"] == "1"
    for name in ("EMPORIA_IDENTITY_PRIVATE_KEY", "EMPORIA_X402_PHRASE_SECRET", "EMPORIA_SIGNER_TOKEN",
                 "EMPORIA_X402_MAX_PAYMENT_CENTS", "EMPORIA_X402_PAY_NETWORKS", "MY_OWN_IDENTITY",
                 "EMPORIA_SOMETHING_NEW", "VAULT_ADDR", "BW_SESSION"):
        assert env[name] == f"${{{name}}}", name
    assert "OPENROUTER_API_KEY" not in env, "only Emporia's settings and its key sources"
    for literal in agent_env.LITERAL_IN_ENTRY:
        assert literal == "EMPORIA_NO_DOTENV" or literal not in env


def test_doctor_names_a_setting_the_entry_does_not_hand_over(tmp_path):
    entry_env = agent_env.mcp_env({})
    assert agent_env.unreferenced({"EMPORIA_X402_MAX_PAYMENT_CENTS": "500"}, entry_env) == []
    assert agent_env.unreferenced({"EMPORIA_BRAND_NEW": "1"}, entry_env) == ["EMPORIA_BRAND_NEW"]
    linked = {"EMPORIA_IDENTITY_KEY_SOURCE": "env:LINKED", "LINKED": "x"}    # a key the person linked by name
    assert agent_env.unreferenced(linked, entry_env) == ["LINKED"]
    assert agent_env.unreferenced(linked, agent_env.mcp_env(linked)) == []


def test_an_old_mcp_entry_is_given_its_references(tmp_path):
    import yaml

    from emporia import cli
    profile = tmp_path / "profiles" / "zed"
    profile.mkdir(parents=True)
    (profile / "config.yaml").write_text(yaml.dump({"mcp_servers": {"emporia": {
        "command": "python", "env": {"EMPORIA_RELAY_URL": "http://r", "EMPORIA_DOTENV": str(profile / ".env")}}}}))
    (profile / ".env").write_text("EMPORIA_X402_MAX_PAYMENT_CENTS=500\n")
    added = cli.installer().refresh_mcp_references(profile)
    env = yaml.safe_load((profile / "config.yaml").read_text())["mcp_servers"]["emporia"]["env"]
    assert "EMPORIA_NO_DOTENV" in added and env["EMPORIA_NO_DOTENV"] == "1"
    assert env["EMPORIA_X402_MAX_PAYMENT_CENTS"] == "${EMPORIA_X402_MAX_PAYMENT_CENTS}"
    assert env["EMPORIA_RELAY_URL"] == "http://r", "a literal setting stays literal"
    assert cli.installer().refresh_mcp_references(profile) == []
    # What the server then runs with (as Hermes renders it): the value, from the profile.
    assert cli._mcp_env(profile)["EMPORIA_X402_MAX_PAYMENT_CENTS"] == "500"
    assert "EMPORIA_SIGNER_TOKEN" not in cli._mcp_env(profile), "an unset reference is dropped"
