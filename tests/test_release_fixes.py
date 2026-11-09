"""Small pre-release fixes: the MCP server's stderr, the stale 'not available yet' list, and the like."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from emporia import features, mcp_server, setup_choices

ROOT = Path(__file__).resolve().parents[1]


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


# ── standing, record and results find an agent's sessions through the participants table ──────

def _finished(sid, players, status="completed"):
    from datetime import UTC, datetime

    from emporia import relay_server as rs
    from emporia.module_sdk import get_interaction_module
    now = datetime.now(UTC).isoformat()
    state = get_interaction_module("emporia:blotto:v2").initial_state(players, {})
    rs.save_session(session_id=sid, module_type="emporia:blotto:v2", config={}, payment_rules={"mode": "free"},
                    state=state, current_agent=players[0], step_number=3, status=status, participants=players,
                    created_at=now, updated_at=now)
    for p in players:
        rs.add_participant(sid, p)


def test_an_agents_sessions_come_from_the_participants_table_with_the_same_results():
    from fastapi.testclient import TestClient

    from emporia import relay_server as rs
    from emporia.routes import standing
    from tests.test_emporia import _register

    client = TestClient(rs.app, client=("127.0.0.1", 51233))
    names = ["pt_a", "pt_b", "pt_c", "pt_x_d", "pt_xxd"]          # "pt_x_d" is a LIKE pattern that also matches "pt_xxd"
    for n in names:
        _register(client, n)
    _finished("pt_s1", ["pt_a", "pt_b"])
    _finished("pt_s2", ["pt_b", "pt_c"])
    _finished("pt_s3", ["pt_a", "pt_c"], status="abandoned")
    _finished("pt_s4", ["pt_xxd", "pt_c"])
    with rs.DB_LOCK, rs.get_db() as conn:
        def old_way(agent):        # the scan this replaces: a LIKE over the JSON, then the exact filter the callers apply
            rows = conn.execute("SELECT session_id, participants_json FROM sessions WHERE participants_json LIKE ? "
                                "AND status IN ('completed', 'abandoned')", (f'%"{agent}"%',)).fetchall()
            import json
            return {r["session_id"] for r in rows if agent in json.loads(r["participants_json"])}

        def new_way(agent):
            return {r["session_id"] for r in conn.execute(
                "SELECT session_id FROM sessions WHERE session_id IN (SELECT session_id FROM participants "
                "WHERE agent_id = ?) AND status IN ('completed', 'abandoned')", (agent,))}

        for agent in names:
            assert new_way(agent) == old_way(agent), agent
        plan = " ".join(str(tuple(r)) for r in conn.execute(
            "EXPLAIN QUERY PLAN SELECT * FROM sessions WHERE session_id IN (SELECT session_id FROM participants "
            "WHERE agent_id = ?) AND status IN ('completed', 'abandoned')", ("pt_a",)))
    assert "idx_participants_agent" in plan or "sqlite_autoindex_participants" in plan, plan
    assert "SCAN sessions" not in plan, plan

    rec = {n: client.get(f"/agents/{n}/record").json()["games"] for n in names}
    assert rec["pt_a"]["finished"] == 1 and rec["pt_a"]["abandoned"] == 1 and rec["pt_x_d"]["finished"] == 0
    assert rec["pt_xxd"]["finished"] == 1                           # and "pt_x_d" does not collect its game
    got = {r["session_id"] for r in client.get("/results", params={"agent_id": "pt_c"}).json()["results"]}
    assert got == {"pt_s2", "pt_s4"}
    assert [r["session_id"] for r in client.get("/results", params={"agent_id": "pt_x_d"}).json()["results"]] == []
    games = standing._public_games_of("pt_c")
    assert {g["session_id"] for g in games} == {"pt_s2", "pt_s4"} and standing._public_games_of("pt_x_d") == []
    assert "participants_json LIKE" not in (ROOT / "src" / "emporia" / "routes" / "standing.py").read_text()


# ── the README's "Setup choices" table follows setup_choices.py ─────────────────────────────

#: setup choice key -> the README row's first cell. Keys of `emporia agent init` (init_*) are in docs/any-agent.md.
README_ROWS = {
    "profile": "Profile", "role": "Role", "keys": "Keys", "wallet": "Wallets", "wake": "Wake mode",
    "rounds": "Rounds", "optional": "Optional features", "skill_lock": "Skill lock", "playbook": "Playbook",
    "about": "Bio (`about`)", "register": "Register now", "sync_model": "Same model as this profile",
    "signer": "Signer", "stripe": "Card payouts (Stripe)",
}
#: Where the README words an option differently from its stored value.
README_ALIAS = {("keys", "env"): ".env", ("keys", "files"): "key files"}


def _readme_rows() -> dict[str, list[str]]:
    text = (ROOT / "README.md").read_text()
    table = text.split("## Setup choices")[1].split("\n### ")[0]
    rows = {}
    for line in table.splitlines():
        if line.startswith("| ") and not line.startswith(("| Setting", "|---")):
            cells = [c.strip() for c in re.split(r"(?<!\\)\|", line.strip().strip("|"))]
            rows[cells[0]] = cells
    return rows


def test_every_setup_choice_has_a_readme_row_with_its_options_and_default():
    rows = _readme_rows()
    asked = set(setup_choices.CHOICES) - {k for k in setup_choices.CHOICES if k.startswith("init_")}
    assert asked == set(README_ROWS), f"README_ROWS is out of step with setup_choices: {asked ^ set(README_ROWS)}"
    for key, label in README_ROWS.items():
        assert label in rows, f"README 'Setup choices' has no row {label!r}"
        _, options, recommended = rows[label][:3]
        choice = setup_choices.CHOICES[key]
        if key in ("wallet", "optional"):        # described in groups, not value by value
            continue
        for o in choice.options:
            word = README_ALIAS.get((key, o.value), o.value)
            assert word.lower() in options.lower(), f"{label}: option {o.value!r} is not in the README row"
        default = README_ALIAS.get((key, choice.default), choice.default)
        assert default.lower() in recommended.lower(), f"{label}: recommended {choice.default!r} is not in the row"


def test_the_optional_features_row_lists_every_feature_and_the_recommended_set():
    _, options, recommended = _readme_rows()["Optional features"][:3]
    assert {w.strip() for w in options.split(",")} == {"rounds", *features.OPTIONAL}
    assert {w.strip() for w in recommended.split(",")} == {"rounds", *features.DEFAULTS}
    assert {o.value for o in setup_choices.CHOICES["optional"].options} == {"rounds", *features.OPTIONAL}
    assert setup_choices.CHOICES["optional"].recommended() == {"rounds", *features.DEFAULTS}
