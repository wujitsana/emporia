"""Games take no stakes, so create_session and join_session take no payment intent; and P138: an agent's
open games are capped."""
from emporia import mcp_server as m


class _Resp:
    status_code = 200

    def json(self):
        return {"session_id": "sess_x"}


def test_create_session_takes_no_payment_intent():
    import inspect
    params = inspect.signature(m.create_session).parameters
    assert "payment_intent_id" not in params, "a game has no stake to hold"
    assert "payment_intent_id" not in inspect.signature(m.join_session).parameters


def test_an_agent_with_its_games_full_cannot_open_another(monkeypatch):
    """P138: every move is a paid model turn — an agent left alone kept joining."""
    open_ = [{"session_id": f"sess_{i}", "status": "active", "module_type": "emporia:blotto:v2"}
             for i in range(3)]
    sent = []

    class R:
        status_code = 200

        def __init__(self, body):
            self.body = body

        def json(self):
            return self.body

    def fake(method, url, **kw):
        sent.append((method, url))
        if url.endswith("/agents/alpha/sessions"):
            return R({"sessions": open_ + [{"session_id": "s", "status": "completed",
                                             "module_type": "emporia:chess:v1"},
                                            {"session_id": "b", "status": "active",
                                             "module_type": "emporia:service:v1"}]})
        if method == "get":
            return R({"module_type": "emporia:two-thirds:v2"})
        return R({"session_id": "new"})
    monkeypatch.setattr(m, "_bearer_request", fake)

    r = m._create_session("emporia:blotto:v2", agent_id="alpha")
    assert r["error"] == "open_games_limit" and "3 open games" in r["detail"]
    assert m.join_session("sess_new", agent_id="alpha")["error"] == "open_games_limit"
    assert not [x for x in sent if x[0] == "post"], "nothing was created or joined"
    assert m.create_session("emporia:service:v1", agent_id="alpha")["success"], \
        "a purchase is not a game"
    monkeypatch.setenv("EMPORIA_MAX_OPEN_GAMES", "4")
    assert m._create_session("emporia:blotto:v2", agent_id="alpha")["success"]


def test_create_session_is_for_services_and_sends_a_game_to_challenge(monkeypatch):
    from emporia import mcp_server as m

    def no_relay(*a, **k):
        raise AssertionError("a game is refused before the relay is asked")
    monkeypatch.setattr(m, "_bearer_request", no_relay)
    for game in ("emporia:chess:v1", "emporia:blotto:v2", "emporia:bargain:v2"):
        out = m.create_session(game, agent_id="alpha")
        assert out["success"] is False and "challenge" in out["error"] and game in out["error"]
