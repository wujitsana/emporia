"""`emporia relay update --to <full commit sha>`: move a relay that runs as a system service (`emporia relay host
--run service`) to a newer release commit, in the right order.

Run it from the TARGET release, so nothing of it needs to be installed first:

    uvx --from git+https://github.com/wujitsana/emporia@<sha> emporia relay update --to <sha>

A relay in Docker is moved by `installer/relay-update.sh` instead (a host that has only Docker). Both take the same
steps: the commit is checked, the relay's /data is confirmed to be the one this folder holds, the new code is fetched
while the old relay keeps serving, a dry run says whether the database needs an upgrade, then (downtime) stop, full
backup, upgrade after a y/N only if the schema changes, new code, start, same-relay check. A problem stops it; nothing
is deleted. Every step is a command run through `io.run`, so the tests drive it with a fake.
"""
from __future__ import annotations

import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

SHA_RE = re.compile(r"[0-9a-f]{40}")
REPO_SLUG = "wujitsana/emporia"
SERVICE = "emporia-relay"
UNIT = "emporia-relay.service"
NOTHING_TO_DO = "nothing to do: the database is already at this schema"
SCRIPT_CMD = ("curl -fsSLO https://raw.githubusercontent.com/{repo}/{sha}/installer/relay-update.sh && "
              "sh relay-update.sh {sha}")


@dataclass
class Options:
    to: str
    folder: Path
    repo: Path | None = None            # the service's code checkout (default: the unit's WorkingDirectory)
    service: str = SERVICE
    backup_dir: Path | None = None
    url: str = "http://127.0.0.1:8089"
    check: bool = False
    yes: bool = False
    slug: str = REPO_SLUG


@dataclass
class IO:
    """Everything outside this module's logic. The CLI passes real ones; tests pass fakes."""
    run: object                          # run(argv, cwd=None) -> (returncode, output)
    health: object                       # health(url) -> dict | None
    ask: object                          # ask(question) -> bool
    on_github: object                    # on_github(slug, sha) -> bool
    say: object = print
    sleep: object = lambda _s: None
    stamp: object = lambda: "00000000T000000Z"
    exists: object = lambda p: Path(p).exists()
    read_text: object = lambda p: Path(p).read_text()
    writable: object = lambda p: False
    schema_cmd: list[str] = field(default_factory=lambda: [
        sys.executable, "-c", "import sys; from emporia.cli import main; sys.exit(main())"])


class Stop(Exception):
    """A problem: the message says what, and that nothing was changed or what state the relay is in."""


