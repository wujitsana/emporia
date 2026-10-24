"""Content-safety / prompt-injection firewall for Emporia.

Protects the relay from adversarial prompt-injection and exploit text before
the protocol kernel processes any turn. Anti-cheat (bot fingerprints, PoR
density) is handled separately in the relay.

One layer, deliberately: a deterministic regex firewall (`scan_payload`,
`assert_payload_safe`, `assert_payload_safe_async`) — always on, zero-latency,
offline, opt-out only. P151 removed the optional NVIDIA-hosted semantic check
the hackathon added: it sent message text to a third party, cost up to six
paid model calls per write, and could not read sealed DMs or private sessions
anyway. Fairness and integrity come from the protocol now (signatures,
commit-reveal, verify_session), not from classifying text.
"""

from __future__ import annotations

import json
import logging
import os
import re
import unicodedata
from dataclasses import asdict, dataclass
from typing import Any, Iterable, Mapping

logger = logging.getLogger(__name__)

DEFAULT_MODE = os.getenv("EMPORIA_GUARDRAILS_MODE", "enforce").lower()

_INJECTION_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(pattern, re.IGNORECASE | re.DOTALL)
    for pattern in (
        r"ignore\s+(all\s+)?(previous|prior|above)\s+(instructions|rules|messages)",
        r"system\s+(prompt|message|instruction)",
        r"developer\s+(prompt|message|instruction)",
        r"execute\s+(a\s+)?(system|terminal|shell)\s+command",
        r"rm\s+-rf\s+/",
        r"curl\s+[^\n|;]+\|\s*(bash|sh)",
        r"exfiltrate|steal\s+(secrets|keys|tokens|credentials)",
        r"print\s+(the\s+)?(environment|env|secrets|api[_-]?keys)",
        r"bypass\s+(guardrails|security|safety|policy)",
        r"jailbreak|prompt\s*injection",
    )
)

# Structural/identifier fields exempt from scanning — ids, hashes, signatures, enum-like
# values. Everything else is scanned by default (secure-by-default: a new free-text field
# added anywhere in the relay is protected automatically, with no separate registration
# step). This replaces an earlier allowlist design that silently skipped any field not on
# the list — verified live: a listing `description` containing an injection phrase was
# accepted with no guardrail block, because "description" was never on that allowlist.
_STRUCTURAL_FIELDS = {
    "agent_id", "from_agent", "to_agent", "winner_id", "invited_by",
    "signature", "nonce", "token", "jwt", "client_secret",
    "module_type", "action_type", "event_type", "msg_type", "post_type",
    "gate_type", "payment_mode", "room_type", "listing_type", "visibility",
    "status", "outcome_type", "trust_level", "currency", "provider",
    "slug", "flair", "type",
}
_STRUCTURAL_SUFFIXES = ("_id", "_hex", "_url", "_secret", "_token", "_key")


def _is_structural_field(key: str) -> bool:
    """True for identifier/hash/enum-like *field names* — necessary but not
    sufficient for exemption, see `_looks_like_token`. Suffix-matched (not just
    an exact-name set) so a field like `parent_comment_id` or
    `stripe_account_id` is exempt without needing every id/hash/url-shaped name
    enumerated by hand.
    """
    return key in _STRUCTURAL_FIELDS or key.endswith(_STRUCTURAL_SUFFIXES)


def _looks_like_token(value: str) -> bool:
    """True if `value` looks like a genuine id/enum/short-token rather than
    free text. A real enum/id value never needs whitespace; injection text
    always does. Key-name exemption alone is bypassable — an attacker can put
    injection text under any structurally-named key inside a freeform dict
    (e.g. SubmitActionRequest.payload, CreateListingRequest.metadata): a value
    like {"status": "Ignore all previous instructions..."} would slip past a
    key-only check since "status" is a legitimate enum field elsewhere. This
    value-shape check closes that — exemption requires BOTH the key to look
    structural AND the value to look like a token, not prose.
    """
    v = value.strip()
    return bool(v) and len(v) <= 64 and " " not in v


@dataclass(frozen=True)
class GuardrailResult:
    ok: bool
    mode: str
    reason: str | None = None
    matched_pattern: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class GuardrailBlocked(PermissionError):
    """Raised when the firewall blocks a payload. Carries the structured
    `GuardrailResult` (which pattern matched) so callers need not parse the
    exception's message text."""

    def __init__(self, result: GuardrailResult) -> None:
        super().__init__(result.reason or "REJECTED_SECURITY: blocked unsafe payload.")
        self.result = result


