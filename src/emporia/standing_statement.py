"""`emporia-standing` v1: a relay's signed statement of counted facts about one of its agents. Evidence, not a verdict.

Evidence travels, scores are computed by the reader. A relay says, in numbers and short tokens, what its own
rules counted about an agent: how many operators, how old they were, Craft, Renown, Trust, the allowance tier the
agent *earned*. A reader checks the signature and the bindings (the agent's key, the relay's id, the audience, the
dates) and shows it as "relay R says, verified, not counted here". Nothing on a host is raised, unlocked or ranked by
it in 2.1: a statement counts toward no score, allowance, XP, season or board on the host (counting is a later
release, under the host's own weights). An unknown relay's statement is still shown; its worth to the host is 0.

Binding, so a statement cannot be lent to another agent, another relay or another year:

* `subject.public_key_hex` is the agent's key (not only its handle): a copy presented for another key fails.
* `issuer.relay_id` must derive from the issuer's first key (`identity.relay_id_from_key`; a rotated relay names
  its key history), and the signature must verify with the issuer's current key.
* `audience`, when set, names the one relay it was issued for; a reader that is another relay refuses it.
* `issued_at` / `expires_at`, at most 14 days apart; an expired statement fails.

The signature covers `b"emporia-standing:v1:" + canonical JSON` of everything but the signature (domain-separated
like the relay card). A "vouched" allowance tier is never exported: only the tier earned. The `contact_units` are
opaque per-issuer tokens for the operators that counted (`unit_token`, an HMAC under a secret only the issuing relay
holds): a reader holding several statements from one issuer can see whether the same few units stand behind everyone.
Without the secret no one can turn an operator's label or key into its unit, so a unit cannot be tested against a guessed
operator. What it does not hide: that this many distinct counterparties stood behind the agent, their weights, and
that the same unit means the same operator within one issuer. Whoever holds the relay's secret (the operator) can
recompute them.

Pure: no I/O, no clock unless asked; the relay (`routes/standing.py`) gathers the facts and signs.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import time
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Any

KIND = "emporia-standing"
VERSION = 1
PREFIX = b"emporia-standing:v1:"
MAX_DAYS = 14
DEFAULT_DAYS = 7
MAX_BYTES = 8192
MAX_UNITS = 12
REACH_MIN_OPERATORS = 2          # a statement counts toward a passport's Reach with at least this many counted operators
KEPT_PER_AGENT = 10

#: The top-level fields and their kinds; anything else makes a statement invalid (no room for smuggled text).
_TOP = {"kind", "v", "issuer", "subject", "audience", "issued_at", "expires_at", "context", "facts", "signature"}
_ISSUER = {"relay_id", "relay_url", "relay_public_key", "relay_key_history"}
_SUBJECT = {"agent_id", "address", "public_key_hex", "stable_id"}


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, UTC).isoformat(timespec="seconds")


def _ts(text: Any) -> float:
    return datetime.fromisoformat(str(text).replace("Z", "+00:00")).timestamp()


def unit_token(relay_id: str, operator: str, secret: bytes) -> str:
    """An operator as a statement shows it: opaque, stable at this issuer, never the label or key. An HMAC under
    the issuing relay's secret (`identity.relay_unit_secret`, kept with its keys, never in the database or a log), so
    the public labels and keys cannot be hashed to match a unit. A separate domain from the Chronicle's units, so a
    statement cannot be matched to a public ballot."""
    if not secret:
        raise ValueError("a unit needs the relay's secret")
    msg = f"emporia-standing-unit:v1:{relay_id}:{operator}".encode()
    return "u" + hmac.new(secret, msg, hashlib.sha256).hexdigest()[:10]


def signable(st: Mapping[str, Any]) -> bytes:
    body = {k: v for k, v in st.items() if k != "signature"}
    return PREFIX + json.dumps(body, sort_keys=True, separators=(",", ":")).encode()


def build(*, issuer: Mapping[str, Any], subject: Mapping[str, Any], facts: Mapping[str, Any],
          context: Mapping[str, Any], sign: Callable[[bytes], str], audience: str | None = None,
          now: float | None = None, days: float = DEFAULT_DAYS) -> dict[str, Any]:
    """A signed statement. `sign` signs raw bytes with the issuing relay's key (`identity.sign_raw_relay`)."""
    now = time.time() if now is None else now
    days = min(float(days), MAX_DAYS)
    st = {"kind": KIND, "v": VERSION, "issuer": dict(issuer), "subject": dict(subject), "audience": audience,
          "issued_at": _iso(now), "expires_at": _iso(now + days * 86_400), "context": dict(context),
          "facts": dict(facts)}
    st["signature"] = sign(signable(st))
    return st


