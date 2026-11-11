"""Pre-release guards: the old-name commitment file is migrated only on the home relay, and the agent
child environment never carries a relay-only name, even through an `env:NAME` reference."""
import os

from emporia import cli


def test_old_name_commitment_file_is_not_adopted_by_a_visited_relay(tmp_path, monkeypatch):
    from emporia import identity, mcp_server
    monkeypatch.setattr(identity, "KEY_DIR", tmp_path)
    d = tmp_path / "commitments"
    d.mkdir()
    (d / "bob.sess_1.json").write_text('{"1": {"move": "rock"}}')
    (d / "bob.sess_1.rand.json").write_text('{"x": 1}')
    token = mcp_server._RELAY_CTX.set("https://visited.example")
    try:
        p = mcp_server._commitments_file("bob", "sess_1")
    finally:
        mcp_server._RELAY_CTX.reset(token)
    assert not p.exists() and (d / "bob.sess_1.json").exists() and (d / "bob.sess_1.rand.json").exists()
    home = mcp_server._commitments_file("bob", "sess_1")              # the home relay still migrates it
    assert home.exists() and not (d / "bob.sess_1.json").exists()


def test_old_name_commitment_file_is_not_adopted_by_a_non_home_sdk_relay(tmp_path, monkeypatch):
    from emporia import identity
    from emporia.agent_sdk import EmporiaAgent
    monkeypatch.setattr(identity, "KEY_DIR", tmp_path)
    monkeypatch.setenv("EMPORIA_RELAY_URL", "https://home.example")
    d = tmp_path / "commitments"
    d.mkdir()
    (d / "bob.sess_1.json").write_text('{"1": {"move": "rock"}}')
    a = EmporiaAgent.__new__(EmporiaAgent)
    a.agent_id, a.relay_url = "bob", "https://visited.example"
    assert a._read_commitments("sess_1") == {} and (d / "bob.sess_1.json").exists()
    a.relay_url = "https://home.example"
    assert a._read_commitments("sess_1")["1"]["move"] == "rock"


def test_as_agent_env_never_hands_a_relay_only_name_even_through_an_env_reference(tmp_path, monkeypatch):
    from test_signer_installer_fixes import _plain_profile
    monkeypatch.setattr(os, "getuid", lambda: 1000)
    prof = _plain_profile(tmp_path, {
        "EMPORIA_RELAY_URL": "http://r",
        "EMPORIA_IDENTITY_KEY_SOURCE": "env:EMPORIA_OPERATOR_PRIVATE_KEY",
        "EMPORIA_OPERATOR_PRIVATE_KEY": "relay-held",
        "EMPORIA_ENCRYPTION_KEY_SOURCE": "env:MY_ENC_KEY", "MY_ENC_KEY": "k"})
    env = cli._agent_child_env(prof, {})
    assert "EMPORIA_OPERATOR_PRIVATE_KEY" not in env and env["MY_ENC_KEY"] == "k"
