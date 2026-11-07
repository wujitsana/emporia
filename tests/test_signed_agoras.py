"""A topic's settings, invites, uninvites and kicks are its creator's signed acts (P278)."""
import pytest
from fastapi.testclient import TestClient

from emporia.identity import get_public_key_hex
from emporia.relay_server import app
from tests.test_emporia import _register_keyed


@pytest.fixture
def client():
    return TestClient(app, client=("127.0.0.1", 51420))


def _as(agent_id: str) -> dict:
    return {"x-emporia-agent-id": agent_id}


def _topic(client, owner: str) -> str:
    r = client.post("/agoras/topics", json={"name": f"{owner} topic", "visibility": "public"}, headers=_as(owner))
    assert r.status_code == 200, r.text
    return r.json()["slug"]


# --- P278: a topic's settings, invites, uninvites and kicks are its creator's signed acts ---

def test_a_topic_is_made_with_its_creators_signature_over_every_setting():
    from conftest import header_shim_off, signed_topic_body

    from emporia import identity, payee
    from tests.test_emporia import bearer_for
    with header_shim_off():
        c = TestClient(app, client=("127.0.0.1", 51421))
        _register_keyed(c, "st_owner")
        _register_keyed(c, "st_other")
        h = bearer_for(c, "st_owner")
        body = {"name": "St Club", "visibility": "private", "gate_type": "paid_invite", "entry_fee_cents": 300}
        assert c.post("/agoras/topics", json=body, headers=h).status_code == 401
        assert c.post("/agoras/topics", json=signed_topic_body("st_other", body), headers=h).status_code == 403
        cheaper = signed_topic_body("st_owner", body) | {"entry_fee_cents": 100}      # not what was signed
        assert c.post("/agoras/topics", json=cheaper, headers=h).status_code == 403
        r = c.post("/agoras/topics", json=signed_topic_body("st_owner", body), headers=h)
        assert r.status_code == 200, r.text
        slug = r.json()["slug"]
        topic = c.get(f"/agoras/topics/{slug}", headers=h).json()
    key = get_public_key_hex("st_owner")
    assert [a["act"] for a in topic["signed_acts"]] == ["create"]
    assert identity.verify_topic_acts(topic, key)["settings"] is True
    assert identity.verify_topic_acts({**topic, "entry_fee_cents": 900}, key)["settings"] is False
    assert identity.verify_topic_acts(topic, get_public_key_hex("st_other"))["settings"] is False
    assert payee.topic_terms(topic, key) == ({"st_owner"}, 300)
    with pytest.raises(payee.PayeeError, match="did not sign"):
        payee.topic_terms({**topic, "entry_fee_cents": 900}, key)
    assert payee.topic_terms(None, "") == (set(), None)


def test_invites_uninvites_and_kicks_are_signed_and_an_invitee_sees_the_terms(client):
    from conftest import header_shim_off, signed_topic_act

    from emporia import identity
    from tests.test_emporia import bearer_for
    for a in ("sti_owner", "sti_guest", "sti_late", "sti_stranger"):
        _register_keyed(client, a)
    slug = client.post("/agoras/topics", json={"name": "Sti Club", "visibility": "private",
                                               "gate_type": "paid_invite", "entry_fee_cents": 200},
                       headers=_as("sti_owner")).json()["slug"]
    assert client.get(f"/agoras/topics/{slug}", headers=_as("sti_guest")).status_code == 403
    with header_shim_off():
        c = TestClient(app, client=("127.0.0.1", 51422))
        h = bearer_for(c, "sti_owner")
        assert c.post(f"/agoras/topics/{slug}/invite", json={"invitee_id": "sti_guest"}, headers=h).status_code == 401
        forged = signed_topic_act("invite", slug, "sti_guest", "sti_stranger")
        assert c.post(f"/agoras/topics/{slug}/invite", json={"invitee_id": "sti_guest", **forged},
                      headers=h).status_code == 403
        assert c.post(f"/agoras/topics/{slug}/invite", headers=h, json={
            "invitee_id": "sti_guest", **signed_topic_act("invite", slug, "sti_guest", "sti_owner")}).status_code == 200
        assert c.delete(f"/agoras/topics/{slug}/invite/sti_late", headers=h).status_code == 401
    # The shim signs the rest as the named creator.
    assert client.post(f"/agoras/topics/{slug}/invite", json={"invitee_id": "sti_late"},
                       headers=_as("sti_owner")).status_code == 200
    assert client.delete(f"/agoras/topics/{slug}/invite/sti_late", headers=_as("sti_owner")).status_code == 200
    assert client.post(f"/agoras/topics/{slug}/kick", json={"target_agent_id": "sti_stranger"},
                       headers=_as("sti_owner")).status_code == 200

    card = client.get(f"/agoras/topics/{slug}", headers=_as("sti_guest")).json()   # invited, not yet paid
    assert card["listing_only"] and [a["act"] for a in card["signed_acts"]] == ["create"]
    assert "post_count" not in card
    assert identity.verify_topic_acts(card, get_public_key_hex("sti_owner"))["settings"] is True
    assert client.get(f"/agoras/topics/{slug}", headers=_as("sti_late")).status_code == 403    # invite taken back
    full = client.get(f"/agoras/topics/{slug}", headers=_as("sti_owner")).json()
    checked = identity.verify_topic_acts(full, get_public_key_hex("sti_owner"))
    assert [(a["act"], a["target"], a["verified"]) for a in checked["acts"]] == [
        ("invite", "sti_guest", True), ("invite", "sti_late", True), ("uninvite", "sti_late", True),
        ("kick", "sti_stranger", True)]
