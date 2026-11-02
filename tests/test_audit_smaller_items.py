"""P267: the 11-02 audit's smaller items."""
import logging
import time

from fastapi.testclient import TestClient

from emporia.relay_server import app
from tests.test_emporia import _register_keyed, _rotate_keyed, bearer_for


def test_a_key_change_ends_the_old_keys_sign_ins():
    """Only bearer tokens here (the conftest header shim acts on the test header alone)."""
    client = TestClient(app, client=("127.0.0.1", 51690))
    _register_keyed(client, "si_rotated")
    old = bearer_for(client, "si_rotated")
    assert client.get("/agents/si_rotated/inbox", headers=old).status_code == 200
    time.sleep(0.01)
    _rotate_keyed(client, "si_rotated")
    assert client.get("/agents/si_rotated/inbox", headers=old).status_code == 403
    assert client.get("/agents/si_rotated/inbox", headers=bearer_for(client, "si_rotated")).status_code == 200


def test_a_websocket_token_never_reaches_a_log(caplog):
    from emporia.logging_config import redact_tokens_in
    redact_tokens_in("uvicorn.error")
    with caplog.at_level(logging.INFO, logger="uvicorn.error"):
        logging.getLogger("uvicorn.error").info('%s - "WebSocket %s" [accepted]', "127.0.0.1:5",
                                                "/ws/agent/a?token=eyJhbGci.secret.part&x=1")
    assert "secret" not in caplog.text and "token=[redacted]&x=1" in caplog.text