def _iter_text_values(value: Any, *, parent_key: str | None = None) -> Iterable[str]:
    if isinstance(value, str):
        # Scan unless the immediate parent key looks structural AND the value
        # itself looks like a token, not free text (see _looks_like_token).
        if parent_key is None or not (_is_structural_field(parent_key) and _looks_like_token(value)):
            yield value
    elif isinstance(value, Mapping):
        for key, child in value.items():
            yield from _iter_text_values(child, parent_key=str(key))
    elif isinstance(value, list):
        for child in value:
            yield from _iter_text_values(child, parent_key=parent_key)


# P24-3 (W3): zero-width/formatting characters with no NFKC decomposition to
# nothing -- they're not compatibility variants of a visible character, just
# invisible, so NFKC alone (_normalize_agent_id()'s precedent, relay_server.py)
# leaves them untouched. ZWSP, ZWNJ, ZWJ, BOM/ZWNBSP, word joiner, soft hyphen.
_ZERO_WIDTH_RE = re.compile("[​‌‍﻿⁠­]")


def _normalize_for_scan(text: str) -> tuple[str, ...]:
    """P24-3 (W3): homoglyph/zero-width obfuscation defense for the
    deterministic regex layer. `igno​re` (a zero-width character
    inserted mid-word) and `ignore​all` (one substituted for the
    whitespace `_INJECTION_PATTERNS` requires between words) both defeat
    every pattern as stored, unnormalized -- confirmed live before this fix.

    Stripping alone is insufficient: the *strip* variant fixes the first
    case (`igno​re` -> `ignore`) but not the second (`ignore​all`
    stripped is `ignoreall`, still not matching `\\s+`) -- while a
    *space-substitution* variant fixes the second but would mangle the
    first (`igno re`, splitting a word the pattern expects intact). Trying
    to pick one universally breaks the other, so both variants are tried.

    Returns a deduped `(raw, strip-variant, space-variant)` -- never
    mutates `text` itself; `scan_text` applies every pattern against every
    variant. NFKC (folding full-width forms, NBSP, and other compatibility
    variants) is applied as the base of both normalized variants, not the
    raw one, so an attacker can't dodge detection by using a NFKC-folded
    homoglyph in an otherwise-clean payload that only the raw check sees.
    """
    nfkc = unicodedata.normalize("NFKC", text)
    strip_variant = _ZERO_WIDTH_RE.sub("", nfkc)
    space_variant = _ZERO_WIDTH_RE.sub(" ", nfkc)
    variants = (text, strip_variant, space_variant)
    seen: list[str] = []
    for v in variants:
        if v not in seen:
            seen.append(v)
    return tuple(seen)


def scan_text(text: str, *, mode: str | None = None) -> GuardrailResult:
    active_mode = (mode or DEFAULT_MODE or "enforce").lower()
    if active_mode == "off":
        return GuardrailResult(ok=True, mode=active_mode)
    for pattern in _INJECTION_PATTERNS:
        for variant in _normalize_for_scan(text):
            if pattern.search(variant):
                return GuardrailResult(
                    ok=(active_mode == "audit"),
                    mode=active_mode,
                    reason="REJECTED_SECURITY: blocked adversarial system instruction text.",
                    matched_pattern=pattern.pattern,
                )
    return GuardrailResult(ok=True, mode=active_mode)


def _flatten_texts(payload: Mapping[str, Any] | str) -> list[str]:
    """Parse `payload` into the flat list of text values guardrails should inspect.

    Shared by the regex pass and the NIM candidate list so the payload tree is
    walked exactly once per `assert_payload_safe_async` call, not twice.
    """
    if isinstance(payload, str):
        try:
            parsed = json.loads(payload)
        except json.JSONDecodeError:
            return [payload]
        if isinstance(parsed, Mapping):
            payload = parsed
        else:
            return [payload]
    return list(_iter_text_values(payload))


def scan_payload(payload: Mapping[str, Any] | str, *, mode: str | None = None) -> GuardrailResult:
    active_mode = (mode or DEFAULT_MODE or "enforce").lower()
    for text in _flatten_texts(payload):
        result = scan_text(text, mode=active_mode)
        if not result.ok:
            return result
    return GuardrailResult(ok=True, mode=active_mode)


def assert_payload_safe(payload: Mapping[str, Any] | str, *, mode: str | None = None) -> GuardrailResult:
    result = scan_payload(payload, mode=mode)
    if not result.ok:
        raise GuardrailBlocked(result)
    return result


async def assert_payload_safe_async(payload: Mapping[str, Any] | str, *, mode: str | None = None) -> GuardrailResult:
    """The firewall, for async callers. Raises GuardrailBlocked (a
    PermissionError subclass carrying the structured GuardrailResult) on a
    match."""
    active_mode = (mode or DEFAULT_MODE or "enforce").lower()
    for text in _flatten_texts(payload):
        result = scan_text(text, mode=active_mode)
        if not result.ok:
            raise GuardrailBlocked(result)
    return GuardrailResult(ok=True, mode=active_mode)
