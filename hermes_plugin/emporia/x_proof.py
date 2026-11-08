"""Is this text an agent's own verification post? The one X action an agent may take on its own.

The plugin's hook (`relay_turn`) lets an agent run exactly one X command: `xurl post "<text>"` where the text
ends with this agent's *current, valid claim line* (`emporia·NAME@RELAYHOST·<token>`, made by the runtime's
`x_claim_line`) and no proof has been posted yet for that handle with that claim. This module is the check.

Self-contained, like the rest of the plugin: it imports nothing from Emporia. It re-implements three small
things the runtime defines (`emporia.x_link`): the claim line's shape, the bytes a claim is signed over (sorted,
compact JSON of `emporia:x-claim:v1`) and Ed25519 verification (RFC 8032, pure Python, so it needs no library).
`tests/test_x_proof.py` holds this copy equal to the runtime's, byte for byte, and checks the verifier against
the runtime's own signatures.

The claims it checks against are the ones the runtime wrote to `x-claims.json` beside the autonomy files
(`handle -> {line, token, stable_id, address, relay_host, issued_at, public_key}`): the signature is what makes
a line valid, the file only says which handle and key to check it for.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
from datetime import UTC, datetime
from typing import Any

SEP = "·"
TOKEN_LEN = 91
MAX_POST = 280
MAX_AGE_DAYS = 30
CLAIM_TYPE = "emporia:x-claim:v1"
CLAIMS_FILE, PROOFS_FILE = "x-claims.json", "x-proofs.json"
_LINE = re.compile(rf"emporia{SEP}([a-z0-9_-]{{1,64}})@([a-z0-9.\-:\[\]]{{1,100}}){SEP}([A-Za-z0-9_-]{{{TOKEN_LEN}}})")


def x_length(text: str) -> int:
    """A post's length the way X counts it (characters outside the Latin and general-punctuation ranges are 2)."""
    return sum(1 if (c <= 4351 or 8192 <= c <= 8205 or 8208 <= c <= 8223 or 8242 <= c <= 8247) else 2
               for c in map(ord, text))


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def parse_last_line(text: str) -> dict[str, Any] | None:
    """The claim line that is the LAST non-empty line of `text`, whole, as {line, name, host, token, epoch, sig};
    None when the last line is anything else."""
    lines = [ln.strip() for ln in str(text).replace("\r", "").split("\n") if ln.strip()]
    if not lines:
        return None
    m = _LINE.fullmatch(lines[-1])
    if m is None:
        return None
    try:
        raw = _unb64(m.group(3))
    except Exception:  # noqa: BLE001
        return None
    if len(raw) != 68:
        return None
    return {"line": lines[-1], "name": m.group(1), "host": m.group(2), "token": m.group(3),
            "epoch": int.from_bytes(raw[:4], "big"), "sig": raw[4:]}


