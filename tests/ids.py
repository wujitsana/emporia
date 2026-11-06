"""Test helper: the stable id of a test agent that never rotated (the hash of its key), which is how a
relay under test is told who its operator is (`EMPORIA_OPERATOR_STABLE_ID`)."""
from __future__ import annotations


def stable_of(agent: str) -> str:
    if not agent:
        return ""
    from emporia import identity, wire
    return wire.stable_id_for(identity.get_public_key_hex(agent))
