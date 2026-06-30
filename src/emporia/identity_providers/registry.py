"""Identity provider registry.

Add new providers here. The relay calls verify_claim(provider, token)
without knowing which provider class handles it.

PROVIDER_REGISTRY maps provider name → provider instance. Adding a new
provider (e.g. GitHub OAuth) is: instantiate, add to the dict.
"""

from __future__ import annotations

from emporia.identity_providers.base import (
    IdentityClaim,
    IdentityProvider,
    IdentityVerificationError,
)
from emporia.identity_providers.nous import NousIdentityProvider

PROVIDER_REGISTRY: dict[str, IdentityProvider] = {
    "nous": NousIdentityProvider(),
    # "github": GitHubIdentityProvider(),  ← add future providers here
}


def verify_claim(provider: str, token: str) -> IdentityClaim:
    """Verify a token with the named provider. Raises IdentityVerificationError on failure."""
    p = PROVIDER_REGISTRY.get(provider)
    if p is None:
        raise IdentityVerificationError(
            f"Unknown identity provider: {provider!r}. "
            f"Supported: {sorted(PROVIDER_REGISTRY)}"
        )
    return p.verify(token)


def supported_providers() -> list[str]:
    return sorted(PROVIDER_REGISTRY.keys())
