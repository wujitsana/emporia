"""installer/relay-update.sh, run with a fake `docker`, `curl` and `sudo` first on PATH: the order of calls, the refusals,
and that --check changes nothing. No Docker is involved. The script must stay POSIX sh."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "installer" / "relay-update.sh"
OLD = "a" * 40
NEW = "b" * 40

FAKE_DOCKER = r"""#!/bin/sh
echo "docker $*" >> "$FAKE_LOG"
case "$*" in
  *"exec "*)
    case "$*" in
      *relay_id*)
        if [ -n "${FAKE_IDENT_AFTER:-}" ] && grep -q "force-recreate" "$FAKE_LOG"; then cat "$FAKE_IDENT_AFTER"; else cat "$FAKE_IDENT"; fi ;;
      *'.get("build")'*) cat "$FAKE_BUILD" ;;
      *os.stat*) echo "42 ['keys']" ;;
    esac ;;
  *inspect*) echo "emporia-data" ;;
  *"run --rm --network none"*)
    [ -z "${FAKE_BACKUP_FAIL:-}" ] || exit 1
    dir=""; prev=""
    for a in "$@"; do case "$prev" in -v) case "$a" in *:/b) dir="${a%:/b}" ;; esac ;; esac; prev="$a"; done
    name="$(printf '%s' "$*" | grep -o 'emporia-data-[0-9TZ]*\.tgz' | head -1)"
    [ -n "$dir" ] && [ -n "$name" ] && echo backup > "$dir/$name" ;;
  *"compose"*os.stat*) if [ -n "${FAKE_OTHER_DATA:-}" ]; then echo "7 ['other']"; else echo "42 ['keys']"; fi ;;
  *"compose"*"--dry-run"*) cat "$FAKE_PLAN"; exit "${FAKE_DRY_RC:-0}" ;;
  *"compose"*" build "*) exit "${FAKE_BUILD_RC:-0}" ;;
  *"compose"*"relay upgrade"*) exit "${FAKE_UPGRADE_RC:-0}" ;;
