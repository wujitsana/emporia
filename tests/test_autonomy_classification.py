"""Every MCP tool is classified for unattended turns: budgeted, money, a game move, a setting, read-only, or an
explicitly exempt state change. A tool added later fails here until someone decides which."""

from __future__ import annotations

import json
import time

import pytest

from emporia import autonomy, features

READ_PREFIXES = ("get_", "list_", "read_", "my_", "whats_")
READ_ONLY = frozenset({
    "agent_card", "lab_threads", "model_results", "records", "money_info",
    "scan_relays", "standing", "supported_games", "validate_turn", "verify",
})
#: State changes left out of the budget on purpose, each with the reason.
EXEMPT = {
    "register_agent": "idempotent identity registration the agent does on every start",
    "x_claim_line": "signs a line locally; nothing is sent or posted",
    "github_claim_line": "signs a line locally; nothing is sent or posted",
    "visit_relay": "a relay not visited before needs the operator's yes (EMPORIA_VISIT_POLICY)",
    "leave_relay": "ends the agent's own access; refused while anything is open there",
    "revoke_room_invite": "withdraws an invite the agent itself made",
    "revoke_agora_invite": "withdraws an invite the agent itself made",
    "unsubscribe_agora_topic": "drops the agent's own subscription",
}
NEWLY_BUDGETED = {"decline_challenge", "dispute_delivery", "leave_session", "leave_room",
                  "block_dm_sender", "reveal_models", "archive_now", "reclaim_escrow"}


def _classified(tool: str) -> str:
    if tool in autonomy.WRITE_TOOLS:
        return "budgeted"
    if tool in autonomy.MONEY_TOOLS:
        return "money"
    if tool in autonomy.GAME_MOVE_TOOLS:
        return "game move"
    if tool in autonomy.SETTINGS_TOOLS:
        return "setting"
    if tool in EXEMPT:
        return "exempt"
    if tool in READ_ONLY or tool.startswith(READ_PREFIXES):
        return "read"
    return ""


def test_every_tool_is_classified():
    unclassified = sorted(t for t in features.all_tools() if not _classified(t))
    assert not unclassified, f"decide how an unattended turn may use: {unclassified}"


def test_the_sets_name_only_real_tools():
    real = features.all_tools()
    for name, tools in (("WRITE_TOOLS", autonomy.WRITE_TOOLS), ("MONEY_TOOLS", autonomy.MONEY_TOOLS),
                        ("SETTINGS_TOOLS", autonomy.SETTINGS_TOOLS), ("GAME_MOVE_TOOLS", autonomy.GAME_MOVE_TOOLS),
                        ("EXEMPT", EXEMPT), ("READ_ONLY", READ_ONLY)):
        assert set(tools) <= real, (name, sorted(set(tools) - real))
    assert not set(EXEMPT) & autonomy.LOGGED_TOOLS and not READ_ONLY & autonomy.LOGGED_TOOLS


def test_the_state_changes_are_budgeted_and_logged():
    assert NEWLY_BUDGETED <= autonomy.WRITE_TOOLS and NEWLY_BUDGETED <= autonomy.LOGGED_TOOLS


def test_listing_blocked_senders_reads_but_blocking_one_is_a_write(monkeypatch, tmp_path):
    monkeypatch.setattr(autonomy, "store_dir", lambda: tmp_path)
    monkeypatch.setattr(autonomy, "data_dir", lambda: tmp_path)
    monkeypatch.delenv("EMPORIA_UNTRACKED_TURNS", raising=False)
    (tmp_path / "autonomy.json").write_text(json.dumps({"turns": {"t": {
        "origin": "rounds", "sender": "rounds", "at": time.time(), "writes": autonomy.MAX_WRITES_PER_TURN,
        "new_dms": []}}}))
    assert autonomy.guard("block_dm_sender", {"list": True}, "me") is None, "a list is no write"
    refused = autonomy.guard("block_dm_sender", {"sender": "eve"}, "me")
    assert refused and "at most" in refused["error"], "blocking still counts against the budget"


@pytest.mark.parametrize("tool", sorted(NEWLY_BUDGETED))
def test_each_counts_against_the_turn_and_is_logged(monkeypatch, tmp_path, tool):
    monkeypatch.setattr(autonomy, "store_dir", lambda: tmp_path)
    monkeypatch.setattr(autonomy, "data_dir", lambda: tmp_path)
    monkeypatch.delenv("EMPORIA_UNTRACKED_TURNS", raising=False)
    (tmp_path / "autonomy.json").write_text(json.dumps({"turns": {"t": {
        "origin": "rounds", "sender": "rounds", "at": time.time(), "writes": autonomy.MAX_WRITES_PER_TURN - 1,
        "new_dms": []}}}))
    # money-shaped tools stay refused as money first; the rest take the last write of the budget
    refusal = autonomy.guard(tool, {}, "me")
    if tool in autonomy.MONEY_TOOLS:
        assert refusal is None or refusal["refused_by"] == "emporia-autonomy"
    else:
        assert refusal is None
        again = autonomy.guard(tool, {}, "me")
        assert again and "at most" in again["error"], "the sixth write of the turn is refused"
    rows = autonomy.recent_actions(5, None)
    assert any(r["tool"] == tool for r in rows)
