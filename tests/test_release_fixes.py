"""Small pre-release fixes: the MCP server's stderr, the stale 'not available yet' list, and the like."""

from __future__ import annotations

import pytest

from emporia import mcp_server


@pytest.mark.parametrize("transport", ["stdio", "http", "sse"])
def test_the_mcp_server_starts_without_a_banner_or_an_update_box(monkeypatch, transport):
    calls = []
    monkeypatch.setattr(mcp_server, "_auto_register", lambda: None)
    monkeypatch.setattr(mcp_server, "_fit_tools_to_relays", lambda: None)
    monkeypatch.setattr(mcp_server.mcp, "run", lambda **kw: calls.append(kw))
    monkeypatch.setenv("EMPORIA_MCP_TRANSPORT", transport)
    import fastmcp
    held = fastmcp.settings.check_for_updates
    try:
        mcp_server.main()
        assert fastmcp.settings.check_for_updates == "off"
    finally:
        fastmcp.settings.check_for_updates = held
    assert len(calls) == 1 and calls[0]["show_banner"] is False
