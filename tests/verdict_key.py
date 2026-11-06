"""Security review pass 2 (P1): a test relay's operator verdict key, and verdict bodies signed with it.

A verdict is signed by the operator's verdict key — a person's key, never the operator agent's —
and the relay takes it only when `EMPORIA_OPERATOR_VERDICT_KEY` names that key. Tests that judge
reports or ideas register this key (`register(monkeypatch)`) and send bodies made here."""
from datetime import UTC, datetime

from cryptography.hazmat.primitives.asymmetric import ed25519

from emporia import identity, operator_verdicts, wire

KEY = ed25519.Ed25519PrivateKey.from_private_bytes(bytes(range(32)))
PUB = KEY.public_key().public_bytes_raw().hex()


def register(monkeypatch, pub: str = PUB) -> str:
    monkeypatch.setenv(operator_verdicts.ENV, pub)
    return pub


def _relay() -> str:
    from emporia import relay_server as rs
    return rs.relay_id()


def bug(report_id, verdict, severity="", fix_commit="", duplicate_of="", *, key=KEY, at=None, relay=None) -> dict:
    """The body of `POST /bugs/{id}/verdict`, signed by `key`."""
    at = at or datetime.now(UTC).isoformat()
    pub = key.public_key().public_bytes_raw().hex()
    p = wire.signed_bug_verdict_payload(report_id, verdict, severity, fix_commit, duplicate_of,
                                        relay or _relay(), pub, at)
    return {"verdict": verdict, "severity": severity, "fix_commit": fix_commit, "duplicate_of": duplicate_of,
            "verdict_key": pub, "at": at, "signature": identity.sign_payload(key, p)}


def idea(post_id, verdict, ref="", duplicate_of="", *, key=KEY, at=None, relay=None) -> dict:
    """The body of `POST /ideas/{id}/verdict`, signed by `key`."""
    at = at or datetime.now(UTC).isoformat()
    pub = key.public_key().public_bytes_raw().hex()
    p = wire.signed_idea_verdict_payload(post_id, verdict, ref, duplicate_of, relay or _relay(), pub, at)
    return {"verdict": verdict, "ref": ref, "duplicate_of": duplicate_of, "verdict_key": pub, "at": at,
            "signature": identity.sign_payload(key, p)}
