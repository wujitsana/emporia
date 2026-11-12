"""The Hermes route, for real. tests/conftest.py points EMPORIA_HERMES_BIN at a path that does not exist, so no
other test runs `hermes`; these do, in a throwaway root with profiles and a sticky `active_profile` that is not the
profile written. They skip cleanly where Hermes is not installed."""
import shutil
import stat
from pathlib import Path

import pytest

from emporia import cli, hermes_settings, profile_env

HERMES = shutil.which("hermes") or ("/opt/hermes/.venv/bin/hermes" if Path("/opt/hermes/.venv/bin/hermes").exists() else "")
pytestmark = pytest.mark.skipif(not HERMES or not Path("/opt/hermes/.venv/bin/python").exists(),
                                reason="Hermes is not installed here")


@pytest.fixture
def root(tmp_path, monkeypatch):
    """<root>/profiles/zed and /other, <root>/active_profile = other; HOME and HERMES_HOME are throwaway."""
    r = tmp_path / "root"
    for n in ("zed", "other"):
        (r / "profiles" / n).mkdir(parents=True)
        (r / "profiles" / n / "config.yaml").write_text("model: {}\n")
    (r / "config.yaml").write_text("model: {}\n")
    (r / "active_profile").write_text("other\n")
    monkeypatch.setenv("EMPORIA_HERMES_BIN", HERMES)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("HERMES_HOME", raising=False)
    hermes_settings.forget()
    yield r
    hermes_settings.forget()


def _env(profile: Path) -> str:
    p = profile / ".env"
    return p.read_text() if p.exists() else ""


def test_empty_values_are_written_as_no_value_and_setup_does_not_trip(root):
    """B1: setup's playbook choice writes "" for every EMPORIA_PLAYBOOK_* name (cli._ask_playbook); the read-back
    used to report "did not take" for a value Hermes holds as nothing."""
    zed = root / "profiles" / "zed"
    from emporia import playbook as P
    cli.merge_env_file(zed / ".env", {"EMPORIA_PLAYBOOK": "scout", **{var: "" for var in P.SETTINGS}})
    assert "EMPORIA_PLAYBOOK=scout" in _env(zed) and "EMPORIA_PLAYBOOK_GAMES" not in _env(zed)
    cli.merge_env_file(zed / ".env", {"EMPORIA_PLAYBOOK_GAMES": "5"})
    assert "EMPORIA_PLAYBOOK_GAMES=5" in _env(zed)
    assert cli.merge_env_file(zed / ".env", {"EMPORIA_PLAYBOOK_GAMES": ""}) == ["EMPORIA_PLAYBOOK_GAMES"]   # cleared
    assert "EMPORIA_PLAYBOOK_GAMES" not in _env(zed)
    cli.merge_env_file(zed / ".env", {"EMPORIA_SIGNER_TOKEN": "tok-sentinel"})                              # a secret
    assert cli.merge_env_file(zed / ".env", {"EMPORIA_SIGNER_TOKEN": ""}) == ["EMPORIA_SIGNER_TOKEN"]
    assert "EMPORIA_SIGNER_TOKEN" not in _env(zed)


def test_a_write_lands_in_the_named_profile_whatever_the_sticky_one(root):
    zed, other = root / "profiles" / "zed", root / "profiles" / "other"
    hermes_settings.write(zed, {"EMPORIA_PLAYBOOK": "scout"})
    hermes_settings.write(root, {"EMPORIA_PLAYBOOK": "host"})
    assert "EMPORIA_PLAYBOOK=scout" in _env(zed) and "EMPORIA_PLAYBOOK=host" in _env(root)
    assert _env(other) == ""


def test_relay_urls_and_a_key_go_on_stdin_and_the_env_stays_owner_only(root):
    zed = root / "profiles" / "zed"
    hermes_settings.write(zed, {"EMPORIA_RELAY_URL": "https://relay.example", "EMPORIA_BACKUP_RELAYS": "https://b.example",
                                "EMPORIA_IDENTITY_PRIVATE_KEY": "ab" * 32, "EMPORIA_AGENT_ID": "zed"})
    got = hermes_settings.read(zed, fresh=True)
    assert got["EMPORIA_RELAY_URL"] == "https://relay.example" and got["EMPORIA_IDENTITY_PRIVATE_KEY"] == "ab" * 32
    assert stat.S_IMODE((zed / ".env").stat().st_mode) == 0o600
    hermes_settings.write(zed, unset=["EMPORIA_RELAY_URL", "EMPORIA_IDENTITY_PRIVATE_KEY", "EMPORIA_NEVER_SET"])
    assert hermes_settings.read(zed, fresh=True) == {"EMPORIA_AGENT_ID": "zed", "EMPORIA_BACKUP_RELAYS": "https://b.example"}


