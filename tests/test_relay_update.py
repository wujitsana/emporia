"""`emporia relay update` for a service relay: the order of steps, the refusals and what --check leaves alone.
Every command goes through a fake runner; nothing here touches a service, git or the network."""

from __future__ import annotations

from pathlib import Path

import pytest

from emporia import relay_update as ru

OLD = "a" * 40
NEW = "b" * 40
IDENT = {"relay_id": "rid", "relay_public_key": "pub", "build": OLD[:8], "schema_version": 52}


class Fake:
    """Records every command; answers from a table keyed by the command's first useful words."""

    def __init__(self, tmp: Path, *, need_upgrade=False, answer=True, github=True, head=OLD,
                 dirty="", after=None, same_data=True, upgrade_rc=0, dry_rc=0, backup_rc=0, sync_rc=0):
        self.tmp = tmp
        self.calls: list[list[str]] = []
        self.said: list[str] = []
        self.need, self.answer, self.github, self.head, self.dirty = need_upgrade, answer, github, head, dirty
        self.same_data, self.upgrade_rc, self.dry_rc, self.backup_rc, self.sync_rc = (
            same_data, upgrade_rc, dry_rc, backup_rc, sync_rc)
        self.after = IDENT | {"build": NEW[:8]} if after is None else after
        self.asked = 0
        self.up = True
        self.folder = tmp / "relay"
        self.repo = tmp / "code"
        (self.folder / "data").mkdir(parents=True)
        (self.folder / "data" / "emporia.sqlite3").write_text("")
        (self.repo / ".git").mkdir(parents=True)
        (self.folder / ru.UNIT).write_text(
            f"[Service]\nUser=emporia-relay\nWorkingDirectory={self.repo}\nEnvironmentFile={self.folder}/.env\n")

    def cmds(self) -> list[str]:
        return [" ".join(c) for c in self.calls]

    def run(self, argv, cwd=None):
        self.calls.append(list(argv))
        text = " ".join(argv)
        if argv[:2] == ["systemctl", "show"]:
            data = self.folder / "data" if self.same_data else self.tmp / "elsewhere"
            return 0, f"User=emporia-relay\nWorkingDirectory={self.repo}\nReadWritePaths={data}\n"
        if "status --porcelain" in text:
            return 0, self.dirty
        if "rev-parse HEAD" in text:
            return 0, self.head + "\n"
        if "--dry-run" in text:
            return (self.dry_rc, "schema 52 -> 53: add table x\n" if self.need
                    else f"database: x\n{ru.NOTHING_TO_DO}\n")
        if "relay upgrade" in text or ("upgrade" in argv and "--yes" in argv):
            return self.upgrade_rc, "upgraded\n"
        if "cp -a" in text:
            return self.backup_rc, ""
        if argv[0] == "uv":
            return self.sync_rc, ""
        return 0, ""

    def io(self) -> ru.IO:
        def health(_url):
            return dict(IDENT) if self.calls_before_start() else (dict(self.after) if self.up else None)
        return ru.IO(run=self.run, health=health, ask=self._ask, on_github=lambda *_: self.github,
                     say=self.said.append, stamp=lambda: "STAMP", writable=lambda _p: True,
                     schema_cmd=["python", "-m", "emporia"])

    def calls_before_start(self) -> bool:
        return not any(c[:3] == ["sudo", "systemctl", "start"] for c in self.calls)

    def _ask(self, _q):
        self.asked += 1
        return self.answer

    def opts(self, **kw) -> ru.Options:
        return ru.Options(to=kw.pop("to", NEW), folder=self.folder, repo=self.repo, **kw)

    def go(self, **kw) -> int:
        return ru.update(self.opts(**kw), self.io())

    def out(self) -> str:
        return "\n".join(self.said)


@pytest.fixture
def fk(tmp_path):
    return Fake(tmp_path)


def index(cmds, needle):
    return next(i for i, c in enumerate(cmds) if needle in c)


@pytest.mark.parametrize("bad", ["", "abc1234", "A" * 40, "b" * 39, "b" * 41, "main", "g" * 40])
def test_only_a_full_lowercase_sha_is_accepted(fk, bad):
    assert fk.go(to=bad) == 1
    assert "full 40-character" in fk.out()
    assert fk.calls == []


def test_a_commit_github_does_not_know_is_refused_before_anything_runs(tmp_path):
    fk = Fake(tmp_path, github=False)
    assert fk.go() == 1
    assert "GitHub does not know" in fk.out() and fk.calls == []


def test_a_docker_relay_is_pointed_at_the_script(fk):
    (fk.folder / "compose.snippet.yaml").write_text("services: {}\n")
    assert fk.go() == 1
    out = fk.out()
    assert f"raw.githubusercontent.com/wujitsana/emporia/{NEW}/installer/relay-update.sh" in out
    assert fk.calls == []


def test_a_service_that_does_not_use_this_folder_is_refused_and_nothing_changes(tmp_path):
    fk = Fake(tmp_path, same_data=False)
    assert fk.go() == 1
    assert "does not use this folder's data" in fk.out()
    assert not any("systemctl stop" in c or "checkout" in c for c in fk.cmds())


def test_a_checkout_with_its_own_changes_is_refused(tmp_path):
    fk = Fake(tmp_path, dirty=" M src/emporia/x.py")
    assert fk.go() == 1
    assert "changes of its own" in fk.out()
    assert not any("systemctl stop" in c for c in fk.cmds())


def test_already_at_the_commit_does_nothing(tmp_path):
    fk = Fake(tmp_path, head=NEW)
    assert fk.go() == 0
    assert "Nothing to do" in fk.out()
    assert not any("systemctl" in c and "show" not in c for c in fk.cmds())


