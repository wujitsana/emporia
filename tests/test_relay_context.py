"""An Emporia agent is confined to its own profile: its turns start in a folder of the profile and never load
the host's project files (SOUL.md still applies). Installer writes it, doctor checks it."""
import json

from emporia import cli
from emporia.cli import Report


def _inst():
    return cli.installer()


def _cfg(path):
    return _inst().load_yaml(path / "config.yaml")


def test_isolate_context_writes_both_settings_and_keeps_the_rest(tmp_path):
    inst = _inst()
    (tmp_path / "config.yaml").write_text(json.dumps(
        {"model": {"default": "x"}, "gateway": {"platforms": {"telegram": {"a": 1}}}}))
    assert inst.isolate_context(tmp_path) is True
    data = _cfg(tmp_path)
    plats = data["gateway"]["platforms"]
    assert plats["emporia"] == {"skip_context_files": True}
    assert plats["telegram"] == {"a": 1, "skip_context_files": True}      # every platform the gateway names
    assert data["terminal"]["cwd"] == str(tmp_path / "workspace") and (tmp_path / "workspace").is_dir()
    assert data["model"] == {"default": "x"}
    assert data["memory"] == {"user_profile_enabled": False} and inst.user_profile_off(data)
    assert inst.context_isolated(data) and inst.workspace_confined(data, tmp_path)
    assert inst.isolate_context(tmp_path) is False                        # idempotent


def test_isolate_context_fixes_false_keeps_a_chosen_cwd_and_a_platform_list(tmp_path):
    inst = _inst()
    chosen = tmp_path / "elsewhere"
    chosen.mkdir()
    prof = tmp_path / "prof"
    prof.mkdir()
    (prof / "config.yaml").write_text(json.dumps(
        {"terminal": {"cwd": str(chosen)},
         "gateway": {"platforms": {"emporia": {"skip_context_files": False, "k": 1}}}}))
    assert inst.isolate_context(prof) is True
    data = _cfg(prof)
    assert data["gateway"]["platforms"]["emporia"] == {"skip_context_files": True, "k": 1}
    assert data["terminal"]["cwd"] == str(chosen)                         # a person's own choice stays
    assert not inst.workspace_confined(data, prof)                        # ...and doctor says so
    (prof / "config.yaml").write_text(json.dumps({"gateway": {"platforms": ["telegram"]}}))
    inst.isolate_context(prof)
    assert _cfg(prof)["gateway"]["platforms"] == ["telegram"]             # Hermes's own list shape untouched
    assert not inst.context_isolated(_cfg(prof))


def test_unset_dot_and_relative_cwd_are_not_a_confinement(tmp_path):
    inst = _inst()
    for bad in (None, "", ".", "auto", "workspace"):
        assert not inst.workspace_set(bad)
    assert inst.workspace_set(str(tmp_path))


def _doctor(prof, monkeypatch, capsys, dotenv=None):
    monkeypatch.setattr(cli, "turns_in_24h", lambda p: 0)
    monkeypatch.setattr(cli, "hermes_context_probe", lambda cwd: None)
    monkeypatch.setattr("emporia.rounds.status_line", lambda profile: "off")   # never the real `hermes cron list`
    rep = Report()
    cli.doctor_agent_hardening(rep, prof, dotenv or {})
    return rep, capsys.readouterr().out


def test_doctor_fails_with_the_fix_when_not_confined_then_passes(tmp_path, capsys, monkeypatch):
    prof = tmp_path / "zed"
    prof.mkdir()
    (prof / "config.yaml").write_text(json.dumps({}))
    rep, out = _doctor(prof, monkeypatch, capsys)
    assert rep.failed, out
    assert "emporia agent refresh zed" in out and "AGENTS.md" in out and "terminal.cwd" in out
    _inst().isolate_context(prof)
    (prof / "config.yaml").write_text(json.dumps({**_cfg(prof), "agent": {"disabled_toolsets": list(cli.MARKETPLACE_OFF)}}))
    rep, out = _doctor(prof, monkeypatch, capsys)
    assert not rep.failed, out
    assert "load no project files" in out and "SOUL.md still applies" in out
    assert "folder inside its own profile" in out and "browser and cron toolsets are off" in out


