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
