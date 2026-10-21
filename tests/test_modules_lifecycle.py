"""Every module can at least be created through the relay (P89).

`code_review.initial_state` indexed `participants[1]`, but a session is created
from its creator alone, so creating one was a 500. No test created one: the
suite only ever listed code-review *listings*.
"""
import uuid

import pytest
from fastapi.testclient import TestClient

from emporia.module_sdk import MODULE_REGISTRY
from emporia.relay_server import app
from tests.test_emporia import _register


@pytest.fixture
def client():
    return TestClient(app, client=("127.0.0.1", 51216), raise_server_exceptions=False)


@pytest.mark.parametrize("module_type", sorted(MODULE_REGISTRY))
def test_every_module_can_be_created(client, module_type):
    creator = "mod_" + uuid.uuid4().hex[:8]
    _register(client, creator)
    r = client.post("/sessions", json={"module_type": module_type, "creator_agent_id": creator},
                    headers={"x-emporia-agent-id": creator})
    assert r.status_code == 200, (module_type, r.status_code, r.text)
