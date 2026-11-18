"""No silent localhost:8088: with no relay configured the tools and the CLI refuse and name the setting."""
import pytest

from emporia import cli, mcp_server, relay_default, signing


def test_a_setting_that_names_nothing_is_not_a_relay(monkeypatch):
    monkeypatch.delenv(relay_default.DEV_FLAG, raising=False)
    for bad in (None, "", "  ", "${EMPORIA_RELAY_URL}"):
        assert relay_default.resolve(bad) == ""
    assert relay_default.resolve("https://relay.example") == "https://relay.example"
    monkeypatch.setenv(relay_default.DEV_FLAG, "1")
    assert relay_default.resolve("") == relay_default.LOCAL and relay_default.resolve("${X}") == relay_default.LOCAL
    assert relay_default.resolve("https://relay.example") == "https://relay.example"


def test_a_tool_with_no_relay_refuses_and_names_the_setting(monkeypatch):
    monkeypatch.setattr(mcp_server, "RELAY_URL", "")
    monkeypatch.setattr(mcp_server, "AGENT_ID", "someone")
    from emporia import mcp_surface
    wrapped = mcp_surface._shaped("whats_next", lambda: mcp_server._base())
    got = wrapped()
    assert got["success"] is False and "EMPORIA_RELAY_URL" in got["error"] and got["retryable"] is False
    assert "emporia agent refresh" in got["next_step"]
    with pytest.raises(relay_default.NoRelay):
        mcp_server._base()


def test_a_named_relay_is_used_as_before(monkeypatch):
    monkeypatch.setattr(mcp_server, "RELAY_URL", "https://relay.example")
    assert mcp_server._base() == "https://relay.example"


def test_the_cli_stops_with_a_plain_error_and_the_dev_setting_brings_the_default_back(monkeypatch):
    monkeypatch.delenv(relay_default.DEV_FLAG, raising=False)
    monkeypatch.setenv("EMPORIA_RELAY_URL", "")
    with pytest.raises(SystemExit, match="EMPORIA_RELAY_URL"):
        cli.relay_url()
    monkeypatch.setenv(relay_default.DEV_FLAG, "1")
    assert cli.relay_url() == "http://localhost:8088"
    monkeypatch.setenv("EMPORIA_RELAY_URL", "https://relay.example/")
    assert cli.relay_url() == "https://relay.example"


def test_signing_no_longer_guesses_the_home_relay(monkeypatch):
    monkeypatch.delenv(relay_default.DEV_FLAG, raising=False)

    class Ctx:
        env = {"EMPORIA_RELAY_URL": ""}
    assert signing._relay_of(Ctx) == ""
    Ctx.env = {"EMPORIA_RELAY_URL": "https://relay.example/"}
    assert signing._relay_of(Ctx) == "https://relay.example"
