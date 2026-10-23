"""P125: the local DM reader opens a viewer's sealed DMs on the owner's
machine — and only with its token, and only for keys it holds."""
import json

import pytest
from fastapi.testclient import TestClient

from emporia import crypto_box, dm_reader, identity


@pytest.fixture
def ring(tmp_path, monkeypatch):
    keys = tmp_path / "alpha" / "home" / ".hermes" / "keys"
    keys.mkdir(parents=True)
    monkeypatch.setattr(identity, "KEY_DIR", keys)
    _, alpha_pub = identity.generate_or_load_encryption_keypair("alpha")
    other = tmp_path / "beta" / "home" / ".hermes" / "keys"
    other.mkdir(parents=True)
    monkeypatch.setattr(identity, "KEY_DIR", other)
    _, beta_pub = identity.generate_or_load_encryption_keypair("beta")
    return dm_reader.KeyRing(dm_reader.find_key_dirs(tmp_path)), alpha_pub, beta_pub


def _sealed(text, alpha_pub, beta_pub, thread="dm_t1", sender="beta", nonce="n" * 32):
    env = crypto_box.seal(text, {"alpha": alpha_pub.hex() if isinstance(alpha_pub, bytes) else alpha_pub,
                                 "beta": beta_pub.hex() if isinstance(beta_pub, bytes) else beta_pub},
                          crypto_box.message_aad("dm", thread, sender, "chat", nonce))
    return {"message_id": "dmm_1", "sender_id": sender, "msg_type": "chat", "nonce": nonce,
            "content": json.dumps(env)}


def test_the_reader_opens_a_sealed_dm_for_an_agent_whose_key_it_holds(ring):
    keyring, alpha_pub, beta_pub = ring
    assert keyring.agents() == ["alpha", "beta"]
    msg = _sealed("hello from beta", alpha_pub, beta_pub)
    out = dm_reader.open_messages(keyring, "alpha", "dm_t1", [msg, {"message_id": "p", "content": "plain"}])
    assert out == {"opened": {"dmm_1": "hello from beta"}}
    # The wrong thread is a different AAD: it does not open.
    assert dm_reader.open_messages(keyring, "alpha", "dm_other", [msg])["opened"] == {"dmm_1": None}
    assert "no encryption key" in dm_reader.open_messages(keyring, "gamma", "dm_t1", [msg])["error"]


def test_the_reader_answers_only_with_its_token(ring):
    keyring, alpha_pub, beta_pub = ring
    app = dm_reader.build_app(keyring, "t" * 32, ["http://localhost:8088"])
    c = TestClient(app)
    body = {"viewer": "alpha", "thread_id": "dm_t1", "messages": [_sealed("hi", alpha_pub, beta_pub)]}
    assert c.post("/dm/open", json=body).status_code == 401
    assert c.post("/dm/open", json=body, headers={"x-emporia-reader-token": "wrong"}).status_code == 401
    r = c.post("/dm/open", json=body, headers={"x-emporia-reader-token": "t" * 32})
    assert r.status_code == 200 and r.json()["opened"]["dmm_1"] == "hi"