def _unit_fields(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in text.splitlines():
        key, sep, value = line.partition("=")
        if sep and key in ("User", "WorkingDirectory", "EnvironmentFile", "ReadWritePaths"):
            out[key] = value.strip()
    return out


def _sudo(argv: list[str], user: str = "") -> list[str]:
    return ["sudo", *(["-u", user] if user else []), *argv]


def update(o: Options, io: IO) -> int:
    say = io.say
    stamp = io.stamp()
    state = {"stopped": False, "schema_ran": False}
    old = ""
    before = ""
    backup = ""
    repo = o.repo
    data = o.folder / "data"
    db = data / "emporia.sqlite3"

    def step(title: str) -> None:
        say("")
        say(f"== {title}")

    def change(argv: list[str], what: str, *, cwd: Path | None = None) -> tuple[int, str]:
        """A command that changes something: printed, and run unless --check."""
        if o.check:
            say(f"   would run: {' '.join(argv)}")
            return 0, ""
        rc, out = io.run(argv, cwd=cwd)
        if rc != 0:
            say(f"   {what} failed (exit {rc}): {out.strip()[:400]}")
        return rc, out

    def rollback() -> None:
        say("")
        say("Rollback, if you need it (nothing was deleted):")
        say(f"  1. sudo systemctl stop {o.service}")
        if old and repo:
            say(f"  2. old code: git -C {repo} checkout --detach {old} && (cd {repo} && "
                f"uv sync --frozen --no-dev --extra x402 --extra relay)")
        if backup or o.check:
            where = backup or f"{o.backup_dir or o.folder.parent / 'emporia-backups'}/emporia-data-{stamp}"
            say(f"  3. only if the database upgrade ran: sudo mv {data} {data}.failed-{stamp} && "
                f"sudo cp -a {where} {data}")
        else:
            say("  3. (no data backup was made yet)")
        say(f"  4. sudo systemctl start {o.service}")
        say(f"  The update again: uvx --from git+https://github.com/{o.slug}@{o.to} emporia relay update --to {o.to}")

    try:
        step(f"1. the commit {o.to}")
        if not SHA_RE.fullmatch(o.to):
            raise Stop(f"a full 40-character commit sha is needed (got {o.to!r}); a short one can name two commits")
        if not io.on_github(o.slug, o.to):
            raise Stop(f"GitHub does not know commit {o.to} in {o.slug} (is it pushed? is it the full sha?)")
        say("   on GitHub: yes")

        step("2. this install")
        if io.exists(o.folder / "compose.snippet.yaml"):
            raise Stop(f"{o.folder} is a Docker relay. Update it with the script that ships at the target commit:\n"
                       f"     {SCRIPT_CMD.format(repo=o.slug, sha=o.to)}\n"
                       f"   (add --check first, and --sudo if your Docker needs sudo)")
        unit_path = o.folder / UNIT
        if not io.exists(unit_path):
            raise Stop(f"no {UNIT} in {o.folder}: this command updates a relay made by `emporia relay host --run "
                       f"service` (pass its folder with --dir)")
        fields = _unit_fields(io.read_text(unit_path))
        user = fields.get("User", "")
        if repo is None:
            repo = Path(fields["WorkingDirectory"]) if fields.get("WorkingDirectory") else None
        if repo is None or not io.exists(repo / ".git"):
            raise Stop(f"the service's code is not a git checkout ({repo}); pass its folder with --repo")
        say(f"   folder {o.folder}, code {repo}, service user {user or '?'}")
        rc, shown = io.run(["systemctl", "show", o.service, "-p", "User", "-p", "WorkingDirectory",
                            "-p", "ReadWritePaths"])
        live = {k: v.strip() for k, _, v in (ln.partition("=") for ln in shown.splitlines())}
        if rc != 0 or str(data) not in live.get("ReadWritePaths", "").split() \
                or live.get("WorkingDirectory", "") != str(repo):
            raise Stop(f"the installed service {o.service} does not use this folder's data and code "
                       f"(it shows {live or 'nothing'}; this folder is {data}, {repo}). Nothing was changed")
        say("   the installed service uses this folder's data and code: yes")
        h = io.health(o.url)
        if not h:
            raise Stop(f"the relay is not answering at {o.url}/health (nothing was changed)")
        before = f"{h.get('relay_id')} {h.get('relay_public_key')}"
        say(f"   identity: {before}; build {h.get('build')}; schema {h.get('schema_version')}")
        if not io.exists(db):
            raise Stop(f"no database at {db}")

        step("3. fetch the new code (the relay keeps serving)")
        rc, out = io.run(["git", "-C", str(repo), "status", "--porcelain", "--untracked-files=no"])
        if rc != 0 or out.strip():
            raise Stop(f"{repo} has changes of its own ({out.strip()[:300] or 'git failed'}); nothing was changed")
        rc, out = io.run(["git", "-C", str(repo), "rev-parse", "HEAD"])
        old = out.strip()
        say(f"   running now: {old}")
        if old == o.to:
            say(f"   already at {o.to}. Nothing to do.")
            return 0
        change(["git", "-C", str(repo), "fetch", "--quiet", "origin", o.to], "the fetch")
        if not o.check:
            rc, _ = io.run(["git", "-C", str(repo), "cat-file", "-e", f"{o.to}^{{commit}}"])
            if rc != 0:
                raise Stop(f"the commit {o.to} did not arrive in {repo} (nothing was changed)")
            say("   fetched")

        step("4. does the database need an upgrade? (dry run with the new code; changes nothing)")
        prefix = [] if io.writable(db) else _sudo([], user)
        upgrade = [*prefix, *io.schema_cmd, "relay", "upgrade", "--db", str(db)]
        rc, plan = io.run([*upgrade, "--dry-run"])
        say(plan.rstrip())
        if rc != 0:
            raise Stop("the new release refuses this database (see above); the relay was not stopped")
        need = NOTHING_TO_DO not in plan
        say("   this release upgrades the database" if need else "   no upgrade needed")

        step("5. stop the relay and take a full backup of its data")
        bdir = o.backup_dir or o.folder.parent / "emporia-backups"
        backup_path = bdir / f"emporia-data-{stamp}"
        rc, _ = change(_sudo(["systemctl", "stop", o.service]), "the stop")
        if rc != 0:
            raise Stop("could not stop the relay; nothing was changed")
        state["stopped"] = not o.check
        rc, _ = change(["mkdir", "-p", str(bdir)], "making the backup folder")
        if rc == 0:
            rc, _ = change(_sudo(["cp", "-a", str(data), str(backup_path)]), "the backup")
        if rc != 0:
            io.run(_sudo(["systemctl", "start", o.service]))
            state["stopped"] = False
            raise Stop("the backup failed; the old relay was started again and nothing was changed")
        backup = str(backup_path)
        say(f"   backup: {backup}")

        step("6. upgrade the database, only if the schema changes")
        if o.check:
            say("   would ask: apply the upgrade shown by the dry run? [y/N]   (only when step 4 shows one; --yes answers it)")
            say(f"   would run: {' '.join([*upgrade, '--yes'])}")
        elif need:
            if not (o.yes or io.ask("Apply the upgrade shown above?")):
                io.run(_sudo(["systemctl", "start", o.service]))
                state["stopped"] = False
                say(f"Stopped before changing anything; the old relay ({old[:8]}) is started again.")
                rollback()
                return 1
            rc, out = io.run([*upgrade, "--yes"])
            if rc != 0:
                say(f"!!! THE DATABASE UPGRADE FAILED. The relay is stopped.\n{out.strip()[:400]}")
                rollback()
                return 1
            state["schema_ran"] = True
            say("   upgraded")
        else:
            say("   not needed")

        step("7. the new code")
        rc, _ = change(["git", "-C", str(repo), "checkout", "--quiet", "--detach", o.to], "the checkout")
        if rc == 0:
            rc, _ = change(["uv", "sync", "--frozen", "--no-dev", "--extra", "x402", "--extra", "relay"],
                           "the install of its packages", cwd=repo)
        if rc != 0:
            io.run(["git", "-C", str(repo), "checkout", "--quiet", "--detach", old])
            if not state["schema_ran"]:
                io.run(_sudo(["systemctl", "start", o.service]))
                say("   the old code is back and the old relay is started again; nothing else changed")
            else:
                say("!!! the database was upgraded; the relay stays stopped")
            rollback()
            return 1
        if not o.check:
            say(f"   {repo} is at {o.to[:12]}; the build commit is read from its git data")

        step("8. start the new relay")
        change(_sudo(["systemctl", "start", o.service]), "the start")

        step("9. same relay?")
        if o.check:
            say(f"   would check: /health shows the same relay_id and relay_public_key as before, and build {o.to[:8]}")
            rollback()
            say("")
            say("--check: nothing was changed.")
            return 0
        after: dict = {}
        for _ in range(60):
            after = io.health(o.url) or {}
            if after:
                break
            io.sleep(3)
        shown = f"{after.get('relay_id')} {after.get('relay_public_key')}" if after else ""
        say(f"   before: {before}")
        say(f"   after:  {shown or '(not answering)'}")
        say(f"   build:  {after.get('build')} (wanted {o.to[:8]})")
        if shown == before and after.get("build") == o.to[:8]:
            say(f"SAME RELAY, NEW BUILD - DONE. Backup: {backup}")
            rollback()
            return 0
        say("!!! NOT AS EXPECTED: the relay's identity or its build differs from what was checked. Delete nothing.")
        rollback()
        return 2
    except Stop as e:
        say(f"STOP: {e}")
        if state["stopped"]:
            say("   The relay is stopped.")
        if backup or old:
            rollback()
        return 1
