"""Test helper: the stable id of a test agent that never rotated (the hash of its key), which is how a
relay under test is told who its operator is (`EMPORIA_OPERATOR_STABLE_ID`)."""
from __future__ import annotations


def stable_of(agent: str) -> str:
    if not agent:
        return ""
    import sys

    from emporia import identity, wire
    rs = sys.modules.get("emporia.relay_server")          # the relay's own answer, when it holds one (a rotated key)
    try:
        held = rs.AGENT_REGISTRY.get_stable_id(agent) if rs else None
    except Exception:  # noqa: BLE001
        held = None
    return held or wire.stable_id_for(identity.get_public_key_hex(agent))
