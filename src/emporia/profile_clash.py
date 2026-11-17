"""Two Hermes profiles on one machine that share an Emporia identity.

`hermes profile create NEW --clone-from SRC` copies SRC's `.env` (its EMPORIA_AGENT_ID and display name) and its key
folder, so NEW signs as SRC: two profiles, one agent, one key. The relay sees one agent whose two halves overwrite
each other's state, and either profile can act for the other. Setup and register refuse it; `emporia doctor` warns.

Only public things are compared: the agent ids, and the public half of each profile's identity key, derived in
memory from the key file in that profile's own key folder (`EMPORIA_KEYS_DIR`, else `<profile>/home/.hermes/keys`).
A private key is never printed, copied or returned.
"""

from __future__ import annotations

from pathlib import Path


def _env_of(profile: Path) -> dict[str, str]:
    from emporia import profile_env
    try:
        return profile_env.read(Path(profile) / ".env")
    except Exception:  # noqa: BLE001 — an unreadable profile has nothing to compare
        return {}


def key_dir_of(profile: Path, env: dict[str, str] | None = None) -> Path:
    env = _env_of(profile) if env is None else env
    return Path(env.get("EMPORIA_KEYS_DIR") or Path(profile) / "home" / ".hermes" / "keys").expanduser()


def public_key_of(profile: Path, agent_id: str, env: dict[str, str] | None = None) -> str:
    """The hex public key of `agent_id`'s identity key file in this profile's key folder, "" when there is none (or
    it is held by a signer or a key source, which are not files here)."""
    from emporia.identity import _keyfile_stem
    path = key_dir_of(profile, env) / f"{_keyfile_stem(agent_id)}.priv"
    try:
        raw = path.read_bytes() if path.stat().st_size == 32 else b""
        if len(raw) != 32:
            return ""
        from cryptography.hazmat.primitives.asymmetric import ed25519
        return ed25519.Ed25519PrivateKey.from_private_bytes(raw).public_key().public_bytes_raw().hex()
    except (OSError, ValueError):
        return ""


def clashes(profile: Path, agent_id: str = "") -> list[dict[str, str]]:
    """The other Hermes profiles under the same root whose Emporia agent id, or identity key, is this profile's:
    [{"profile", "same"}], `same` being `agent id`, `identity key` or `agent id and identity key`. `agent_id`: the
    id this profile is (about to be) set up as; else its own EMPORIA_AGENT_ID."""
    from emporia import hermes_layout
    profile = Path(profile)
    mine = _env_of(profile)
    me = (agent_id or mine.get("EMPORIA_AGENT_ID") or "").strip().lower()
    my_pub = public_key_of(profile, me, mine) if me else ""
    out: list[dict[str, str]] = []
    for other in hermes_layout.profiles(hermes_layout.root_of(profile)):
        if other.resolve() == profile.resolve():
            continue
        env = _env_of(other)
        theirs = (env.get("EMPORIA_AGENT_ID") or "").strip().lower()
        if not theirs:
            continue
        same = []
        if me and theirs == me:
            same.append("agent id")
        if my_pub and public_key_of(other, theirs, env) == my_pub:
            same.append("identity key")
        if same:
            out.append({"profile": hermes_layout.name_of(other), "same": " and ".join(same)})
    return out


def refusal(name: str, found: list[dict[str, str]]) -> str:
    """What setup and register say: who shares what, and the fix. Names profiles and agent ids only."""
    others = ", ".join(f"{c['profile']} (same {c['same']})" for c in found)
    only_key = all(c["same"] == "identity key" for c in found)
    fix = ("Give this one its own key: `emporia agent rotate-key " + name + "`." if only_key else
           f"Make a fresh profile instead (`hermes profile create NEW`, without --clone-from, then "
           f"`hermes emporia setup --agent-profile NEW`) and delete this one.")
    return (f"{name} shares its Emporia identity with {others}: a profile made with `--clone-from` copies the "
            f"other's agent id and key, so both would sign as one agent. Never clone an Emporia profile. {fix} "
            f"Nothing was changed.")
