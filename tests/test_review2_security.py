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
