"""Nous Research OAuth identity provider.

Verifies a Nous access JWT using JWKS (local crypto — relay never calls
Nous on the agent's behalf). Extracts `sub` as the subject_id and the
`display_name` from the token's claims if present.

Security properties:
- Token signature verified against Nous public key from JWKS endpoint
- `exp` claim checked automatically by PyJWT
- Raw token is never stored — only the verified sub/display_name
- JWKS keys cached for CACHE_TTL_SECONDS (keys rotate rarely)
- Works without any relay-side Nous credentials
"""

from __future__ import annotations

import time
from typing import Any

import httpx
import jwt

from emporia.identity_providers.base import (
    IdentityClaim,
    IdentityProvider,
    IdentityVerificationError,
)

OIDC_DISCOVERY = "https://portal.nousresearch.com/.well-known/openid-configuration"
JWKS_FALLBACK = "https://portal.nousresearch.com/.well-known/jwks.json"
ISSUER = "https://portal.nousresearch.com"
AUDIENCE = "hermes-cli:hermes-cli"
CACHE_TTL_SECONDS = 3600  # JWKS keys rotate infrequently


class _JwksCache:
    def __init__(self) -> None:
        self._keys: list[dict[str, Any]] = []
        self._fetched_at: float = 0.0

    def get(self) -> list[dict[str, Any]]:
        if time.time() - self._fetched_at < CACHE_TTL_SECONDS and self._keys:
            return self._keys
        try:
            disc = httpx.get(OIDC_DISCOVERY, timeout=5.0)
            if disc.status_code == 200:
                jwks_uri = disc.json().get("jwks_uri", JWKS_FALLBACK)
            else:
                jwks_uri = JWKS_FALLBACK
        except Exception:
            jwks_uri = JWKS_FALLBACK
        r = httpx.get(jwks_uri, timeout=5.0)
        r.raise_for_status()
        self._keys = r.json().get("keys", [])
        self._fetched_at = time.time()
        return self._keys

    def invalidate(self) -> None:
        self._fetched_at = 0.0


_cache = _JwksCache()


class NousIdentityProvider(IdentityProvider):
    PROVIDER_NAME = "nous"

    def verify(self, token: str) -> IdentityClaim:
        keys = _try_verify(token)
        subject_id = keys.get("sub", "")
        if not subject_id:
            raise IdentityVerificationError("Nous JWT missing sub claim")
        return IdentityClaim(
            provider="nous",
            subject_id=subject_id,
            display_name=keys.get("display_name") or keys.get("username") or "",
            email=keys.get("email", ""),
            org_id=keys.get("org_id", ""),
            raw_claims={k: v for k, v in keys.items()
                        if k not in ("sub", "display_name", "email", "org_id")},
        )


def _try_verify(token: str) -> dict[str, Any]:
    """Try JWKS verification; retry once on key-not-found (handles rotation)."""
    last_err: Exception = Exception("no keys")
    for attempt in range(2):
        keys = _cache.get()
        for key_data in keys:
            try:
                public_key = jwt.algorithms.RSAAlgorithm.from_jwk(key_data)
                payload = jwt.decode(
                    token,
                    public_key,
                    algorithms=["RS256"],
                    audience=AUDIENCE,
                    issuer=ISSUER,
                    options={"verify_exp": True},
                )
                return payload
            except jwt.exceptions.InvalidSignatureError:
                continue
            except jwt.exceptions.PyJWTError as e:
                last_err = e
                continue
        # Key not found in current JWKS — force refresh once
        if attempt == 0:
            _cache.invalidate()
    raise IdentityVerificationError(f"Nous JWT verification failed: {last_err}")
