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
    # P164: keys linked to a password manager or env var instead of files.
    identity_source: str = ""
    encryption_source: str = ""

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


def _profile_env(key_dir: Path) -> dict[str, str]:
    """The profile .env beside a Hermes keys directory (<profile>/home/.hermes/keys)."""
    env_file = key_dir.parent.parent.parent / ".env"
    out: dict[str, str] = {}
    if len(key_dir.parts) >= 4 and env_file.is_file():
        for line in env_file.read_text(errors="replace").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def discover(key_dirs: list[Path]) -> dict[str, LocalAgent]:
    """`{agent_id: LocalAgent}` for every identity key found. An agent whose
    name is the profile directory holding the keys wins over a copy elsewhere."""
    from emporia.identity import ENCRYPTION_KEY_SOURCE_VAR, IDENTITY_KEY_SOURCE_VAR
    found: dict[str, LocalAgent] = {}
    for d in key_dirs:
        profile = d.parent.parent.parent.name if len(d.parts) >= 4 else ""
        env = _profile_env(d)
        if profile and env.get(IDENTITY_KEY_SOURCE_VAR):
            agent_id = env.get("EMPORIA_AGENT_ID") or profile
            found[agent_id] = LocalAgent(agent_id, d, env[IDENTITY_KEY_SOURCE_VAR],
                                         env.get(ENCRYPTION_KEY_SOURCE_VAR, ""))
        for f in sorted(d.glob("*.priv")):
            if f.name.endswith(".x25519.priv") or f.name.startswith(_RESERVED_PREFIX):
                continue
            agent_id = f.name.removesuffix(".priv")
            own = agent_id == profile
            if (agent_id not in found or own) and not (found.get(agent_id) and found[agent_id].identity_source):
                found[agent_id] = LocalAgent(agent_id, d, "", env.get(ENCRYPTION_KEY_SOURCE_VAR, "") if own else "")
    return found


def load_identity_key(agent: LocalAgent):
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    if agent.identity_source:
        from emporia.key_source import key32
        return Ed25519PrivateKey.from_private_bytes(key32(agent.identity_source, _profile_env(agent.key_dir)))
    return Ed25519PrivateKey.from_private_bytes(agent.identity_key.read_bytes())


def load_encryption_keys(agent: LocalAgent, others: list[Path]) -> list:
    """The agent's X25519 key, then any other copy on file. A message opens with
    whichever key it was sealed to; order only saves work."""
    from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
    paths = [agent.encryption_key] + [d / agent.encryption_key.name for d in others
                                      if d != agent.key_dir]
    keys = []
    if agent.encryption_source:
        from emporia.key_source import KeySourceError, key32
        try:
            keys.append(X25519PrivateKey.from_private_bytes(
                key32(agent.encryption_source, _profile_env(agent.key_dir))))
        except (KeySourceError, ValueError):
            pass
    for p in paths:
        if p.is_file():
            try:
                keys.append(X25519PrivateKey.from_private_bytes(p.read_bytes()))
            except ValueError:
                continue
    return keys
