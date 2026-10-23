"""Which agents this machine holds keys for, and where (P126).

The console signs in to the relay as an agent and opens its DMs, so it needs
that agent's Ed25519 identity key and X25519 encryption key. They live in the
agent's own key directory — for Hermes, `<profiles>/<name>/home/.hermes/keys`
(P102); for any other system, wherever its EMPORIA_KEYS_DIR points.

A key directory can also hold stale copies of *other* agents' keys (a shared
directory from before P102), so an agent whose name matches a profile
directory uses that profile's keys first.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

_RESERVED_PREFIX = "_"          # _relay_identity.priv, _relay_jwt_secret.bin


@dataclass(frozen=True)
class LocalAgent:
    agent_id: str
    key_dir: Path

    @property
    def identity_key(self) -> Path:
        return self.key_dir / f"{_stem(self.agent_id)}.priv"

    @property
    def encryption_key(self) -> Path:
        return self.key_dir / f"{_stem(self.agent_id)}.x25519.priv"


def _stem(agent_id: str) -> str:
    from emporia.identity import _keyfile_stem
    return _keyfile_stem(agent_id)


def hermes_key_dirs(profiles_root: Path) -> list[Path]:
    return sorted(p for p in profiles_root.glob("*/home/.hermes/keys") if p.is_dir())


def discover(key_dirs: list[Path]) -> dict[str, LocalAgent]:
    """`{agent_id: LocalAgent}` for every identity key found. An agent whose
    name is the profile directory holding the keys wins over a copy elsewhere."""
    found: dict[str, LocalAgent] = {}
    for d in key_dirs:
        profile = d.parent.parent.parent.name if len(d.parts) >= 4 else ""
        for f in sorted(d.glob("*.priv")):
            if f.name.endswith(".x25519.priv") or f.name.startswith(_RESERVED_PREFIX):
                continue
            agent_id = f.name.removesuffix(".priv")
            own = agent_id == profile
            if agent_id not in found or own:
                found[agent_id] = LocalAgent(agent_id, d)
    return found


def load_identity_key(agent: LocalAgent):
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    return Ed25519PrivateKey.from_private_bytes(agent.identity_key.read_bytes())


def load_encryption_keys(agent: LocalAgent, others: list[Path]) -> list:
    """The agent's X25519 key, then any other copy on file. A message opens with
    whichever key it was sealed to; order only saves work."""
    from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
    paths = [agent.encryption_key] + [d / agent.encryption_key.name for d in others
                                      if d != agent.key_dir]
    keys = []
    for p in paths:
        if p.is_file():
            try:
                keys.append(X25519PrivateKey.from_private_bytes(p.read_bytes()))
            except ValueError:
                continue
    return keys
