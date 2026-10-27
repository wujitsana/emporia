"""P201: the Emporia platform plugin tells Hermes which of a profile's settings are secret."""
from pathlib import Path

import yaml

MANIFEST = Path(__file__).resolve().parent.parent / "hermes_plugin" / "emporia" / "plugin.yaml"


def test_every_key_emporia_can_keep_in_a_profile_env_is_declared_secret():
    """Hermes strips a platform plugin's `password: true` settings from every
    command and script the agent runs; nothing else of Emporia's is on its list."""
    from emporia.key_source import ENV_KEY_NAMES
    manifest = yaml.safe_load(MANIFEST.read_text())
    entries = manifest["requires_env"] + manifest["optional_env"]
    secret = {e["name"] for e in entries if e.get("password") is True}
    expected = set(ENV_KEY_NAMES.values()) | {"EMPORIA_NOUS_JWT", "EMPORIA_OPERATOR_PRIVATE_KEY",
                                              "STRIPE_SECRET_KEY", "STRIPE_WEBHOOK_SECRET",
                                              "OP_SERVICE_ACCOUNT_TOKEN", "BW_SESSION", "VAULT_TOKEN"}
    assert expected <= secret, expected - secret


def test_doctor_finds_secrets_in_config_yaml_and_secure_moves_them(tmp_path, monkeypatch, capsys):
    from emporia import cli
    profile = tmp_path / "profiles" / "p201doc"
    profile.mkdir(parents=True)
    (profile / "config.yaml").write_text(yaml.dump({"env": {"EMPORIA_NOUS_JWT": "jwt"}}))
    (profile / ".env").write_text("EMPORIA_AGENT_ID=p201doc\n")
    rep = cli.Report()
    cli.doctor_profile_secrets(rep, profile, {})
    out = capsys.readouterr().out
    assert rep.failed and "config.yaml holds secrets: EMPORIA_NOUS_JWT" in out and "jwt\n" not in out
    monkeypatch.setattr(cli, "profiles_root", lambda: tmp_path / "profiles")
    assert cli.main(["agent", "secure", "p201doc"]) == 0
    assert "jwt" not in capsys.readouterr().out
    rep = cli.Report()
    cli.doctor_profile_secrets(rep, profile, {})
    assert not rep.failed and "owner-only" in capsys.readouterr().out
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_container")
    cli.doctor_machine_secrets(rep)
    assert "STRIPE_SECRET_KEY set for the whole container" in capsys.readouterr().out
