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

    @property
    def in_signer(self) -> bool:
        """P293: its keys are held by its signer; the profile has only their public halves."""
        return (self.key_dir / f"{_stem(self.agent_id)}.signer.pub").is_file()


def _signer_client(agent: LocalAgent):
    """The agent's own signer, as its profile names it — so the console asks it,
    as the agent would, and never holds a key."""
    from emporia import signer_client
    env = _profile_env(agent.key_dir)
    if not env.get("EMPORIA_SIGNER_URL") or not env.get("EMPORIA_SIGNER_TOKEN"):
        raise RuntimeError(f"{agent.agent_id}'s keys are in its signer, but its profile does not say where")
    return signer_client.Client(env["EMPORIA_SIGNER_URL"], env["EMPORIA_SIGNER_TOKEN"],
                                env.get("EMPORIA_AGENT_ID") or agent.agent_id)


class _SignsInThroughItsSigner:
    """Stands in for the identity key where only a sign-in is ever signed."""

    def __init__(self, agent: LocalAgent):
        self.agent = agent

    def sign(self, data: bytes) -> bytes:
        raise RuntimeError("the console signs nothing but a dashboard sign-in through an agent's signer")


def dashboard_sign_in(agent: LocalAgent, nonce: str, relay_url: str) -> str:
    """Review follow-up M4: the agent's signature (base64url) over a dashboard sign-in for
    `relay_url` — its own key's, or its signer's (which applies its person's policy)."""
    from emporia import identity, wire
    payload = wire.signed_dashboard_sign_in_payload(nonce, relay_url)
    if why := wire.dashboard_sign_in_problem(payload["nonce"], payload["relay"]):
        raise ValueError(f"not signed: {why}")
    key = load_identity_key(agent)
    if isinstance(key, _SignsInThroughItsSigner):
        return _signer_client(agent).sign("dashboard-sign-in", [nonce, relay_url], {})
    return identity.sign_payload(key, payload)


def _stem(agent_id: str) -> str:
    from emporia.identity import _keyfile_stem
    return _keyfile_stem(agent_id)


def _profile_env(key_dir: Path) -> dict[str, str]:
    """The profile .env beside a Hermes keys directory (<profile>/home/.hermes/keys).
    Catalog rule 11: read through Hermes (emporia.profile_env), never opened here."""
    from emporia import profile_env
    env_file = key_dir.parent.parent.parent / ".env"
    if len(key_dir.parts) >= 4 and env_file.is_file():
        return profile_env.read(env_file)
    return {}


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
        held = [(f, f.name.removesuffix(".signer.pub")) for f in sorted(d.glob("*.signer.pub"))
                if not f.name.endswith(".x25519.signer.pub")]      # P293: keys in its signer
        for f, agent_id in [(f, f.name.removesuffix(".priv")) for f in sorted(d.glob("*.priv"))] + held:
            if f.name.endswith(".x25519.priv") or f.name.startswith(_RESERVED_PREFIX):
                continue
            own = agent_id == profile
            if (agent_id not in found or own) and not (found.get(agent_id) and found[agent_id].identity_source):
                found[agent_id] = LocalAgent(agent_id, d, "", env.get(ENCRYPTION_KEY_SOURCE_VAR, "") if own else "")
    return found


def load_identity_key(agent: LocalAgent):
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    if agent.in_signer:
        return _SignsInThroughItsSigner(agent)
    if agent.identity_source:
        from emporia.key_source import key32
        return Ed25519PrivateKey.from_private_bytes(key32(agent.identity_source, _profile_env(agent.key_dir)))
    return Ed25519PrivateKey.from_private_bytes(agent.identity_key.read_bytes())


def load_encryption_keys(agent: LocalAgent, others: list[Path]) -> list:
    """The agent's X25519 key, then any other copy on file. A message opens with
    whichever key it was sealed to; order only saves work. Behind a signer (P293),
    its opener: the signer opens each message, and no key comes here."""
    from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
    if (agent.key_dir / f"{_stem(agent.agent_id)}.x25519.signer.pub").is_file():
        from emporia import signer_client
        return [signer_client.Opener(_signer_client(agent))]
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
    # P271: the keys it replaced, for messages sealed before
    from emporia.identity import retired_encryption_key_hexes
    for d in [agent.key_dir] + [d for d in others if d != agent.key_dir]:
        for h in retired_encryption_key_hexes(agent.agent_id, env=_profile_env(d), key_dir=d):
            keys.append(X25519PrivateKey.from_private_bytes(bytes.fromhex(h)))
    return keys