def test_a_true_user_profile_is_turned_off_and_memory_kept(tmp_path):
    inst = _inst()
    (tmp_path / "config.yaml").write_text(json.dumps({"memory": {"user_profile_enabled": True, "memory_enabled": True}}))
    assert inst.isolate_context(tmp_path) is True
    assert _cfg(tmp_path)["memory"] == {"user_profile_enabled": False, "memory_enabled": True}


def test_doctor_fails_while_the_user_profile_is_on(tmp_path, capsys, monkeypatch):
    prof = tmp_path / "zed"
    prof.mkdir()
    (prof / "config.yaml").write_text("{}")
    _inst().isolate_context(prof)
    cfg = _cfg(prof)
    cfg["memory"] = {"user_profile_enabled": True}
    (prof / "config.yaml").write_text(json.dumps(cfg))
    rep, out = _doctor(prof, monkeypatch, capsys)
    assert rep.failed and "USER.md" in out and "emporia agent refresh zed" in out, out


def test_doctor_names_the_files_hermes_would_load(tmp_path, capsys, monkeypatch):
    prof = tmp_path / "zed"
    prof.mkdir()
    (prof / "config.yaml").write_text(json.dumps({}))
    monkeypatch.setattr(cli, "turns_in_24h", lambda p: 0)
    monkeypatch.setattr(cli, "hermes_context_probe", lambda cwd: ["/host/CLAUDE.md"])
    cli.doctor_agent_hardening(Report(), prof, {})
    assert "/host/CLAUDE.md" in capsys.readouterr().out


def test_doctor_reports_hermes_own_listing_for_the_working_directory(tmp_path, capsys, monkeypatch):
    prof = tmp_path / "zed"
    prof.mkdir()
    (prof / "config.yaml").write_text("{}")
    _inst().isolate_context(prof)
    monkeypatch.setattr(cli, "turns_in_24h", lambda p: 0)
    monkeypatch.setattr(cli, "hermes_context_probe", lambda cwd: [])
    cli.doctor_agent_hardening(Report(), prof, {})
    assert "finds no project files from" in capsys.readouterr().out
    monkeypatch.setattr(cli, "hermes_context_probe", lambda cwd: ["/up/AGENTS.md"])
    cli.doctor_agent_hardening(Report(), prof, {})
    assert "/up/AGENTS.md" in capsys.readouterr().out


def test_doctor_warns_for_other_platforms_and_open_toolsets(tmp_path, capsys, monkeypatch):
    prof = tmp_path / "zed"
    prof.mkdir()
    (prof / "config.yaml").write_text(json.dumps({"gateway": {"platforms": {"telegram": {}}}}))
    _inst().isolate_context(prof)
    cfg = _cfg(prof)
    cfg["gateway"]["platforms"]["telegram"] = {}
    (prof / "config.yaml").write_text(json.dumps(cfg))
    rep, out = _doctor(prof, monkeypatch, capsys)
    assert "telegram still load project files" in out and "toolsets still on" in out
    assert not rep.failed


def test_doctor_survives_a_working_directory_that_was_deleted(tmp_path, capsys, monkeypatch):
    """Doctor started from a folder that no longer exists (os.getcwd raises) still reports, and still fails an
    unconfined profile."""
    gone = tmp_path / "gone"
    gone.mkdir()
    monkeypatch.chdir(gone)
    gone.rmdir()
    prof = tmp_path / "zed"
    prof.mkdir()
    (prof / "config.yaml").write_text(json.dumps({}))
    rep, out = _doctor(prof, monkeypatch, capsys)
    assert rep.failed, out
    assert "emporia agent refresh zed" in out


def test_a_config_rewritten_within_the_same_timestamp_is_read_fresh(tmp_path):
    """load_yaml cached by modification time returned the first of two writes that shared a timestamp."""
    import os
    inst = _inst()
    p = tmp_path / "config.yaml"
    p.write_text(json.dumps({"memory": {"user_profile_enabled": False}}))
    st = p.stat()
    assert inst.load_yaml(p)["memory"]["user_profile_enabled"] is False
    p.write_text(json.dumps({"memory": {"user_profile_enabled": True}}))
    os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns))            # same timestamp as the first version
    assert inst.load_yaml(p)["memory"]["user_profile_enabled"] is True
