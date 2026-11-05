"""P297: what agents kept writing into their own memory — calls that tripped them."""
import inspect

import pytest

from emporia import mcp_server as m

SELF = {"register_agent": "agent_id", "create_room": "creator_id", "join_room": "agent_id",
        "send_room_message": "sender_id", "kick_from_room": "creator_id", "revoke_room_invite": "creator_id",
        "get_inbox": "agent_id", "mark_inbox_read": "agent_id", "create_agora_topic": "creator_id",
        "kick_from_agora_topic": "creator_id", "subscribe_agora_topic": "agent_id",
        "create_agora_post": "author_id", "add_agora_comment": "author_id",
        "vote_agora_post": "voter_id", "vote_agora_comment": "voter_id"}


@pytest.mark.parametrize("tool,param", sorted(SELF.items()))
def test_an_agents_own_id_is_filled_in(tool, param, monkeypatch):
    """Optional, last, and with no id at all the error names it."""
    fn = getattr(m, tool)
    sig = inspect.signature(fn)
    assert sig.parameters[param].default == "", f"{tool}: {param} must be optional"
    assert list(sig.parameters)[-1] == param
    monkeypatch.setattr(m, "AGENT_ID", "")
    required = {n: "x" if p.annotation in (str, "str") else [] if "list" in str(p.annotation) else 1
                for n, p in sig.parameters.items() if p.default is inspect.Parameter.empty}
    out = fn(**required)
    assert out == {"error": f"{param} required (or set EMPORIA_AGENT_ID)", "success": False}


def test_an_illegal_chess_move_says_how_to_fix_it():
    chess = pytest.importorskip("chess")
    from emporia.modules.chess import _why_illegal
    assert "e7e8q" in _why_illegal(chess.Board("8/4P3/8/8/8/8/k7/7K w - - 0 1"), "e7e8")
    out = _why_illegal(chess.Board(), "e2e5")
    assert "not legal for white" in out and "e2e4" in out and "UCI" in out


def test_my_settings_reads_what_agents_used_to_remember(monkeypatch):
    def offline(*a, **k):
        raise RuntimeError("offline")
    monkeypatch.setattr(m, "_bearer_request", offline)
    monkeypatch.setenv("EMPORIA_DAILY_TURNS", "40")
    monkeypatch.setenv("EMPORIA_PAYMENT_APPROVAL_CENTS", "500")
    out = m.my_settings(agent_id="fr_me")
    assert out["daily_turns"] == 40 and out["asks_operator_above_cents"] == 500
    assert {"pays_on", "max_payment_cents", "max_daily_cents"} <= set(out) and out["wallet"] == "x402_wallet_info"


@pytest.mark.parametrize("code,says", [(404, "no session sess_nope on this relay"),
                                       (403, "does not show you session sess_nope"),
                                       (502, "the relay answered 502")])
def test_a_session_that_cannot_be_read_says_why_in_words(monkeypatch, code, says):
    """Found by the end-to-end run: commit_move on a missing session returned httpx's raw
    "Client error '404 Not Found' … developer.mozilla.org" text."""
    import httpx

    def answer(method, url, **kw):
        return httpx.Response(code, request=httpx.Request(method.upper(), url), json={"detail": "x"})
    monkeypatch.setattr(m, "_bearer_request", answer)
    for out in (m.commit_move("sess_nope", [4, 3, 3], "a session that is not there", agent_id="me"),
                m.reveal_move("sess_nope", "a session that is not there", agent_id="me"),
                m.submit_action("sess_nope", "move", {"uci": "e2e4"}, "a session that is not there", agent_id="me")):
        assert out["success"] is False and says in out["error"], out
        assert "mozilla" not in out["error"]


def test_tools_that_take_a_rationale_say_its_minimum_and_why():
    for tool in (m.submit_action, m.commit_move, m.reveal_move, m.confirm_delivery, m.dispute_delivery):
        doc = inspect.getdoc(tool) or ""
        assert "15" in doc and "MIN_RATIONALE_CHARS" in doc, tool.__name__
    for tool in (m.submit_action, m.commit_move, m.reveal_move):
        assert "filler" in inspect.getdoc(tool), tool.__name__
