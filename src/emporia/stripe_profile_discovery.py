"""Best-effort STRIPE_PROFILE_ID discovery when only STRIPE_SECRET_KEY is configured.

Stripe Machine Payments profile IDs (profile_… / profile_test_…) are normally
created in the Dashboard. Listing them via API requires a secret key with
account read scope; restricted keys (rk_…) cannot retrieve them.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import httpx

_PROFILE_ID_RE = re.compile(r"\b(profile(?:_test)?_[A-Za-z0-9]+)\b")


def _valid_profile_id(value: str | None) -> bool:
    if not value:
        return False
    v = value.strip()
    return v.startswith("profile_") or v.startswith("profile_test_")


def find_profile_id_in_data(obj: Any) -> str | None:
    """Walk JSON-like structures for the first valid Machine Payments profile id."""
    if isinstance(obj, str):
        m = _PROFILE_ID_RE.search(obj)
        if m and _valid_profile_id(m.group(1)):
            return m.group(1)
        return None
    if isinstance(obj, dict):
        for key in ("id", "profile_id", "payment_profile_id", "machine_payment_profile_id"):
            val = obj.get(key)
            if isinstance(val, str) and _valid_profile_id(val):
                return val.strip()
        for v in obj.values():
            found = find_profile_id_in_data(v)
            if found:
                return found
    elif isinstance(obj, list):
        for item in obj:
            found = find_profile_id_in_data(item)
            if found:
                return found
    return None


def discover_stripe_profile_id_from_api(
    secret_key: str,
    *,
    api_version: str = "2026-04-22.preview",
    timeout: float = 20.0,
) -> str | None:
    """Try Stripe API endpoints that may embed a profile id (sk_* keys only)."""
    key = (secret_key or "").strip()
    if not key or key.startswith("rk_"):
        return None

    versions = [api_version, "2026-04-22.preview", "2026-03-04.preview", "2025-09-30.preview"]
    seen: set[str] = set()
    for ver in versions:
        ver = (ver or "").strip()
        if not ver or ver in seen:
            continue
        seen.add(ver)
        headers = {
            "Authorization": f"Bearer {key}",
            "Stripe-Version": ver,
        }
        try:
            r = httpx.get(
                "https://api.stripe.com/v1/account",
                headers=headers,
                timeout=timeout,
            )
            if r.status_code != 200:
                continue
            found = find_profile_id_in_data(r.json())
            if found:
                return found
        except Exception:
            continue
    return None


def stripe_mpp_admin_notice(secret_key: str, *, profile_ready: bool) -> str | None:
    """
    Operator-facing message when Stripe secret is configured but MPP seller profile is not.
    Never includes secret values.
    """
    key = (secret_key or "").strip()
    if profile_ready or not key:
        return None
    if key.startswith("rk_"):
        return (
            "ADMIN: STRIPE_SECRET_KEY is set but STRIPE_PROFILE_ID is missing — the MPP payment "
            "rail is OFF (only free + stripe_pi). Restricted keys (rk_*) cannot auto-fetch a "
            "Machine Payment profile from Stripe. Copy profile_… or profile_test_… from "
            "Stripe Dashboard → Machine payments / Agentic commerce, add STRIPE_PROFILE_ID to "
            "the Hermes profile .env, run: emporia installer --install-profile, then restart "
            "the relay."
        )
    return (
        "ADMIN: STRIPE_SECRET_KEY is set but STRIPE_PROFILE_ID is missing — the MPP payment "
        "rail is OFF (only free + stripe_pi). Auto-discovery checked env files and Stripe "
        "Account API but found no profile id. Set STRIPE_PROFILE_ID=profile_… in profile .env "
        "(Dashboard → Machine payments), run install-profile, restart relay."
    )


def scan_stripe_profile_id_in_tree(
    profile_dir: Path,
    *,
    extra_roots: tuple[Path, ...] = (),
) -> str | None:
    """Find STRIPE_PROFILE_ID= in profile .env, config env, and ancestor .env files."""
    profile_dir = profile_dir.resolve()
    paths: list[Path] = [
        profile_dir / ".env",
        profile_dir / "emporia" / ".env",
    ]
    current = profile_dir.parent
    for _ in range(14):
        if current == current.parent:
            break
        paths.append(current / ".env")
        current = current.parent
    for root in extra_roots:
        paths.append(root / ".env")

    for path in paths:
        if not path.is_file():
            continue
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            if k.strip() != "STRIPE_PROFILE_ID":
                continue
            val = v.strip().strip('"').strip("'")
            if _valid_profile_id(val):
                return val
    return None


def resolve_stripe_profile_id(
    secret_key: str | None,
    profile_dir: Path | None,
    *,
    explicit: str | None = None,
    api_version: str = "2026-04-22.preview",
) -> tuple[str | None, str]:
    """
    Return (profile_id, note) for installer logging.
    note is a short human-readable provenance / failure hint (no secrets).
    """
    if explicit and _valid_profile_id(explicit):
        return explicit.strip(), "cli"

    if explicit and explicit.strip():
        return None, "invalid_cli"

    if profile_dir:
        from_file = scan_stripe_profile_id_in_tree(profile_dir)
        if from_file:
            return from_file, "env_file"

    if secret_key and secret_key.strip():
        from_api = discover_stripe_profile_id_from_api(secret_key.strip(), api_version=api_version)
        if from_api:
            return from_api, "stripe_api"

    if secret_key and secret_key.strip().startswith("rk_"):
        return None, "restricted_key"
    if secret_key:
        return None, "not_found"
    return None, "no_stripe_key"