def test_unsetting_a_name_that_is_not_there_is_not_a_failure(root):
    hermes_settings.write(root / "profiles" / "zed", unset=["EMPORIA_PLAYBOOK"])


def test_general_reads_return_declared_names_only_and_presence_is_a_boolean(root):
    zed = root / "profiles" / "zed"
    (zed / ".env").write_text("OPENROUTER_API_KEY=sk-sentinel\nAPI_SERVER_KEY=" + "k" * 20 + "\nAPI_SERVER_HOST=0.0.0.0\n"
                              "EMPORIA_AGENT_ID=zed\nSECRET_ELSEWHERE=zzz\n")
    got = hermes_settings.read(zed, fresh=True)
    assert got == {"EMPORIA_AGENT_ID": "zed", "API_SERVER_HOST": "0.0.0.0"}
    assert profile_env.present(zed / ".env", ["OPENROUTER_API_KEY", "NOPE"]) == {"OPENROUTER_API_KEY": True, "NOPE": False}
    assert profile_env.present(zed / ".env", {"API_SERVER_KEY": 16, "OPENROUTER_API_KEY": 99}) == {
        "API_SERVER_KEY": True, "OPENROUTER_API_KEY": False}
    assert profile_env.read_names(zed / ".env", ["SECRET_ELSEWHERE"]) == {"SECRET_ELSEWHERE": "zzz"}


def test_the_doctor_checks_that_read_secrets_work_on_a_healthy_profile(root):
    """B2: the provider-key note, the api_server host check, secure_profile and `--from-env` over real Hermes."""
    zed = root / "profiles" / "zed"
    (zed / ".env").write_text("OPENROUTER_API_KEY=sk-sentinel\nAPI_SERVER_KEY=" + "k" * 20 + "\nAPI_SERVER_HOST=0.0.0.0\n")
    inst = cli.installer()
    assert inst._provider_key_note({"provider": "openrouter"}, zed) is None            # the key is there
    (zed / ".env").write_text("EMPORIA_AGENT_ID=zed\n")
    assert "OPENROUTER_API_KEY" in inst._provider_key_note({"provider": "openrouter"}, zed)
    (zed / ".env").write_text("API_SERVER_KEY=" + "k" * 20 + "\nAPI_SERVER_HOST=0.0.0.0\n")
    dotenv = profile_env.read(zed / ".env")
    assert cli.api_server_host(zed, dotenv) == "0.0.0.0", "the 0.0.0.0 warning stays"
    # secure_profile: a different value already in the .env is kept; the named secret moves; nothing else is read
    (zed / ".env").write_text("EMPORIA_X402_EVM_PRIVATE_KEY=in-env\nUNRELATED=1\n")
    (zed / "config.yaml").write_text("mcp_servers:\n  emporia:\n    env:\n      EMPORIA_X402_EVM_PRIVATE_KEY: literal\n"
                                     "      EMPORIA_X402_PHRASE_SECRET: phrase-literal\n")
    moved = inst.secure_profile(zed)
    assert moved == ["EMPORIA_X402_EVM_PRIVATE_KEY", "EMPORIA_X402_PHRASE_SECRET"]
    held = profile_env.read(zed / ".env")
    assert held["EMPORIA_X402_EVM_PRIVATE_KEY"] == "in-env" and held["EMPORIA_X402_PHRASE_SECRET"] == "phrase-literal"
    assert "UNRELATED=1" in _env(zed), "what is not Emporia's is left in the file"
    # wallet import --from-env: a name that is not declared is found, because the operator named it
    (zed / ".env").write_text("MY_OWN_WALLET_KEY=" + "ab" * 32 + "\n")
    assert profile_env.read_names(zed / ".env", ["MY_OWN_WALLET_KEY"]) == {"MY_OWN_WALLET_KEY": "ab" * 32}


