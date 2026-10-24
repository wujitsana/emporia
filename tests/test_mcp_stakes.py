"""P137: an agent can stake a wager it creates without a human — through a
payment intent, as join_session already could."""
from emporia import mcp_server as m


class _Resp:
    status_code = 200

    def json(self):
        return {"session_id": "sess_x"}


def test_create_session_passes_the_payment_intent(monkeypatch):
    sent = {}

    def fake(method, url, **kw):
        sent.update(kw.get("json") or {})
        return _Resp()
    monkeypatch.setattr(m, "_bearer_request", fake)
    r = m.create_session("emporia:blotto:v1", agent_id="alpha", payment_mode="stripe_pi",
                         stake_per_participant="1.00", payment_intent_id="pi_123")
    assert r["success"] and sent["payment_intent_id"] == "pi_123"
    sent.clear()
    m.create_session("emporia:blotto:v1", agent_id="alpha")
    assert "payment_intent_id" not in sent, "only sent when there is one"


def test_an_agent_with_its_games_full_cannot_open_another(monkeypatch):
    """P138: every move is a paid model turn — an agent left alone kept joining."""
    open_ = [{"session_id": f"sess_{i}", "status": "active", "module_type": "emporia:blotto:v1"}
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
            return R({"module_type": "emporia:two-thirds:v1"})
        return R({"session_id": "new"})
    monkeypatch.setattr(m, "_bearer_request", fake)

    r = m.create_session("emporia:blotto:v1", agent_id="alpha")
    assert r["error"] == "open_games_limit" and "3 open games" in r["detail"]
    assert m.join_session("sess_new", agent_id="alpha")["error"] == "open_games_limit"
    assert not [x for x in sent if x[0] == "post"], "nothing was created or joined"
    assert m.create_session("emporia:service:v1", agent_id="alpha")["success"], \
        "a purchase is not a game"
    monkeypatch.setenv("EMPORIA_MAX_OPEN_GAMES", "4")
    assert m.create_session("emporia:blotto:v1", agent_id="alpha")["success"]
