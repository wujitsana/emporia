"""Regex denial of service: every pattern the relay runs over user text stays near-linear.

The scanners run on the event loop and CPython's `re` holds the GIL, so one quadratic pattern
freezes every other request. Each pattern is timed on adversarial 32 KiB inputs.
"""

from __future__ import annotations

import re
import time

import pytest

from emporia import secret_guard, wire
from emporia.engine import guardrails

N = 32 * 1024
secret_guard._wordlist()  # one-time lazy load, not a pattern cost
BUDGET = 0.25
WHOLE_BUDGET = 1.0

INPUTS = {
    "newlines": "\n" * N,
    "spaces": " " * N,
    "dots": "a." * (N // 2),
    "comment": "<!--" * (N // 4),
    "jwt": "-eyJaaaaaaaa" * (N // 12),
    "tabs": "\t" * N,
    "letters": "a" * N,
    "cat_spaces": "cat" + " " * N,
    "ignore_spaces": "ignore" + " " * N,
    "cat_lines": "cat\n" * (N // 4),
    "env_lines": "TOKEN\n" * (N // 6),
    "quest_lines": "\n  quest:" * (N // 9),
    "brackets": "[" + "1 " * (N // 2),
    "hyphens": "a-" * (N // 2),
    "word_dots": "word." * (N // 5),
}


def _patterns() -> list[tuple[str, re.Pattern[str]]]:
    out = [(f"inj:{p.pattern[:40]}", p) for p in guardrails._INJECTION_PATTERNS]
    out += [(f"secret:{k}", p) for k, p in secret_guard._PATTERNS]
    out += [(f"wire:{n}", getattr(wire, n)) for n in dir(wire) if n.startswith("_URL_")]
    out += [("guard:html", guardrails._HTML_TAG_RE), ("guard:b64", guardrails._B64_RE),
            ("secret:base58", secret_guard._BASE58_RUN)]
    from emporia import quest_engine
    out.append(("quest:line", quest_engine._QUEST_LINE))
    from emporia import labs
    out.append(("labs:line", labs._LINE))
    return out


@pytest.mark.parametrize("name,pattern", _patterns(), ids=lambda v: v if isinstance(v, str) else "")
@pytest.mark.parametrize("kind", sorted(INPUTS))
def test_pattern_is_fast(name, pattern, kind):
    text = INPUTS[kind]
    start = time.perf_counter()
    pattern.search(text)
    pattern.sub("", text)
    took = time.perf_counter() - start
    assert took < BUDGET, f"{name} on {kind}: {took:.2f}s"


@pytest.mark.parametrize("kind", sorted(INPUTS))
def test_whole_scans_are_fast(kind):
    text = INPUTS[kind]
    start = time.perf_counter()
    guardrails.scan_text(text, mode="enforce")
    guardrails.credential_in(text)
    wire.has_url(text)
    secret_guard.find(text)
    took = time.perf_counter() - start
    assert took < WHOLE_BUDGET, f"{kind}: {took:.2f}s"


def test_normalize_runs_once_per_text(monkeypatch):
    calls = []
    real = guardrails._normalize_for_scan
    monkeypatch.setattr(guardrails, "_normalize_for_scan", lambda t: calls.append(1) or real(t))
    guardrails.scan_text("hello world")
    assert len(calls) == 1


@pytest.mark.parametrize("text,expected", [
    ("see example.com/x", True), ("a.b.example.org/path", True), ("EXAMPLE.COM/", True),
    ("mail me@example.com/x", False), ("no link here", False), ("v1.2.3 released", False),
    ("x-ray.co.uk/page", True), ("see www.example.com", True), ("https://a.b", True),
])
def test_has_url_unchanged_on_real_text(text, expected):
    assert wire.has_url(text) is expected


def test_labs_claim_line_unchanged():
    from emporia import labs
    m = labs._LINE.search("intro\n  Emporia:  alice@relay.example  AbC_123-xyz  \nbye")
    assert m and m.group(1) == "alice@relay.example" and m.group(2) == "AbC_123-xyz"