def test_an_unset_of_a_name_a_secret_source_supplies_is_not_a_failure(root, monkeypatch, capsys):
    """S7: when `config unset` says the name is not in the .env yet Hermes still resolves it, it comes from a source."""
    zed = root / "profiles" / "zed"
    held = {"EMPORIA_PLAYBOOK": "from-a-source"}
    monkeypatch.setattr(hermes_settings, "_fetch", lambda home, names, vault=True: dict(held))
    monkeypatch.setattr(hermes_settings, "_hermes_config", lambda home, *a: type(
        "R", (), {"returncode": 1, "stdout": "", "stderr": "Config key not set: EMPORIA_PLAYBOOK"})())
    hermes_settings.write(zed, unset=["EMPORIA_PLAYBOOK"])
    err = capsys.readouterr().err
    assert "EMPORIA_PLAYBOOK" in err and "from-a-source" not in err


def test_the_names_a_person_gives_are_read_by_name_through_real_hermes(root):
    """F2: `wallet link env:NAME`, `identity link env:NAME`, an interactive `wallet import` variable name, and the
    A2A trusted-peers presence all work on a name Emporia does not declare, with the rest of the .env unread."""
    zed = root / "profiles" / "zed"
    (zed / ".env").write_text("MY_WALLET_KEY=" + "ab" * 32 + "\nA2A_TRUSTED_PEERS=peer-sentinel\nOP_SERVICE_ACCOUNT_TOKEN=optok\n"
                              "BW_SESSION=bwtok\nOPENROUTER_API_KEY=sk-sentinel\n")
    env = cli._source_env(zed, "env:MY_WALLET_KEY")
    assert env["MY_WALLET_KEY"] == "ab" * 32 and "OPENROUTER_API_KEY" not in env and "A2A_TRUSTED_PEERS" not in env
    from emporia.key_source import read_text
    assert read_text("env:MY_WALLET_KEY", env=env) == "ab" * 32
    vault = cli._source_env(zed, "cmd:op read op://v/i/f")                    # the CLI it names: its variables only
    assert vault["OP_SERVICE_ACCOUNT_TOKEN"] == "optok" and "BW_SESSION" not in vault
    assert "OP_SERVICE_ACCOUNT_TOKEN" not in cli._source_env(zed, "cmd:/home/u/key.sh")
    assert profile_env.read_names(zed / ".env", ["MY_WALLET_KEY"]) == {"MY_WALLET_KEY": "ab" * 32}
    (zed / "config.yaml").write_text("model: {}\n")
    assert cli.a2a_state(zed)[1] is True                                        # trusted peers: a presence check
    (zed / ".env").write_text("OPENROUTER_API_KEY=sk\n")
    assert cli.a2a_state(zed)[1] is False


def test_the_doctor_says_when_a_cmd_source_names_no_vault_cli_but_a_vault_variable_is_held(root):
    zed = root / "profiles" / "zed"
    (zed / ".env").write_text("OP_SERVICE_ACCOUNT_TOKEN=optok-sentinel\n")
    dotenv = {"EMPORIA_IDENTITY_KEY_SOURCE": "cmd:/home/u/key.sh"}

    class Rep:
        def __init__(self):
            self.warned = []

        def warn(self, msg, fix=""):
            self.warned.append((msg, fix))
    rep = Rep()
    cli.doctor_vault_sources(rep, zed, dotenv)
    msg, fix = rep.warned[0]
    assert "OP_SERVICE_ACCOUNT_TOKEN" in msg and "names no known vault CLI" in msg and "cmd:op read" in fix
    assert "optok-sentinel" not in msg + fix, "a presence check: no value"
    rep = Rep()
    cli.doctor_vault_sources(rep, zed, {"EMPORIA_IDENTITY_KEY_SOURCE": "cmd:op read op://v/i/f"})
    cli.doctor_vault_sources(rep, zed, {})
    assert rep.warned == []


def test_real_hermes_words_an_unset_of_a_missing_name_as_not_set(root):
    """The exact message N1 relies on, from the installed Hermes."""
    r = hermes_settings._hermes_config(root / "profiles" / "zed", "unset", "--", "EMPORIA_NEVER_HELD")
    assert r.returncode == 1 and "not set" in f"{r.stdout}\n{r.stderr}".lower()