esac
exit 0
"""

FAKE_CURL = r"""#!/bin/sh
for a in "$@"; do url="$a"; done
echo "curl $url" >> "$FAKE_LOG"
case "$url" in
  */commits/*) printf '%s' "${FAKE_COMMIT_REPLY:-}" ;;
  */compare/*) printf '{"status": "%s"}' "${FAKE_COMPARE:-behind}" ;;
  *relay-update.sh) cat "$FAKE_RAW" ;;
  *) exit 22 ;;
esac
"""


@pytest.fixture
def env(tmp_path):
    bin_ = tmp_path / "bin"
    bin_.mkdir()
    for name, body in (("docker", FAKE_DOCKER), ("curl", FAKE_CURL)):
        (bin_ / name).write_text(body)
        (bin_ / name).chmod(0o755)
    home = tmp_path / "home"
    d = home / ".emporia"
    d.mkdir(parents=True)
    (d / "compose.snippet.yaml").write_text(
        f"services:\n  emporia-relay:\n    build: https://github.com/wujitsana/emporia.git#{OLD}\n"
        f"    image: emporia-relay:{OLD[:12]}\n")
    (d / ".env").write_text("EMPORIA_ACCESS=open\nEMPORIA_BUILD_COMMIT=" + OLD + "\n")
    script = tmp_path / "relay-update.sh"
    shutil.copy(SCRIPT, script)
    (tmp_path / "ident").write_text("rid pub\n")
    (tmp_path / "build").write_text(NEW[:8] + "\n")
    (tmp_path / "plan").write_text("database: x\nnothing to do: the database is already at this schema\n")
    e = {"PATH": f"{bin_}:/usr/bin:/bin", "HOME": str(home), "FAKE_LOG": str(tmp_path / "log"),
         "FAKE_IDENT": str(tmp_path / "ident"), "FAKE_BUILD": str(tmp_path / "build"),
         "FAKE_PLAN": str(tmp_path / "plan"), "FAKE_RAW": str(script), "FAKE_COMMIT_REPLY": NEW}
    return tmp_path, script, e


def run(env, *args, stdin="", extra=None):
    tmp, script, e = env
    e = {**e, **(extra or {})}
    p = subprocess.run(["sh", str(script), *args], capture_output=True, text=True, input=stdin, env=e, timeout=60)
    log = (tmp / "log").read_text().splitlines() if (tmp / "log").exists() else []
    return p, log


def position(log, needle):
    return next(i for i, line in enumerate(log) if needle in line)


def snippet(env) -> str:
    return (env[2] and Path(env[2]["HOME"]) / ".emporia" / "compose.snippet.yaml").read_text()


@pytest.mark.parametrize("bad", ["", "abc1234", NEW[:39], NEW + "0", NEW.upper(), "main"])
def test_only_a_full_lowercase_sha(env, bad):
    p, log = run(env, *([bad] if bad else []))
    assert p.returncode != 0
    assert log == []
    assert "40-character" in (p.stdout + p.stderr) or "usage" in (p.stdout + p.stderr)


def test_a_commit_github_does_not_know_stops_before_docker(env):
    p, log = run(env, NEW, extra={"FAKE_COMMIT_REPLY": ""})
    assert p.returncode == 1 and "GitHub does not know" in p.stderr
    assert not any(line.startswith("docker") for line in log)


def test_a_script_that_is_not_the_one_at_that_commit_is_refused(env):
    tmp, _, _ = env
    other = tmp / "other.sh"
    other.write_text("#!/bin/sh\necho different\n")
    p, log = run(env, NEW, extra={"FAKE_RAW": str(other)})
    assert p.returncode == 1 and "not the installer/relay-update.sh" in p.stderr
    assert not any(line.startswith("docker") for line in log)


def test_a_different_data_folder_is_refused_and_the_pin_is_untouched(env):
    p, log = run(env, NEW, extra={"FAKE_OTHER_DATA": "1"})
    assert p.returncode == 1 and "does not see the live relay's /data" in p.stderr
    assert OLD in snippet(env) and not any(" build " in line for line in log)


def test_no_schema_change_builds_first_asks_nothing_and_checks_the_identity(env):
    p, log = run(env, NEW)
    assert p.returncode == 0, p.stdout + p.stderr
    order = [position(log, n) for n in ("exec emporia-relay", "inspect", "compose run --rm --no-deps emporia-relay python",
                                        " build ", "--dry-run", " stop ", "tar czf", "up -d --force-recreate")]
    assert order == sorted(order)
    assert f"--build-arg EMPORIA_BUILD_COMMIT={NEW}" in log[position(log, " build ")]
    assert not any("--yes" in line for line in log)
    assert "SAME RELAY, NEW BUILD" in p.stdout and "Rollback, if you need it" in p.stdout
    assert NEW in snippet(env) and OLD not in snippet(env)
    assert f"emporia-relay:{NEW[:12]}" in snippet(env)
    home = Path(env[2]["HOME"])
    assert list((home / ".emporia").glob("compose.snippet.yaml.bak-*"))
    assert "emporia-backups/emporia-data-" in p.stdout


def test_settings_change_only_in_the_one_build_commit_line_and_after_a_backup(env):
    home = Path(env[2]["HOME"])
    p, _ = run(env, NEW)
    assert p.returncode == 0
    text = (home / ".emporia" / ".env").read_text()
    assert text == f"EMPORIA_ACCESS=open\nEMPORIA_BUILD_COMMIT={NEW}\n"
    assert (next((home / ".emporia").glob(".env.bak-*"))).read_text().endswith(OLD + "\n")


def test_an_env_without_the_line_is_left_alone(env):
    home = Path(env[2]["HOME"])
    (home / ".emporia" / ".env").write_text("EMPORIA_ACCESS=open\n")
    p, _ = run(env, NEW)
    assert p.returncode == 0
    assert (home / ".emporia" / ".env").read_text() == "EMPORIA_ACCESS=open\n"
    assert not list((home / ".emporia").glob(".env.bak-*"))


def schema_plan(env):
    (env[0] / "plan").write_text("schema 52 -> 53: add table x\n")


def test_a_schema_change_asks_and_n_restores_the_pin_and_starts_the_old_relay(env):
    schema_plan(env)
    p, log = run(env, NEW, stdin="n\n")
    assert p.returncode == 1
    assert OLD in snippet(env) and NEW not in snippet(env)
    assert not any("--yes" in line for line in log)
    assert position(log, " stop ") < position(log, "up -d emporia-relay")
    assert "old relay" in p.stdout and "Rollback, if you need it" in p.stdout


def test_a_schema_change_with_y_upgrades_after_the_backup_and_before_the_start(env):
    schema_plan(env)
    p, log = run(env, NEW, stdin="y\n")
    assert p.returncode == 0, p.stdout + p.stderr
    upgrade = next(i for i, line in enumerate(log) if "relay upgrade" in line and "--yes" in line)
    assert position(log, "tar czf") < upgrade < position(log, "up -d --force-recreate")


def test_yes_answers_the_question(env):
    schema_plan(env)
    p, log = run(env, "--yes", NEW)
    assert p.returncode == 0 and any("relay upgrade" in line and "--yes" in line for line in log)


def test_a_database_the_new_release_refuses_restores_the_pin_and_never_stops_the_relay(env):
    p, log = run(env, NEW, extra={"FAKE_DRY_RC": "1"})
    assert p.returncode == 1 and "refuses this database" in p.stderr
    assert OLD in snippet(env) and not any(" stop " in line for line in log)


def test_a_failed_build_restores_the_pin_and_never_stops_the_relay(env):
    p, log = run(env, NEW, extra={"FAKE_BUILD_RC": "1"})
    assert p.returncode == 1 and "the build failed" in p.stderr
    assert OLD in snippet(env) and not any(" stop " in line for line in log)


def test_a_failed_upgrade_leaves_the_relay_stopped_with_the_rollback(env):
    schema_plan(env)
    p, log = run(env, "--yes", NEW, extra={"FAKE_UPGRADE_RC": "1"})
    assert p.returncode == 1 and "DATABASE UPGRADE FAILED" in p.stdout and "Rollback" in p.stdout
    assert not any("up -d" in line for line in log)


def test_a_different_identity_is_loud(env):
    tmp, _, _ = env
    (tmp / "after").write_text("someone-else otherkey\n")
    p, _ = run(env, NEW, extra={"FAKE_IDENT_AFTER": str(tmp / "after")})
    assert p.returncode == 2 and "NOT AS EXPECTED" in p.stdout and "Rollback, if you need it" in p.stdout


def test_a_wrong_build_is_loud(env):
    tmp, _, _ = env
    (tmp / "build").write_text(OLD[:8] + "\n")
    p, _ = run(env, NEW)
    assert p.returncode == 2 and "NOT AS EXPECTED" in p.stdout


def test_check_prints_the_steps_and_changes_nothing(env):
    schema_plan(env)
    before = snippet(env)
    home = Path(env[2]["HOME"])
    envfile = (home / ".emporia" / ".env").read_text()
    p, log = run(env, "--check", NEW)
    assert p.returncode == 0, p.stdout + p.stderr
    assert snippet(env) == before and (home / ".emporia" / ".env").read_text() == envfile
    assert not list((home / ".emporia").glob("*.bak-*"))
    assert not (home / "emporia-backups").exists()
    for word in (" build ", " stop ", " up ", "tar czf", "relay upgrade", "run --rm --no-deps emporia-relay emporia"):
        assert not any(word in line for line in log if line.startswith("docker")), word
    for needle in ("would run:", "--check: nothing was changed", "Rollback, if you need it"):
        assert needle in p.stdout


def test_the_script_is_posix_sh_and_lints_clean_where_shellcheck_exists():
    text = SCRIPT.read_text()
    assert text.startswith("#!/bin/sh\n")
    for bashism in ("[[", "pipefail", "<<<", "local ", "function "):
        assert bashism not in text
    if shutil.which("shellcheck"):
        r = subprocess.run(["shellcheck", "-s", "sh", str(SCRIPT)], capture_output=True, text=True)
        assert r.returncode == 0, r.stdout
    assert os.access(SCRIPT, os.X_OK)
