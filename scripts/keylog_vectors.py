"""P253: the key-log vectors dashboard/src/keyLog.test.ts checks against, made by the
Python side so the TypeScript reader is held to it. Fresh keys each run (no private key is
written out).

    uv run python scripts/keylog_vectors.py > dashboard/src/keyLog.vectors.json
"""
import json
from cryptography.hazmat.primitives.asymmetric import ed25519
from cryptography.hazmat.primitives import serialization
from emporia import wire, identity

def new():
    k = ed25519.Ed25519PrivateKey.generate()
    pub = k.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw).hex()
    return k, pub

(a, A), (b, B), (c, C), (d, D), (r, R) = new(), new(), new(), new(), new()
sid = wire.stable_id_for(A)
T1, T2, T3, T4, T5 = ("2026-10-01T10:00:00.000001+00:00", "2026-10-02T10:00:00+00:00",
                      "2026-10-03T10:00:00.250000+00:00", "2026-10-04T10:00:00+00:00", "2026-10-05T10:00:00+00:00")
log = [
    {"by": "key", "old": A, "new": B, "at": T1, "signature": identity.sign_payload(a, wire.signed_key_rotation_payload(sid, A, B, T1))},
    {"by": "commit", "commitment": wire.recovery_commitment_for(R), "at": T2,
     "signature": identity.sign_payload(b, wire.signed_recovery_commitment_payload(sid, wire.recovery_commitment_for(R), T2))},
    {"by": "key", "old": B, "new": C, "at": T4, "signature": identity.sign_payload(b, wire.signed_key_rotation_payload(sid, B, C, T4))},
    {"by": "recovery", "old": B, "new": D, "recovery_public_key": R, "compromised_since": T3, "at": T5,
     "signature": identity.sign_payload(r, wire.signed_key_recovery_payload(sid, B, D, R, T3, T5))},
]
windows = wire.key_log_windows(sid, log, identity.verify, D)
assert windows and windows[-1]["key"] == D, windows

def msg(key, when, text):
    nonce = "ab" * 16
    sig = identity.sign_payload(key, wire.signed_dm_thread_payload("dm_t1", text, "text", nonce))
    m = {"content": text, "msg_type": "text", "nonce": nonce, "signature": sig, "created_at": when}
    m["python_verified"] = identity.verify_dm_message("dm_t1", m, identity.SigningKey(D, windows))
    return m

messages = [
    msg(a, "2026-09-30T09:00:00", "é from the genesis key, before the rotation"),
    msg(a, "2026-10-01T11:00:00+00:00", "genesis key, after it handed over"),
    msg(b, "2026-10-02T12:00:00+00:00", "second key, in its time"),
    msg(b, "2026-10-03T10:00:00.250001+00:00", "second key, after compromised_since"),
    msg(c, "2026-10-04T12:00:00+00:00", "the thief's key, undone"),
    msg(d, "2026-10-06T00:00:00Z", "the recovered key"),
]
print(json.dumps({"stable_id": sid, "current": D, "key_log": log, "windows": windows, "messages": messages}, indent=1))
