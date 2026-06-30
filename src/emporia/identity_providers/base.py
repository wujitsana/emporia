"""Abstract base for pluggable identity providers.

Each provider receives a raw credential token and returns a verified
IdentityClaim — or raises IdentityVerificationError on failure.

To add a new provider:
  1. Subclass IdentityProvider
  2. Set PROVIDER_NAME (e.g. "github", "google")
  3. Implement verify(token) → IdentityClaim
  4. Register in registry.py PROVIDER_REGISTRY

The relay stores only the verified claim fields — never the raw token.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field


class IdentityVerificationError(Exception):
    """Raised when a provider cannot verify the supplied token."""


@dataclass
class IdentityClaim:
    """Verified identity assertion from a provider.

    provider:       short slug, e.g. "nous", "github"
    subject_id:     provider-scoped user ID (e.g. Nous sub, GitHub login)
    display_name:   human-readable handle from the provider
    email:          optional verified email
    org_id:         optional organisation / team ID
    raw_claims:     extra provider-specific fields (not persisted by default)
    """

    provider: str
    subject_id: str
    display_name: str = ""
    email: str = ""
    org_id: str = ""
    raw_claims: dict = field(default_factory=dict)

    def trust_tag(self) -> str:
        return f"✓ {self.provider}"


class IdentityProvider(ABC):
    PROVIDER_NAME: str = ""

    @abstractmethod
    def verify(self, token: str) -> IdentityClaim:
        """Verify the token and return a claim. Raises IdentityVerificationError on failure."""
