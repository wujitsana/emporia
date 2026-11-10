"""`analytics.model_cost`: dollars are what was metered; sessions a subscription pays for are counted apart."""
import sqlite3
import time

from emporia import analytics


def _db(path, rows, columns=True):
    conn = sqlite3.connect(path)
    extra = ", cost_status TEXT, billing_provider TEXT" if columns else ""
    conn.execute("CREATE TABLE sessions (id TEXT, source TEXT, chat_id TEXT, started_at REAL, estimated_cost_usd REAL, "
                 f"actual_cost_usd REAL, input_tokens INT, output_tokens INT{extra})")
    for r in rows:
        if columns:
            conn.execute("INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?,?,?)", r)
        else:
            conn.execute("INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?)", r[:8])
    conn.commit()
    conn.close()


NOW = time.time()
ROWS = [
    ("s1", "emporia", "emporia:alpha", NOW - 10, 0.10, None, 100, 10, "estimated", "openrouter"),
    ("s2", "emporia", "emporia:alpha:dm:beta", NOW - 20, 0.05, 0.04, 50, 5, "actual", "openrouter"),
    ("s3", "emporia", "emporia:alpha:session:x", NOW - 30, 1.50, None, 200, 20, "included", "openai-codex"),
    ("s4", "emporia", "emporia:alpha:room:r", NOW - 40, 2.00, None, 300, 30, "included", "openai-codex"),
    ("s5", "cli", "emporia:alpha", NOW - 50, 9.0, None, 1, 1, "estimated", "openrouter"),            # not Emporia
    ("s6", "emporia", "emporia:alpha:dm:old", NOW - 40 * 86400, 5.0, None, 1, 1, "estimated", ""),     # too old
]


def test_metered_dollars_leave_out_what_a_subscription_pays_for(tmp_path):
    _db(tmp_path / "state.db", ROWS)
    m = analytics.model_cost(tmp_path, "alpha", NOW - 30 * 86400)
    assert m["available"] and m["usd"] == 0.14, "actual else estimated, and nothing for an included session"
    assert m["sessions"] == 4 and m["tokens"] == 100 + 10 + 55 + 220 + 330
    assert (m["included_sessions"], m["included_tokens"], m["included_providers"]) == (2, 550, ["openai-codex"])
    assert m["by_kind"]["session"] == {"usd": 0.0, "sessions": 1} and m["by_kind"]["main"]["usd"] == 0.1


def test_the_text_says_metered_and_what_the_subscription_covers(tmp_path):
    _db(tmp_path / "state.db", ROWS)
    m = analytics.model_cost(tmp_path, "alpha", NOW - 30 * 86400)
    assert analytics.included_text(m) == "2 sessions included in your openai-codex subscription (count against its limits)"
    st = analytics.agent_stats("alpha", {}, m, 0, 300, 30, NOW)
    text = analytics.render(st)
    assert "$0.14 metered over 4 conversations" in text
    assert "2 sessions included in your openai-codex subscription (count against its limits)" in text
    assert st["net_cents"] == -14, "the net counts metered money only"


def test_nothing_included_says_nothing_and_an_old_state_db_is_all_metered(tmp_path):
    _db(tmp_path / "state.db", [ROWS[0], ROWS[1]])
    m = analytics.model_cost(tmp_path, "alpha", NOW - 86400)
    assert m["included_sessions"] == 0 and analytics.included_text(m) == ""
    assert "included" not in analytics.render(analytics.agent_stats("alpha", {}, m, 0, 300, 30, NOW))
    old = tmp_path / "old"
    old.mkdir()
    _db(old / "state.db", ROWS[:4], columns=False)            # a Hermes without cost_status: nothing is included
    m = analytics.model_cost(old, "alpha", NOW - 86400)
    assert m["usd"] == 3.64 and m["included_sessions"] == 0


def test_a_subscription_without_a_provider_name_is_still_counted(tmp_path):
    _db(tmp_path / "state.db", [("s", "emporia", "emporia:alpha", NOW - 5, 1.0, None, 10, 1, "included", None)])
    m = analytics.model_cost(tmp_path, "alpha", NOW - 86400)
    assert m["usd"] == 0.0 and m["included_providers"] == ["subscription"]
    assert analytics.included_text(m) == "1 session included in your subscription (count against its limits)"
