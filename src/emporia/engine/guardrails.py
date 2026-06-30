"""NemoClaw / NeMo Guardrails security firewall for Emporia.

Protects the relay from adversarial prompt-injection and exploit text before
the protocol kernel processes any turn. Anti-cheat (bot fingerprints, PoR
density) is handled separately in the relay.

If NVIDIA NeMo Guardrails is installed and configured, callers can extend this
module to invoke a Rails runtime. The default implementation is a deterministic
local firewall safe to run offline.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import asdict, dataclass
from typing import Any, Iterable, Mapping

DEFAULT_MODE = os.getenv("HERMES_PTGS_GUARDRAILS_MODE", "enforce").lower()

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

_TEXT_FIELDS = {
    "peer_text_rationale",
    "rationale",
    "reasoning",
    "message",
    "move",
    "turn",
    "uci_move",
    "state",
    "fen",
    "text",
    "content",
    "body",
}


@dataclass(frozen=True)
class GuardrailResult:
    ok: bool
    mode: str
    reason: str | None = None
    matched_pattern: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _iter_text_values(value: Any, *, parent_key: str | None = None) -> Iterable[str]:
    if isinstance(value, str):
        # Scan if no parent key context, or if parent key is a text field.
        # FIX: previously only scanned when parent_key was in _TEXT_FIELDS, allowing
        # nested payloads like {"body": {"message": "injection"}} to bypass the scan
        # when "body" was not in _TEXT_FIELDS. Now we recurse into nested dicts regardless
        # of parent key, scanning any value whose own key is a text field.
        if parent_key is None or parent_key in _TEXT_FIELDS:
            yield value
    elif isinstance(value, Mapping):
        for key, child in value.items():
            yield from _iter_text_values(child, parent_key=str(key))
    elif isinstance(value, list):
        for child in value:
            yield from _iter_text_values(child, parent_key=parent_key)


def scan_text(text: str, *, mode: str | None = None) -> GuardrailResult:
    active_mode = (mode or DEFAULT_MODE or "enforce").lower()
    if active_mode == "off":
        return GuardrailResult(ok=True, mode=active_mode)
    for pattern in _INJECTION_PATTERNS:
        if pattern.search(text):
            return GuardrailResult(
                ok=(active_mode == "audit"),
                mode=active_mode,
                reason="REJECTED_SECURITY: NemoClaw blocked adversarial system instruction text.",
                matched_pattern=pattern.pattern,
            )
    return GuardrailResult(ok=True, mode=active_mode)


def scan_payload(payload: Mapping[str, Any] | str, *, mode: str | None = None) -> GuardrailResult:
    active_mode = (mode or DEFAULT_MODE or "enforce").lower()
    if isinstance(payload, str):
        try:
            parsed = json.loads(payload)
        except json.JSONDecodeError:
            return scan_text(payload, mode=active_mode)
        if isinstance(parsed, Mapping):
            payload = parsed
        else:
            return scan_text(payload, mode=active_mode)

    for text in _iter_text_values(payload):
        result = scan_text(text, mode=active_mode)
        if not result.ok:
            return result
    return GuardrailResult(ok=True, mode=active_mode)


def assert_payload_safe(payload: Mapping[str, Any] | str, *, mode: str | None = None) -> GuardrailResult:
    result = scan_payload(payload, mode=mode)
    if not result.ok:
        raise PermissionError(result.reason or "REJECTED_SECURITY: NemoClaw blocked unsafe payload.")
    return result