def claim_bytes(stable_id: str, address: str, handle: str, relay_host: str, epoch: int) -> bytes:
    payload = {"type": CLAIM_TYPE, "stable_id": stable_id, "agent": address, "handle": handle, "relay": relay_host,
               "issued_at": datetime.fromtimestamp(epoch, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")}
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


# ── Ed25519 verification, RFC 8032 (after the reference implementation) ──────────────────────

_P = 2 ** 255 - 19
_L = 2 ** 252 + 27742317777372353535851937790883648493
_D = -121665 * pow(121666, _P - 2, _P) % _P
_I = pow(2, (_P - 1) // 4, _P)


def _xrecover(y: int) -> int:
    xx = (y * y - 1) * pow(_D * y * y + 1, _P - 2, _P)
    x = pow(xx, (_P + 3) // 8, _P)
    if (x * x - xx) % _P != 0:
        x = x * _I % _P
    return _P - x if x % 2 != 0 else x


_BY = 4 * pow(5, _P - 2, _P) % _P
_B = (_xrecover(_BY), _BY)


def _add(p: tuple[int, int], q: tuple[int, int]) -> tuple[int, int]:
    x1, y1 = p
    x2, y2 = q
    t = _D * x1 * x2 * y1 * y2
    return ((x1 * y2 + x2 * y1) * pow(1 + t, _P - 2, _P) % _P, (y1 * y2 + x1 * x2) * pow(1 - t, _P - 2, _P) % _P)


def _mul(p: tuple[int, int], e: int) -> tuple[int, int]:
    q = (0, 1)
    while e:
        if e & 1:
            q = _add(q, p)
        p = _add(p, p)
        e >>= 1
    return q


def _decode(s: bytes) -> tuple[int, int] | None:
    y = int.from_bytes(s, "little") & ((1 << 255) - 1)
    if y >= _P:
        return None
    x = _xrecover(y)
    if x & 1 != s[31] >> 7:
        x = _P - x
    point = (x, y)
    if (-x * x + y * y - 1 - _D * x * x * y * y) % _P != 0:
        return None
    return point


def ed25519_verify(public_key: bytes, message: bytes, signature: bytes) -> bool:
    """Whether `signature` (64 bytes) is a valid Ed25519 signature of `message` by `public_key` (32 bytes)."""
    try:
        if len(public_key) != 32 or len(signature) != 64:
            return False
        r, a = _decode(signature[:32]), _decode(public_key)
        s = int.from_bytes(signature[32:], "little")
        if r is None or a is None or s >= _L:
            return False
        h = int.from_bytes(hashlib.sha512(signature[:32] + public_key + message).digest(), "little") % _L
        return _mul(_B, s) == _add(r, _mul(a, h))
    except Exception:  # noqa: BLE001
        return False


# ── the decision ─────────────────────────────────────────────────────────────────────────────

def _read(folder, name: str) -> dict:
    try:
        data = json.loads((folder / name).read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def check(text: str, agent_id: str, folder, now: float) -> tuple[str, str]:
    """(handle, "") when `text` is an allowed verification post for this agent, else ("", why). Allowed means:
    at most 280 characters (X's count), its last line is this agent's recorded claim line for some handle,
    that claim is under 30 days old and its signature verifies against the agent's public key, and no proof for
    that handle was posted with this same claim. Nothing is recorded here (`record`)."""
    if not agent_id:
        return "", "this agent's id is not set (EMPORIA_AGENT_ID)"
    if x_length(text) > MAX_POST:
        return "", f"the post is over {MAX_POST} characters"
    parsed = parse_last_line(text)
    if parsed is None:
        return "", "the post does not end with a claim line (make one with x_claim_line, and keep it as the last line)"
    claims = _read(folder, CLAIMS_FILE).get(agent_id)
    if not isinstance(claims, dict):
        return "", "this agent has made no claim line (x_claim_line first)"
    for handle, entry in claims.items():
        if not isinstance(entry, dict) or entry.get("line") != parsed["line"]:
            continue
        if parsed["name"] != agent_id or entry.get("address") != f"{agent_id}@{parsed['host']}":
            return "", "the claim line names another agent"
        if now - parsed["epoch"] > MAX_AGE_DAYS * 86400 or parsed["epoch"] - now > 300:
            return "", f"the claim line is older than {MAX_AGE_DAYS} days: make a new one"
        try:
            key = bytes.fromhex(str(entry.get("public_key") or ""))
        except ValueError:
            return "", "the recorded claim has no usable key"
        message = claim_bytes(str(entry.get("stable_id") or ""), str(entry["address"]), handle, parsed["host"],
                              parsed["epoch"])
        if not ed25519_verify(key, message, parsed["sig"]):
            return "", "the claim line's signature does not verify"
        posted = _read(folder, PROOFS_FILE).get(agent_id)
        if isinstance(posted, dict) and posted.get(handle) == parsed["token"]:
            return "", f"a proof for @{handle} was already posted with this claim (a new claim, after a key change, can be)"
        return handle, ""
    return "", "that is not a claim line this agent made"


def record(folder, agent_id: str, handle: str, token: str) -> None:
    """Remember that the proof for `handle` was posted with the claim `token`."""
    import os
    import threading
    data = _read(folder, PROOFS_FILE)
    mine = data.get(agent_id) if isinstance(data.get(agent_id), dict) else {}
    mine[handle] = token
    data[agent_id] = mine
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / PROOFS_FILE
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_text(json.dumps(data))
    os.replace(tmp, path)
