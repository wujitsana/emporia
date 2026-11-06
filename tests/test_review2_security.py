"""Security review pass 2 (2026-11-06): each test fails without its fix."""
import base64

from emporia import signer
from tests.test_signer_phase2 import AGENT, _open, _sealed, held  # noqa: F401  (fixture)


def test_the_signer_takes_its_agent_id_in_one_spelling_only(held):  # noqa: F811
    """A token is the agent's, under the id the signer files it as. Before, `P2_AGENT` found
    p2_agent's token (the folder folds case) while the open cap counted per spelling, so a
    new spelling opened past the cap and never reached the burst alert."""
    signer.save_policy(AGENT, {"EMPORIA_RELAY_URL": "https://home.example", "EMPORIA_SIGNER_OPEN_PER_MIN": "1"})
    env, aad = _sealed("one", held.made["encryption"])
    assert _open(held, env, AGENT, aad).status_code == 200
    assert _open(held, env, AGENT, aad).status_code == 429                 # the cap
    for spelling in (AGENT.upper(), f" {AGENT}", f"{AGENT}!"):
        r = held.app.post("/v1/open", json={"envelope": env, "label": AGENT,
                                            "aad": base64.b64encode(aad).decode()},
                          headers={"authorization": f"Bearer {held.token}", "x-emporia-agent": spelling})
        assert r.status_code == 401, spelling
        assert "one" not in r.text
    r = held.app.get("/v1/wallet", headers={"authorization": f"Bearer {held.token}",
                                            "x-emporia-agent": AGENT.upper()})
    assert r.status_code == 401


def test_a_bargain_joiner_signs_one_deal_share_per_game(monkeypatch):
    """A hostile relay that refused the join and served another deal_commitment on the retry
    got a second share — a second deal — and could keep the hands it liked. The client now
    signs a share for the first commitment it saw, and refuses a game whose commitment moved."""
    import uuid

    from emporia import identity, wire
    from emporia import mcp_server as m

    class R:
        def __init__(self, body, code=200):
            self._b, self.status_code = body, code

        def json(self):
            return self._b

    served = {"c": "ab" * 32}
    posted = []

    def fake(method, url, **kw):
        if method == "post":
            posted.append(kw.get("json"))
            return R({"detail": "try again"}, 503)
        return R({"session_id": "sess_g", "module_type": "emporia:bargain:v1", "status": "waiting",
                  "config": {"deal_commitment": served["c"]}, "payment_rules": {}, "participants": ["x"]})
    monkeypatch.setattr(m, "_bearer_request", fake)
    me = f"gr_{uuid.uuid4().hex[:6]}"
    identity.get_public_key_hex(me)
    m.join_session("sess_g", agent_id=me)
    m.join_session("sess_g", agent_id=me)                       # a retry of the same game: the same share
    assert len(posted) == 2 and posted[0]["deal_signature"] == posted[1]["deal_signature"]
    served["c"] = "cd" * 32                                     # the relay deals again
    r = m.join_session("sess_g", agent_id=me)
    assert not r["success"] and "not the one you signed" in r["error"]
    assert len(posted) == 2, "no share signed for the new commitment"
    assert not any(identity.verify(wire.signed_deal_seed_payload("sess_g", me, "cd" * 32), b["deal_signature"],
                                   identity.get_public_key_hex(me)) for b in posted)
    served["c"] = {"not": "hex"}
    assert not m.join_session("sess_g2", agent_id=me)["success"]
