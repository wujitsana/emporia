"""Two Hermes profiles on one machine that share an Emporia identity.

`hermes profile create NEW --clone-from SRC` copies SRC's `.env` (its EMPORIA_AGENT_ID and display name) and its key
folder, so NEW signs as SRC: two profiles, one agent. The relay sees one agent whose two halves overwrite each
other's state, and either profile can act for the other. Setup and register refuse it; `emporia doctor` warns.

Two profiles clash when they have the same agent id, or when their key folders (`EMPORIA_KEYS_DIR`, else
`<profile>/home/.hermes/keys`) resolve to one path. Only agent ids and folder paths are compared: no key file of
any profile is opened.
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
    return Path(env.get("EMPORIA_KEYS_DIR") or Path(profile) / "home" / ".hermes" / "keys").expanduser().resolve()


def clashes(profile: Path, agent_id: str = "") -> list[dict[str, str]]:
    """The other Hermes profiles under the same root whose Emporia agent id is this profile's, or whose key folder is
    the same folder: [{"profile", "same"}], `same` being `agent id`, `key folder` or `agent id and key folder`.
    `agent_id`: the id this profile is (about to be) set up as; else its own EMPORIA_AGENT_ID."""
    from emporia import hermes_layout
    profile = Path(profile)
    mine = _env_of(profile)
    me = (agent_id or mine.get("EMPORIA_AGENT_ID") or "").strip().lower()
    my_dir = key_dir_of(profile, mine)
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
        if key_dir_of(other, env) == my_dir:
            same.append("key folder")
        if same:
            out.append({"profile": hermes_layout.name_of(other), "same": " and ".join(same)})
    return out


def refusal(name: str, found: list[dict[str, str]]) -> str:
    """What setup and register say: who shares what, and the fix. Names profiles only."""
    others = ", ".join(f"{c['profile']} (same {c['same']})" for c in found)
    return (f"{name} shares its Emporia identity with {others}: a profile made with `--clone-from` copies the "
            f"other's agent id and keys, so both would sign as one agent. Never clone an Emporia profile. Make a "
            f"fresh profile instead (`hermes profile create NEW`, without --clone-from, then "
            f"`hermes emporia setup --agent-profile NEW`) and delete this one. Nothing was changed.")
