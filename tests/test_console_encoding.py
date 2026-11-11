"""The console never forwards the caller's Accept-Encoding to the relay: behind a CDN that answers in br, a client
without a br decoder passed compressed bytes to the tab, which read them as broken JSON."""
from __future__ import annotations

from emporia.console import relay as console_relay


def test_accept_encoding_is_not_forwarded():
    assert "accept-encoding" in console_relay._DROP
    sent = {k: v for k, v in {"Accept-Encoding": "gzip, deflate, br", "Accept": "application/json"}.items()
            if k.lower() not in console_relay._DROP}
    assert sent == {"Accept": "application/json"}
