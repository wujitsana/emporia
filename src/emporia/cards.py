"""The one switch for the card (Stripe) payment rail.

Cards are paused for this release (docs/payments.md, docs/security-log.md R2-7): the
relay opened a Stripe account per agent and, with a test key, confirmed entries itself.
The Stripe code stays, but nothing offers, enables or runs it unless ``EMPORIA_CARDS=on``.

Every "is Stripe configured" decision on the relay reads the key through
``stripe_secret_key()``, which returns "" while paused, so a relay with a key set
behaves exactly as if the key were unset.
"""

from __future__ import annotations

import logging
import os

PAUSED_LINE = "Cards (Stripe) are paused in this release — see docs/payments.md"
PAUSED_RELAY_MSG = "cards are paused on this relay"

_log = logging.getLogger("emporia.cards")
_warned = False


def cards_paused() -> bool:
    """True unless EMPORIA_CARDS=on is set."""
    return (os.environ.get("EMPORIA_CARDS") or "").strip().lower() not in ("on", "1", "true", "yes")


def stripe_secret_key() -> str:
    """The configured Stripe secret key, or "" while cards are paused."""
    global _warned
    key = os.environ.get("STRIPE_SECRET_KEY", "")
    if key.strip() and cards_paused():
        if not _warned:
            _warned = True
            _log.warning("cards_paused: STRIPE_SECRET_KEY is set but cards are paused "
                         "(EMPORIA_CARDS is not on); treating it as unset")
        return ""
    return key


def stripe_profile_id() -> str:
    """STRIPE_PROFILE_ID, or "" while cards are paused."""
    return "" if cards_paused() else (os.environ.get("STRIPE_PROFILE_ID", "") or "").strip()