def test_without_a_schema_change_no_question_is_asked_and_the_order_holds(fk):
    assert fk.go() == 0
    cmds = fk.cmds()
    fetch, dry = index(cmds, "fetch"), index(cmds, "--dry-run")
    stop, backup = index(cmds, "systemctl stop"), index(cmds, "cp -a")
    checkout, sync = index(cmds, "checkout"), index(cmds, "uv sync")
    start = index(cmds, "systemctl start")
    assert fetch < dry < stop < backup < checkout < sync < start      # new code is fetched while the old relay serves
    assert fk.asked == 0
    assert not any("--yes" in c for c in cmds)                        # no upgrade ran
    out = fk.out()
    assert "no upgrade needed" in out and "SAME RELAY, NEW BUILD" in out
    assert "Rollback, if you need it" in out and "emporia-data-STAMP" in out


def test_the_backup_is_a_full_copy_of_the_data_folder_taken_while_stopped(fk):
    fk.go(backup_dir=fk.tmp / "bk")
    cp = next(c for c in fk.calls if "cp" in c)
    assert cp == ["sudo", "cp", "-a", str(fk.folder / "data"), str(fk.tmp / "bk" / "emporia-data-STAMP")]
    assert fk.cmds().index(" ".join(cp)) > index(fk.cmds(), "systemctl stop")


def test_with_a_schema_change_the_user_is_asked_and_yes_upgrades_before_the_new_code(tmp_path):
    fk = Fake(tmp_path, need_upgrade=True, answer=True)
    assert fk.go() == 0
    assert fk.asked == 1
    cmds = fk.cmds()
    assert index(cmds, "cp -a") < index(cmds, "--yes") < index(cmds, "checkout")


def test_yes_flag_answers_the_question(tmp_path):
    fk = Fake(tmp_path, need_upgrade=True, answer=False)
    assert fk.go(yes=True) == 0 and fk.asked == 0
    assert any("--yes" in c for c in fk.cmds())


def test_no_leaves_the_old_code_and_starts_the_old_relay_again(tmp_path):
    fk = Fake(tmp_path, need_upgrade=True, answer=False)
    assert fk.go() == 1
    cmds = fk.cmds()
    assert not any("--yes" in c for c in cmds)
    assert not any("checkout" in c for c in cmds) and not any("uv sync" in c for c in cmds)
    assert index(cmds, "systemctl stop") < index(cmds, "systemctl start")
    assert "started again" in fk.out()


def test_a_refused_database_stops_before_the_relay_is_stopped(tmp_path):
    fk = Fake(tmp_path, dry_rc=1)
    assert fk.go() == 1
    assert "refuses this database" in fk.out()
    assert not any("systemctl stop" in c for c in fk.cmds())


def test_a_failed_backup_starts_the_old_relay_and_changes_nothing_else(tmp_path):
    fk = Fake(tmp_path, backup_rc=1)
    assert fk.go() == 1
    cmds = fk.cmds()
    assert "the backup failed" in fk.out()
    assert index(cmds, "systemctl stop") < index(cmds, "systemctl start")
    assert not any("checkout" in c for c in cmds)


def test_a_failed_upgrade_leaves_the_relay_stopped_and_prints_the_rollback(tmp_path):
    fk = Fake(tmp_path, need_upgrade=True, upgrade_rc=1)
    assert fk.go(yes=True) == 1
    out = fk.out()
    assert "UPGRADE FAILED" in out and "Rollback, if you need it" in out
    assert not any("checkout" in c for c in fk.cmds())
    assert not any(c.startswith("sudo systemctl start") for c in fk.cmds())


def test_a_failed_install_of_the_new_code_puts_the_old_code_back(tmp_path):
    fk = Fake(tmp_path, sync_rc=1)
    assert fk.go() == 1
    assert any(f"checkout --quiet --detach {OLD}" in c for c in fk.cmds())
    assert any(c.startswith("sudo systemctl start") for c in fk.cmds())     # no schema change: the old relay runs again


def test_a_different_identity_is_loud_and_prints_the_rollback(tmp_path):
    fk = Fake(tmp_path, after=IDENT | {"relay_id": "someone-else", "build": NEW[:8]})
    assert fk.go() == 2
    out = fk.out()
    assert "NOT AS EXPECTED" in out and "Rollback, if you need it" in out


def test_a_wrong_build_is_loud_even_with_the_same_identity(tmp_path):
    fk = Fake(tmp_path, after=dict(IDENT))                 # still reports the old build
    assert fk.go() == 2
    assert "NOT AS EXPECTED" in fk.out()


def test_check_prints_every_step_and_changes_nothing(tmp_path):
    fk = Fake(tmp_path, need_upgrade=True)
    assert fk.go(check=True) == 0
    changing = [c for c in fk.cmds()
                if any(w in c for w in ("systemctl stop", "systemctl start", "cp -a", "checkout", "uv sync", "fetch",
                                        "mkdir", "--yes"))]
    ran = {" ".join(c) for c in fk.calls}
    assert not [c for c in changing if c in ran]
    out = fk.out()
    for needle in ("would run: sudo systemctl stop", "would run: sudo cp -a", "would run: git -C",
                   "would run: uv sync", "would run: sudo systemctl start", "--check: nothing was changed"):
        assert needle in out
    assert fk.asked == 0


def test_the_command_is_wired_and_refuses_a_short_sha(capsys):
    from emporia import cli
    assert cli.main(["relay", "update", "--to", "abc1234"]) == 1
    assert "full 40-character" in capsys.readouterr().out