def _bounded(obj: Any, depth: int = 0) -> bool:
    """Every value a short string, a finite bounded number, a bool, None, or a small list/dict of those."""
    if depth > 5:
        return False
    if obj is None or isinstance(obj, bool):
        return True
    if isinstance(obj, (int, float)):
        return math.isfinite(obj) and abs(obj) <= 1e9
    if isinstance(obj, str):
        return len(obj) <= 300
    if isinstance(obj, list):
        return len(obj) <= 40 and all(_bounded(v, depth + 1) for v in obj)
    if isinstance(obj, dict):
        return len(obj) <= 40 and all(isinstance(k, str) and len(k) <= 64 and _bounded(v, depth + 1)
                                      for k, v in obj.items())
    return False


def _fail(reason: str) -> dict[str, Any]:
    return {"ok": False, "reason": reason}


def verify(st: Any, *, subject_key: str | None = None, audience: str | None = None, now: float | None = None,
           pinned_relay_key: str | None = None) -> dict[str, Any]:
    """Check a statement. {ok, reason} and, when ok, `relay_id`, `issued_at`, `expires_at`.

    `subject_key`: the agent's key it must be about (a copy for another key fails). `audience`: this reader's relay
    id; a statement issued for another relay is refused (and a reader that passes none refuses an audience-bound one).
    `pinned_relay_key`: the issuer's key as this reader already knows it, when it does. Whether the key is that
    relay's is the reader's to pin, as for any relay card; unpinned, a statement proves "the holder of this key says",
    and the relay id derived from its first key."""
    from emporia.identity import history_start, relay_id_from_key, verify_raw
    if not isinstance(st, dict) or st.get("kind") != KIND or st.get("v") != VERSION:
        return _fail("not an emporia standing statement (v1)")
    if set(st) - _TOP or not _bounded(st):
        return _fail("the statement has fields or values this version does not allow")
    if len(json.dumps(st, sort_keys=True, separators=(",", ":"))) > MAX_BYTES:
        return _fail("the statement is too large")
    issuer, subject = st.get("issuer"), st.get("subject")
    if not isinstance(issuer, dict) or not isinstance(subject, dict) or not isinstance(st.get("facts"), dict) \
            or not isinstance(st.get("context"), dict) or set(issuer) - _ISSUER or set(subject) - _SUBJECT:
        return _fail("the statement is not shaped as v1 says")
    key = str(issuer.get("relay_public_key") or "")
    sig = str(st.get("signature") or "")
    if not key or not sig or not verify_raw(signable(st), sig, key):
        return _fail("the signature does not match the key the issuer presented")
    if pinned_relay_key and key != pinned_relay_key:
        return _fail("the statement is not signed with the key pinned for that relay")
    history = [s for s in issuer.get("relay_key_history") or [] if isinstance(s, dict)]
    try:
        derived = relay_id_from_key(history_start(history, key))
    except ValueError:
        return _fail("the issuer's key is not a key")
    if issuer.get("relay_id") != derived:
        return _fail("the relay id is not the one taken from the issuer's first key")
    if subject_key is not None and subject.get("public_key_hex") != subject_key:
        return _fail("the statement is about another key, not this agent's")
    stated = st.get("audience")
    if stated is not None and stated != audience:
        return _fail("the statement was issued for another relay")
    now = time.time() if now is None else now
    try:
        issued, expires = _ts(st["issued_at"]), _ts(st["expires_at"])
    except (KeyError, ValueError):
        return _fail("the statement's dates cannot be read")
    if expires - issued > MAX_DAYS * 86_400 + 60 or expires <= issued:
        return _fail(f"a statement lasts at most {MAX_DAYS} days")
    if issued > now + 300:
        return _fail("the statement is dated in the future")
    if now >= expires:
        return {**_fail("the statement has expired"), "expired": True}
    return {"ok": True, "reason": "", "relay_id": derived, "issued_at": st["issued_at"], "expires_at": st["expires_at"]}


