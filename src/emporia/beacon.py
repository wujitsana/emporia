"""The drand beacon client: fetch a round, verify its BLS signature ourselves (`emporia:rand:v1`).

Chain: `quicknet` (3 s rounds, `bls-unchained-g1-rfc9380`): signatures on G1, the public key on G2,
hash-to-curve per RFC 9380 (BLS12381G1_XMD:SHA-256_SSWU_RO_), message = sha256(round as 8 bytes
big-endian). Verified in pure Python with `py_ecc` (about 0.3 s a round). Every reader, the relay
included, checks the signature against the pinned key (`emporia.rand.PUBLIC_KEY`); a value from
anywhere else, a relay's cache too, is therefore as good as drand's own.
"""

from __future__ import annotations

import hashlib
import time
from collections.abc import Callable

from emporia import rand

DST = b"BLS_SIG_BLS12381G1_XMD:SHA-256_SSWU_RO_NUL_"
#: Where a client reads a round when its relay has none. Each answer is verified anyway.
DRAND_URLS = ("https://api.drand.sh", "https://drand.cloudflare.com", "https://api2.drand.sh")


def _point_sig(sig_hex: str):
    from py_ecc.bls.point_compression import decompress_G1
    return decompress_G1(int.from_bytes(bytes.fromhex(sig_hex), "big"))


def verify(round_no: int, sig_hex: str) -> bool:
    """True when `sig_hex` is the chain's signature of `round_no` (pinned key, pure Python)."""
    try:
        from py_ecc.bls.hash_to_curve import hash_to_G1
        from py_ecc.bls.point_compression import decompress_G2
        from py_ecc.optimized_bls12_381 import G2, curve_order, final_exponentiate, is_inf, multiply, neg, pairing
        if not rand.is_hex(sig_hex, 48) or isinstance(round_no, bool) or not isinstance(round_no, int) \
                or round_no < 1:
            return False
        pk_raw = bytes.fromhex(rand.PUBLIC_KEY)
        pk = decompress_G2((int.from_bytes(pk_raw[:48], "big"), int.from_bytes(pk_raw[48:], "big")))
        sig = _point_sig(sig_hex)
        if not is_inf(multiply(sig, curve_order)):      # not in the prime-order subgroup
            return False
        msg = hashlib.sha256(round_no.to_bytes(8, "big")).digest()
        h = hash_to_G1(msg, DST, hashlib.sha256)
        check = final_exponentiate(pairing(neg(G2), sig, final_exponentiate=False)
                                   * pairing(pk, h, final_exponentiate=False))
        return check == check.one()
    except Exception:  # noqa: BLE001 — any malformed input is simply "not valid"
        return False


def randomness(sig_hex: str) -> str:
    """drand's own `randomness` for a round: sha256 of its signature."""
    return hashlib.sha256(bytes.fromhex(sig_hex)).hexdigest()


def available(round_no: int, now: float | None = None) -> bool:
    return (time.time() if now is None else now) >= rand.round_time(round_no)


def fetch(round_no: int, get: Callable[[str], dict | None], bases: tuple[str, ...] = DRAND_URLS) -> str | None:
    """The verified signature of `round_no`, asking each base in turn, or None. `get(url)` returns
    the JSON answer or None; it is a parameter so tests use recorded beacons and never the network."""
    for base in bases:
        try:
            d = get(f"{base.rstrip('/')}/{rand.CHAIN_HASH}/public/{round_no}")
        except Exception:  # noqa: BLE001
            continue
        if isinstance(d, dict) and d.get("round") == round_no and verify(round_no, str(d.get("signature") or "")):
            return str(d["signature"])
    return None


def http_get(url: str) -> dict | None:
    import httpx
    r = httpx.get(url, timeout=6)
    return r.json() if r.status_code == 200 else None
