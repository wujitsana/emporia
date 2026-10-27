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
                                              "OP_SERVICE_ACCOUNT_TOKEN", "BW_SESSION", "VAULT_TOKEN",
                                              "EMPORIA_SIGNER_TOKEN"}
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
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_from_the_profile_env")
    from emporia import config
    monkeypatch.setattr(config, "ENV_BEFORE_DOTENV", frozenset())       # merged from a .env: not the container's
    cli.doctor_machine_secrets(rep)
    assert "no relay secret in the container's environment" in capsys.readouterr().out
    monkeypatch.setattr(config, "ENV_BEFORE_DOTENV", frozenset({"STRIPE_SECRET_KEY"}))   # given by compose
    cli.doctor_machine_secrets(rep)
    assert "STRIPE_SECRET_KEY set for the whole container" in capsys.readouterr().out


def test_doctor_fails_an_older_copy_of_the_plugin(tmp_path, capsys):
    """P206: live profiles ran a copy without the key guard; doctor now compares files."""
    import shutil

    from emporia import cli
    profile = tmp_path / "profiles" / "oldcopy"
    installed = profile / "plugins" / "platforms" / "emporia"
    shutil.copytree(MANIFEST.parent, installed, ignore=shutil.ignore_patterns("__pycache__"))
    (profile / "config.yaml").write_text("{}")
    rep = cli.Report()
    cli.doctor_profile_secrets(rep, profile, {})
    assert not rep.failed
    (installed / "key_guard.py").unlink()
    rep = cli.Report()
    cli.doctor_profile_secrets(rep, profile, {})
    assert rep.failed and "key_guard.py" in capsys.readouterr().out


def test_doctor_catches_delegation_that_undoes_a_role(tmp_path, monkeypatch, capsys):
    """P207: Hermes's profile routes run a subagent as another profile, keys and terminal included."""
    from emporia import cli
    root = tmp_path / "profiles"
    off = list(cli.MARKETPLACE_OFF)
    for name, cfg in (("shop", {"agent": {"disabled_toolsets": off},
                                "delegation": {"backend": "profile_routes",
                                               "backends": {"profile_routes": {"profiles": [{"name": "coder"}]}}}}),
                      ("coder", {"delegation": {"backends": {"model_map": {"options": [
                          {"name": "fast", "model": "m", "api_key": "sk-plain"}]}}}})):
        (root / name).mkdir(parents=True)
        (root / name / "config.yaml").write_text(yaml.dump(cfg))
    monkeypatch.setattr(cli, "_has_wallet", lambda profile, agent_id: False)
    rep = cli.Report()
    cli.doctor_delegation(rep, root / "shop")
    assert rep.failed and "which has a terminal" in capsys.readouterr().out
    rep = cli.Report()
    cli.doctor_delegation(rep, root / "coder")
    out = capsys.readouterr().out
    assert rep.failed and "api_key in plain text" in out and "sk-plain" not in out
