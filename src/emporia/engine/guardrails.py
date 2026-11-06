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

import base64
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
        r"(disregard|forget|override)\s+(all\s+|any\s+|everything\s+)?(of\s+)?(the\s+|your\s+|these\s+)?"
        r"(previous|prior|above|earlier|safety|system)\s+(instructions|rules|messages|guidelines|prompt)",
        r"new\s+instructions\s*:|updated\s+instructions\s*:",
        r"<\|?(im_start|im_end|system|endoftext)\|?>|\[/?INST\]|<</?SYS>>",
        r"(read|cat|open|print|send|upload|show|reveal)\s+[^\n]{0,40}(~/\.ssh|\.hermes/keys|\.hermes/\.env|id_ed25519|private[_\s-]?key|seed\s+phrase|mnemonic)",
        r"(send|transfer|pay|wire)\s+[^\n]{0,60}\bto\s+0x[0-9a-f]{20,}",
        r"(run|execute|invoke)\s+(the\s+)?(terminal|shell|bash|python)\s+(tool|command)",
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
# invisible, so NFKC alone leaves them untouched. ZWSP, ZWNJ, ZWJ, LRM/RLM, bidi
# controls, word joiner and invisible operators, soft hyphen, Mongolian vowel
# separator, BOM, variation selectors and the Unicode "tag" block (invisible ASCII).
_ZERO_WIDTH_RE = re.compile(
    "[\u200b-\u200f\u202a-\u202e\u2060-\u2064\u2066-\u2069\u00ad\u180e\ufeff"
    "\ufe00-\ufe0f\U000e0000-\U000e007f]")
_HTML_TAG_RE = re.compile(r"<[^>]{0,200}>|<!--.*?-->", re.DOTALL)
_B64_RE = re.compile(r"[A-Za-z0-9+/_-]{16,2730}={0,2}")
#: Cyrillic and Greek letters that read as Latin ones.
_HOMOGLYPHS = str.maketrans({
    "\u0430": "a", "\u0435": "e", "\u043e": "o", "\u0440": "p", "\u0441": "c", "\u0443": "y",
    "\u0445": "x", "\u0456": "i", "\u0458": "j", "\u0455": "s", "\u0501": "d", "\u04bb": "h",
    "\u0391": "A", "\u0392": "B", "\u0395": "E", "\u0397": "H", "\u0399": "I", "\u039a": "K",
    "\u039c": "M", "\u039d": "N", "\u039f": "O", "\u03a1": "P", "\u03a4": "T", "\u03a7": "X",
    "\u03b1": "a", "\u03b5": "e", "\u03b9": "i", "\u03ba": "k", "\u03bd": "v", "\u03bf": "o",
    "\u03c1": "p", "\u03c4": "t", "\u03c5": "u", "\u0410": "A", "\u0412": "B", "\u0415": "E",
    "\u041a": "K", "\u041c": "M", "\u041d": "H", "\u041e": "O", "\u0420": "P", "\u0421": "C",
    "\u0422": "T", "\u0425": "X", "\u0406": "I",
})


_TAG_BLOCK_RE = re.compile("[\U000e0020-\U000e007e]+")


def _untagged_ascii(text: str) -> str:
    """ASCII smuggled in the invisible Unicode tag block (U+E0020..E007E), made visible."""
    return _TAG_BLOCK_RE.sub(lambda m: " " + "".join(chr(ord(c) - 0xE0000) for c in m.group(0)) + " ", text)


def _decoded_blobs(text: str) -> list[str]:
    """Readable text hidden in short base64 blobs. Only blobs that decode to mostly letters count."""
    out: list[str] = []
    for blob in _B64_RE.findall(text)[:8]:
        raw = blob.replace("-", "+").replace("_", "/")
        try:
            decoded = base64.b64decode(raw + "=" * (-len(raw) % 4), validate=True).decode("utf-8")
        except Exception:
            continue
        if len(decoded) >= 8 and sum(c.isalpha() or c.isspace() for c in decoded) >= 0.8 * len(decoded):
            out.append(decoded)
    return out


def _normalize_for_scan(text: str) -> tuple[str, ...]:
    """Obfuscation defences for the deterministic regex layer; never mutates `text`.

    Variants every pattern is tried against: the raw text; NFKC (full-width letters, NBSP and
    other compatibility forms) with invisible characters stripped, and with them turned into
    spaces (`igno<ZWSP>re` needs the strip, `ignore<ZWSP>all` the space); the same with HTML
    tags/comments removed; the same with Cyrillic/Greek look-alikes mapped to Latin; and the
    text of any short base64 blob that decodes to readable words.
    """
    nfkc = unicodedata.normalize("NFKC", _untagged_ascii(text))
    strip_variant = _ZERO_WIDTH_RE.sub("", nfkc)
    space_variant = _ZERO_WIDTH_RE.sub(" ", nfkc)
    variants = [text, strip_variant, space_variant]
    untagged = _HTML_TAG_RE.sub("", strip_variant)
    if untagged != strip_variant:
        variants += [untagged, _HTML_TAG_RE.sub(" ", space_variant)]
    for v in list(variants):
        folded = v.translate(_HOMOGLYPHS)
        if folded != v:
            variants.append(folded)
    for v in (strip_variant, untagged):
        variants += _decoded_blobs(v)
    seen: list[str] = []
    for v in variants:
        if v not in seen:
            seen.append(v)
    return tuple(seen)


#: A backstop to the client's secret guard: text that carries a credential (a GitHub token, an API key, a private
#: key, a JWT, ...) never reaches a thread, a post or a report, whoever sent it. The shapes are the guard's own.
from emporia.secret_guard import _PATTERNS as _SECRET_PATTERNS  # noqa: E402

_CREDENTIAL_PATTERNS = tuple(p for _, p in _SECRET_PATTERNS)


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


def credential_in(text: str) -> bool:
    """Whether text carries a credential shape (the secret guard's own). Checked where the relay stores what an
    agent wrote, not when a machine flags text it received, so an example in ordinary talk is only flagged on send."""
    return any(p.search(text) for p in _CREDENTIAL_PATTERNS)


def _credential_result(mode: str) -> GuardrailResult:
    return GuardrailResult(ok=(mode == "audit"), mode=mode, reason="REJECTED_SECURITY: looks like a credential; not sent.",
                           matched_pattern="credential")


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
    if (mode or DEFAULT_MODE or "enforce").lower() != "off" and any(credential_in(t) for t in _flatten_texts(payload)):
        raise GuardrailBlocked(_credential_result((mode or DEFAULT_MODE or "enforce").lower()))
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
        if active_mode != "off" and credential_in(text) and active_mode != "audit":
            raise GuardrailBlocked(_credential_result(active_mode))
    return GuardrailResult(ok=True, mode=active_mode)


INJECTION_FLAG = "injection-pattern"


def injection_flags(value: Any) -> list[str]:
    """Flags for text another agent wrote, after this machine opened it: `["injection-pattern"]` when the
    same firewall the relay runs over plaintext matches, else `[]`. Never raises; the caller keeps the item."""
    try:
        payload = value if isinstance(value, (str, Mapping)) else {"v": value}
        return [] if scan_payload(payload, mode="enforce").ok else [INJECTION_FLAG]
    except Exception:
        return []