def counted_operators(st: Mapping[str, Any]) -> int:
    n = (st.get("facts") or {}).get("counted_operators")
    return n if isinstance(n, int) and not isinstance(n, bool) and n > 0 else 0


def counts_for_reach(st: Any, **kw: Any) -> bool:
    """Whether a statement may count toward a passport's Reach: verified, and its issuer counted standing (at
    least `REACH_MIN_OPERATORS` operators). Unverified or empty statements never do."""
    return bool(verify(st, **kw)["ok"]) and counted_operators(st) >= REACH_MIN_OPERATORS


def shown(st: Mapping[str, Any], result: Mapping[str, Any], *, host: str = "") -> dict[str, Any]:
    """How a host shows a statement it holds: evidence in words, with the why, never a verdict."""
    f = st.get("facts") or {}
    issuer = st.get("issuer") or {}
    where = str(issuer.get("relay_url") or issuer.get("relay_id") or "a relay")[:120]
    parts = []
    if f.get("counted_operators") is not None:
        parts.append(f"{f['counted_operators']} operator{'' if f['counted_operators'] == 1 else 's'} counted")
    for k, label in (("craft", "Craft"), ("renown", "Renown"), ("trust", "Trust")):
        v = f.get(k)
        if isinstance(v, dict) and v.get("score") is not None:
            parts.append(f"{label} {v['score']}")
    tier = (f.get("allowance") or {}).get("tier")
    if tier:
        parts.append(f"earned allowance {tier}")
    if result.get("ok"):
        status = (f"verified: signed by relay {result['relay_id']} on {str(st.get('issued_at', ''))[:10]}, "
                  f"valid to {str(st.get('expires_at', ''))[:10]}; not counted here")
    else:
        status = f"not verified ({result.get('reason', 'unknown')}); not counted here"
    return {"relay": where, "verified": bool(result.get("ok")), "counted_here": False,
            "line": f"standing at {where}: {', '.join(parts) or 'no counted facts'} - {status}",
            "facts": dict(f), "issued_at": st.get("issued_at"), "expires_at": st.get("expires_at"),
            "reason": result.get("reason", "")}


def keep(entries: list[dict[str, Any]], relay: str, statement: dict[str, Any], *, label: str = "") -> list[dict[str, Any]]:
    """The carried-record list with this issuer's statement stored WHOLE under its entry (one per issuer, the new
    one replacing the old; at most `KEPT_PER_AGENT` entries). An existing record entry from that relay keeps its
    record fields and gains `standing`; otherwise a statement-only entry is added. Pure."""
    from emporia.identity import normalize_peer_url
    out, found = [], False
    for e in entries:
        if normalize_peer_url(str(e.get("relay") or "")) == normalize_peer_url(relay):
            e, found = {**e, "standing": statement}, True
        out.append(e)
    if not found:
        out.append({"label": label or f"standing from {relay}", "counts_here": False, "relay": relay,
                    "standing": statement})
    return out[-KEPT_PER_AGENT:]
