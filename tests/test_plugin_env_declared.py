"""plugin.yaml declares every person-facing setting, so Hermes's setup and dashboard show it with its meaning, and
every secret as `password: true`. Relay-operator settings (the relay's, not an agent profile's) stay out."""
import re
from pathlib import Path

import yaml

from emporia import agent_env, playbook
from emporia.key_source import ENV_KEY_NAMES, SOURCE_VARS, STORAGE_VAR

REPO = Path(__file__).resolve().parent.parent
MANIFEST = yaml.safe_load((REPO / "hermes_plugin" / "emporia" / "plugin.yaml").read_text())
DECLARED = {e["name"]: e for e in MANIFEST["optional_env"]}

# Not an agent profile's: the relay's own settings, per-process plumbing, or names no person sets.
NOT_DECLARED = {
    "EMPORIA_NO_DOTENV",          # setup/host plumbing: which .env the process may read
    "EMPORIA_HANDED_NAMES",       # the bridge lists the names it was handed
    "EMPORIA_RELAY_HOST",         # `emporia relay host`: a relay's, person-only in the key guard
    "EMPORIA_CLI",                # the key guard's own pattern for the runtime's command
    "EMPORIA_SIGNER_DIR",         # the signer's own folder, not the agent's
    "EMPORIA_ROUNDS",             # rounds are a Hermes cron job (`emporia rounds`), not a setting
}


def _key_guard_source() -> str:
    return (REPO / "hermes_plugin" / "emporia" / "key_guard.py").read_text()


def test_every_name_the_key_guard_holds_for_the_person_is_declared():
    src = _key_guard_source()
    names = set(re.findall(r'"(EMPORIA_[A-Z0-9_]+)(?:\\\\w\*)?"', src))
    names |= set(re.findall(r"\b(EMPORIA_[A-Z0-9_]+)\b(?!_)", src.split("_MAINNET_SETTING =")[1].split("def _setting_written")[0]))
    # `EMPORIA_PLAYBOOK_AUTONOMOUS` is a prefix: the declared name behind it is checked by the pattern test below.
    wanted = {n for n in names if not n.endswith("_") and n not in NOT_DECLARED and n != "EMPORIA_PLAYBOOK_AUTONOMOUS"}
    assert {"EMPORIA_WAKE", "EMPORIA_FEATURES", "EMPORIA_X_LINK", "EMPORIA_X402_MAINNET", "EMPORIA_PLAYBOOK",
            "EMPORIA_SIGN_LIMITS", "EMPORIA_UNTRACKED_TURNS", "EMPORIA_CLIENT"} <= wanted, wanted
    assert not (wanted - set(DECLARED)), sorted(wanted - set(DECLARED))


def test_the_person_setting_patterns_each_match_a_declared_name():
    """`EMPORIA_CLIENT\\w*` and the like: a pattern with no declared name behind it is a setting nobody can see."""
    src = _key_guard_source()
    block = src.split("_PERSON_SETTINGS = (")[1].split(")")[0]
    for pattern in (p.replace("\\\\", "\\") for p in re.findall(r'"([^"]+)"', block)):
        if pattern.startswith("EMPORIA_ROUNDS"):
            continue
        assert any(re.fullmatch(pattern, n) for n in DECLARED), pattern


def test_every_name_setup_writes_is_declared():
    written = set(ENV_KEY_NAMES.values()) | set(SOURCE_VARS.values()) | {STORAGE_VAR}
    written |= set(playbook.SETTINGS) | {"EMPORIA_PLAYBOOK", playbook.GAMES_BY}
    written |= {"EMPORIA_RELAY_URL", "EMPORIA_AGENT_ID", "EMPORIA_PYTHON", "EMPORIA_DOTENV", "EMPORIA_PROFILE_ID",
                "EMPORIA_ALLOWED_USERS", "EMPORIA_DISPLAY_NAME", "EMPORIA_KEYS_DIR", "EMPORIA_OPERATOR_LABEL",
                "EMPORIA_ABOUT", "EMPORIA_CLIENT", "EMPORIA_CLIENT_VERSION",
                "EMPORIA_ENCRYPTION_RETIRED_KEYS", "EMPORIA_SIGNER_TOKEN", "EMPORIA_BACKUP_RELAYS",
                "EMPORIA_VISIT_POLICY", "EMPORIA_VISIT_RELAYS", "EMPORIA_VISIT_DENY", "EMPORIA_PUBLISH_VISITS"}
    from emporia import setup_choices
    written |= set(setup_choices.FIXED)
    from emporia import features, wake
    written |= {features.SETTING, wake.SETTING}
    assert not (written - set(DECLARED)), sorted(written - set(DECLARED))


def test_secrets_are_password_fields_and_settings_are_not():
    entries = MANIFEST["optional_env"]
    secrets = {e["name"] for e in entries if e.get("password") is True}
    assert set(ENV_KEY_NAMES.values()) | {"EMPORIA_SIGNER_TOKEN", "EMPORIA_ENCRYPTION_RETIRED_KEYS"} <= secrets
    for name in set(SOURCE_VARS.values()) | {STORAGE_VAR, "EMPORIA_FEATURES", "EMPORIA_WAKE", "EMPORIA_X_LINK"}:
        assert DECLARED[name]["password"] is False, name       # a reference or a switch, never the key itself
    for e in entries:
        assert e.get("description") and e.get("prompt") and isinstance(e.get("password"), bool), e["name"]
        assert re.fullmatch(r"[A-Z][A-Z0-9_]+", e["name"])
    assert len(DECLARED) == len(entries), "a name declared twice"


def test_what_is_declared_is_something_the_runtime_hands_or_the_gateway_reads():
    """No name that nothing reads: declared names are the runtime's (agent_env) or the gateway adapter's."""
    known = agent_env.known() | set(agent_env.GATEWAY_ONLY) | set(agent_env.LITERAL_IN_ENTRY) | set(agent_env.VAULT_NAMES)
    assert not (set(DECLARED) - known), sorted(set(DECLARED) - known)


def test_relay_operator_settings_stay_out():
    for name in ("EMPORIA_RELAY_HOST", "EMPORIA_RELAY_PORT", "EMPORIA_FEE_ADDRESS", "EMPORIA_ESCROW_CONTRACT",
                 "EMPORIA_RELAY_ACCESS", "EMPORIA_RELAY_MEMBERS", "EMPORIA_SEASON", "EMPORIA_GAMIFY", "EMPORIA_QUESTS"):
        assert name not in DECLARED, name
