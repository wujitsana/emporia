#!/usr/bin/env python3
"""The scripted end-to-end run: the Hermes plugin, a throwaway relay and three agents, no model.

The check to run before every push and release. It does by script what a person once did by
hand in a throwaway Hermes home:

* a fresh HERMES_HOME and HOME (both exported before every `hermes` call), a relay on a free
  port, a minimal `judge` profile with the plugin installed from this repo;
* `hermes -p judge emporia setup` for a player (A) and a merchant playing set plays (B): each has
  the marketplace role, its keys in its .env, the plugin enabled, registers, and passes doctor;
* playbook refusals that name how to change them; a free blotto between A and B (an illegal commit
  refused, a like, quests, the character sheet, the season, the relay's activity count);
* a house agent that adopts a waiting free blotto and plays it to the end;
* `hermes emporia update --runtime-from`, twice: the second changes nothing.

No model or LLM is called, no gateway is started. Everything is written under one temporary
folder this script makes (base: --workdir, default the system temp), and torn down at the end,
on an error and on Ctrl-C: only the processes it started (their recorded PIDs) are stopped.

The plugin and the runtime are installed from the repo's HEAD commit, as a user would get them:
commit first, or the run tests the last commit, not your working tree.

Usage:
    .venv/bin/python scripts/e2e.py [--keep] [--repo PATH] [--workdir DIR]

One line per step (✓ or ✗, with its time); the exit code is 0 only when every step passed. On a
failure it prints the log's path (the log is kept; --keep keeps everything).
"""
from __future__ import annotations

import argparse
import atexit
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path
from typing import Any

REPO_DEFAULT = Path(__file__).resolve().parent.parent
PLAYER, MERCHANT, HOUSE = "e2e-player", "e2e-merchant", "e2e-house"
SETUP_TIMEOUT = 900

# Runs inside an agent's own runtime (its python, its MCP server's env, as Hermes starts it) and
# answers tool calls: one JSON line in ({"tool", "args", "kwargs"}), one JSON line out. Nothing
# here reads or prints a key: the MCP module loads the profile's .env itself (EMPORIA_DOTENV).
WORKER = r'''
import json, os, sys
out = os.fdopen(os.dup(1), "w", buffering=1)
os.dup2(2, 1)                       # anything the tools print goes to the log, not the protocol
import emporia.mcp_server as m
out.write(json.dumps({"ready": True}) + "\n")
for line in sys.stdin:
    req = json.loads(line)
    try:
        fn = getattr(m, req["tool"])
        fn = getattr(fn, "fn", fn)
        res = {"ok": fn(*req.get("args", []), **req.get("kwargs", {}))}
    except Exception as exc:        # a tool that raised is a result the run reports
        res = {"raised": f"{type(exc).__name__}: {exc}"}
    out.write(json.dumps(res, default=str) + "\n")
'''


class StepFailed(Exception):
    pass


def check(cond: Any, what: str) -> None:
    if not cond:
        raise StepFailed(what)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Run:
    def __init__(self, repo: Path, workdir: str | None, keep: bool):
        self.repo = repo
        self.keep = keep
        self.root = Path(tempfile.mkdtemp(prefix="emporia-e2e-", dir=workdir))
        self.hh = self.root / "hermes"           # HERMES_HOME
        self.home = self.root / "home"           # HOME
        self.relay_dir = self.root / "relay"
        self.log_path = self.root / "e2e.log"
        for d in (self.hh, self.home, self.relay_dir):
            d.mkdir()
        self.log = self.log_path.open("a", encoding="utf-8")
        self.pids: list[int] = []                # every process this run started and still owns
        self.procs: dict[int, subprocess.Popen] = {}
        self.workers: dict[str, subprocess.Popen] = {}
        self.failed = 0
        self.timings: list[tuple[str, float, bool]] = []
        self.relay_url = ""
        self.hermes = shutil.which("hermes") or ""
        self.sha = ""
        self._torn_down = False

    # ── output ──────────────────────────────────────────────────────────────
    def note(self, text: str) -> None:
        self.log.write(text.rstrip() + "\n")
        self.log.flush()

    def step(self, name: str, fn, *args) -> Any:
        self.note(f"\n=== {name}")
        t0 = time.monotonic()
        try:
            value = fn(*args)
        except StepFailed as exc:
            ok, value, why = False, None, str(exc)
        except subprocess.TimeoutExpired as exc:
            ok, value, why = False, None, f"timed out: {' '.join(map(str, exc.cmd))[:120]}"
        else:
            ok, why = True, ""
        took = time.monotonic() - t0
        self.timings.append((name, took, ok))
        print(f"{'✓' if ok else '✗'} {name}  ({took:.1f}s)" + (f"\n    {why}" if why else ""), flush=True)
        if not ok:
            self.note(f"FAILED: {why}")
            self.failed += 1
            raise StepFailed(name)
        return value

    # ── environments ────────────────────────────────────────────────────────
    def env(self, **extra: str) -> dict[str, str]:
        """This process's environment, without anything that would make a child act as a person's
        own relay, agent or Hermes home, and with the temp HERMES_HOME and HOME (always both)."""
        drop = ("EMPORIA_", "STRIPE_", "FEDERATED_", "OPERATOR_", "XDG_")
        env = {k: v for k, v in os.environ.items()
               if not k.startswith(drop) and k not in ("PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV")}
        env.update(HERMES_HOME=str(self.hh), HOME=str(self.home))
        # Belt and braces: anything that falls back to a default relay finds this one, never a
        # person's (the default is localhost:8088).
        if self.relay_url:
            env["EMPORIA_RELAY_URL"] = self.relay_url
        env.update(extra)
        return env

    def run(self, argv: list[str], timeout: float = 300, env: dict | None = None,
            ok_codes: tuple[int, ...] | None = (0,)) -> subprocess.CompletedProcess:
        self.note("$ " + " ".join(argv))
        p = subprocess.run(argv, env=env or self.env(), cwd=self.root, stdin=subprocess.DEVNULL,
                           capture_output=True, text=True, timeout=timeout)
        self.note((p.stdout or "") + (p.stderr or "") + f"[exit {p.returncode}]")
        if ok_codes is not None and p.returncode not in ok_codes:
            tail = (p.stdout + p.stderr).strip().splitlines()[-3:]
            raise StepFailed(f"`{' '.join(argv[:5])}` exited {p.returncode}: " + " | ".join(tail))
        return p

    def hermes_cmd(self, *args: str, timeout: float = 300, ok_codes=(0,)) -> subprocess.CompletedProcess:
        check(self.hermes, "hermes is not on PATH")
        return self.run([self.hermes, *args], timeout=timeout, ok_codes=ok_codes)

    def profile(self, name: str) -> Path:
        return self.hh / "profiles" / name

    def runtime_python(self) -> Path:
        return self.profile("judge") / "plugin-data" / "emporia" / "runtime" / ".venv" / "bin" / "python"

    # ── steps ───────────────────────────────────────────────────────────────
    def start_relay(self) -> str:
        port = free_port()
        while port == 8088:                       # never the dev relay's port
            port = free_port()
        self.relay_url = f"http://127.0.0.1:{port}"
        r = self.relay_dir
        env = self.env(
            EMPORIA_NO_DOTENV="1", HERMES_HOME=str(r / "hermes"),
            EMPORIA_RELAY_HOST="127.0.0.1", EMPORIA_RELAY_PORT=str(port), EMPORIA_RELAY_URL=self.relay_url,
            EMPORIA_RELAY_ID="e2e", EMPORIA_RELAY_ACCESS="open", EMPORIA_DB_PATH=str(r / "relay.sqlite3"),
            EMPORIA_KEYS_DIR=str(r / "keys"), EMPORIA_LOG_DIR=str(r / "logs"), EMPORIA_AUDIT_DIR=str(r / "audit"),
            EMPORIA_X402_ENABLED="0", RATE_LIMIT_PER_MINUTE="5000",
            EMPORIA_GAMIFY="sheets,quests", EMPORIA_HOUSE_AGENTS=HOUSE)
        py = self.repo / ".venv" / "bin" / "python"
        out = (r / "relay.log").open("ab")
        # The interpreter itself, no shell in between: the PID recorded is the relay's.
        proc = subprocess.Popen([str(py if py.exists() else sys.executable), "-m", "emporia.relay_server"],
                                cwd=self.root, env=env, stdin=subprocess.DEVNULL, stdout=out, stderr=out)
        self.pids.append(proc.pid)
        self.procs[proc.pid] = proc
        deadline = time.time() + 60
        while time.time() < deadline:
            check(proc.poll() is None, f"the relay exited ({proc.returncode}); see {r / 'relay.log'}")
            try:
                with urllib.request.urlopen(self.relay_url + "/health", timeout=1) as resp:
                    if resp.status == 200:
                        break
            except OSError:
                time.sleep(0.3)
        else:
            raise StepFailed("the relay did not answer /health in 60s")
        served = [line for line in (r / "relay.log").read_text(errors="replace").splitlines()
                  if "Started server process [" in line]
        if served:
            pid = int(served[-1].split("[", 1)[1].split("]", 1)[0])
            check(pid == proc.pid, f"the relay serves from pid {pid}, not the {proc.pid} started")
        self.note(f"relay {self.relay_url} pid {proc.pid}")
        return f"pid {proc.pid}"

    def make_judge(self) -> None:
        self.hermes_cmd("profile", "create", "judge", "--no-skills")
        self.hermes_cmd("-p", "judge", "plugins", "install", self.plugin_source(), "--enable", timeout=600)
        check((self.profile("judge") / "plugins" / "emporia" / "plugin.yaml").is_file(),
              "the plugin is not in judge's plugins/")

    def runtime_from(self) -> str:
        """The repo at the commit this run started at: another commit landing mid-run moves nothing."""
        return f"{self.repo}@{self.sha}"

    def plugin_source(self) -> str:
        return f"file://{self.repo}#hermes_plugin/emporia"

    def setup_agent(self, name: str, *extra: str) -> None:
        self.hermes_cmd("-p", "judge", "emporia", "setup", "--profile", name, "--relay", self.relay_url,
                        "--runtime-from", self.runtime_from(), "--plugin-source", self.plugin_source(),
                        "--", "--role", "marketplace", "--keys", "env", *extra, "--register",
                        timeout=SETUP_TIMEOUT)

    def emporia(self, profile: str, *args: str, ok_codes=(0,), timeout: float = 120) -> str:
        """`hermes -p PROFILE emporia …`: the plugin's own commands, and the runtime's CLI
        passed through for this profile."""
        p = self.hermes_cmd("-p", profile, "emporia", *args, timeout=timeout, ok_codes=ok_codes)
        return p.stdout + p.stderr

    def check_agent(self, name: str) -> None:
        import yaml
        cfg = yaml.safe_load((self.profile(name) / "config.yaml").read_text()) or {}
        check("emporia" in ((cfg.get("plugins") or {}).get("enabled") or []), "plugins.enabled has no emporia")
        off = set((cfg.get("agent") or {}).get("disabled_toolsets") or [])
        check({"terminal", "file"} <= off, f"terminal and file tools are not off: {sorted(off)}")
        role = self.emporia(name, "role", name)
        check("role marketplace" in role, f"not the marketplace role: {role.strip()[:200]}")
        keys = self.emporia(name, "identity", "show", name)
        check(keys.count("linked to an environment variable") >= 2 and "file" not in keys.split("\n", 1)[1],
              f"its keys are not in its .env: {keys.strip()[-200:]}")
        status = self.emporia(name, "status")
        check("Relay   : registered" in status, f"status: {status.strip()[-200:]}")
        check(f"on {self.relay_url}" in status, "status names another relay")
        self.emporia(name, "doctor")

    # ── tools, as the agent ─────────────────────────────────────────────────
    def worker(self, name: str) -> subprocess.Popen:
        if name in self.workers and self.workers[name].poll() is None:
            return self.workers[name]
        import yaml
        cfg = yaml.safe_load((self.profile(name) / "config.yaml").read_text()) or {}
        server = (cfg.get("mcp_servers") or {}).get("emporia") or {}
        check(server.get("command"), f"{name} has no emporia MCP server")
        env = {k: v for k, v in self.env().items() if k != "EMPORIA_RELAY_URL"}
        env.update({k: str(v) for k, v in (server.get("env") or {}).items()})
        env["HERMES_HOME"] = str(self.profile(name))
        err = (self.root / f"worker-{name}.log").open("ab")
        proc = subprocess.Popen([server["command"], "-c", WORKER], cwd=self.root, env=env, text=True,
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=err, bufsize=1)
        self.pids.append(proc.pid)
        self.procs[proc.pid] = proc
        self.workers[name] = proc
        hello = proc.stdout.readline()
        check(hello.strip() == '{"ready": true}', f"{name}'s tools did not start; see worker-{name}.log")
        return proc

    def tool(self, agent: str, name: str, *args, **kwargs) -> Any:
        proc = self.worker(agent)
        proc.stdin.write(json.dumps({"tool": name, "args": list(args), "kwargs": kwargs}) + "\n")
        proc.stdin.flush()
        line = proc.stdout.readline()
        check(line, f"{agent}'s tools stopped; see worker-{agent}.log")
        res = json.loads(line)
        self.note(f"{agent}: {name}{tuple(args)} {kwargs or ''}\n  -> {json.dumps(res, default=str)[:1500]}")
        check("raised" not in res, f"{name} raised {res.get('raised')}")
        return res["ok"]

    def act(self, agent: str, sid: str, moves: dict[str, list[int]]) -> dict:
        """The next commit or reveal in a blotto session, by whoever is to move."""
        s = self.tool(agent, "get_session", sid)
        phase = s["state"]["data"]["phase"]
        mover = s["current_agent"]
        if phase == "commit":
            r = self.tool(mover, "commit_move", sid, moves[mover], "committing my planned spread")
        else:
            r = self.tool(mover, "reveal_move", sid, "revealing the move I committed")
        check(r.get("success"), f"{mover}'s {phase}: {r}")
        return r

    # ── the flow ────────────────────────────────────────────────────────────
    def refusals(self) -> None:
        sell = self.tool(PLAYER, "create_listing", "E2E service", description="a test listing")
        check(not sell.get("success") and "does not sell" in sell.get("error", ""), f"player sold: {sell}")
        chess = self.tool(MERCHANT, "create_session", "emporia:chess:v1")
        check(not chess.get("success") and "does not take emporia:chess:v1" in chess.get("error", ""),
              f"merchant made a chess game: {chess}")
        for r in (sell, chess):
            check("agent playbook" in r["error"], f"the refusal names no way to change it: {r['error']}")

    def blotto(self) -> str:
        s = self.tool(PLAYER, "create_session", "emporia:blotto:v1",
                      config={"rounds": 1, "battlefields": 3, "troops": 10, "opponent": MERCHANT})
        check(s.get("success"), f"create_session: {s}")
        sid = s["session_id"]
        j = self.tool(MERCHANT, "join_session", sid)
        check(j.get("success"), f"join_session: {j}")
        moves = {PLAYER: [6, 4, 0], MERCHANT: [4, 3, 3]}
        for _ in range(4):
            r = self.act(PLAYER, sid, moves)
        check(r.get("is_terminal") and (r.get("outcome") or {}).get("winner") == PLAYER, f"the end: {r}")
        return sid

    def illegal_commit(self) -> None:
        s = self.tool(PLAYER, "create_session", "emporia:blotto:v1",
                      config={"battlefields": 3, "troops": 10, "opponent": MERCHANT})
        sid = s["session_id"]
        self.tool(MERCHANT, "join_session", sid)
        bad = self.tool(PLAYER, "commit_move", sid, [50, 50], "everything on two of the fields")
        check(not bad.get("success") and "not a legal move" in bad.get("error", ""), f"illegal commit: {bad}")
        missing = self.tool(PLAYER, "commit_move", "sess_nope", [4, 3, 3], "a session that is not there")
        check(not missing.get("success") and "no session sess_nope" in missing.get("error", ""),
              f"commit to a missing session: {missing}")

    def like(self, sid: str) -> None:
        r = self.tool(PLAYER, "like", "game", sid)
        check(r.get("success") and r.get("to_agent") == MERCHANT, f"like: {r}")
        back = self.tool(MERCHANT, "like", "game", sid)
        check(back.get("success") and back.get("to_agent") == PLAYER, f"like back: {back}")

    def standing(self) -> None:
        q = self.tool(PLAYER, "my_quests")
        done = {x["id"] for x in q.get("quests", []) if x.get("done")}
        check("first-set-play" in done, f"quests done: {sorted(done)}")
        sheet = self.tool(PLAYER, "character_sheet")
        craft = {c["module_type"]: c for c in sheet.get("craft", [])}
        check("rating" in craft.get("emporia:blotto:v1", {}), f"no Craft blotto rating: {sheet.get('craft')}")
        renown = sheet.get("renown") or {}
        check(renown.get("likes") == 1 and "swap cancels" in " ".join(renown.get("why") or []),
              f"Renown does not say why a swapped like counts nothing: {renown}")
        shown = self.emporia(PLAYER, "sheet", PLAYER)
        check("Renown" in shown and "swap cancels" in shown, f"emporia sheet: {shown.strip()[-300:]}")
        season = self.tool(PLAYER, "season")
        check(season.get("boards", {}).get("emporia:blotto:v1"), f"no blotto ladder: {season.get('boards')}")
        with urllib.request.urlopen(self.relay_url + "/relay/activity", timeout=5) as resp:
            act = json.loads(resp.read())
        check(act.get("active_agents") == 2, f"/relay/activity: {act}")

    def house(self) -> None:
        s = self.tool(PLAYER, "create_session", "emporia:blotto:v1", config={"battlefields": 3, "troops": 10})
        check(s.get("success"), f"create_session: {s}")
        sid = s["session_id"]
        time.sleep(2.5)                           # waiting longer than --adopt-after 2
        moves = {PLAYER: [5, 5, 0]}
        for _ in range(8):
            out = self.emporia(HOUSE, "house", "run", HOUSE, "--once", "--adopt-after", "2")
            check("HTTP Request" not in out, "house run logs every HTTP request")
            g = self.tool(PLAYER, "get_session", sid)
            if g["status"] == "completed":
                break
            check(HOUSE in g.get("participants", []), f"the house did not adopt the game: {g.get('participants')}")
            if g["current_agent"] == PLAYER:
                self.act(PLAYER, sid, moves)
        else:
            raise StepFailed("the house's game did not finish in 8 passes")
        check(HOUSE in g["participants"], f"participants: {g['participants']}")

    def update(self) -> None:
        rt = self.runtime_python().parent.parent.parent
        before = self.run(["git", "-C", str(rt), "rev-parse", "HEAD"]).stdout.strip()
        first = self.emporia(PLAYER, "update", "--runtime-from", self.runtime_from(), timeout=600)
        second = self.emporia(PLAYER, "update", "--runtime-from", self.runtime_from(), timeout=600)
        after = self.run(["git", "-C", str(rt), "rev-parse", "HEAD"]).stdout.strip()
        check(before == after == self.sha, f"runtime moved: {before[:12]} -> {after[:12]} (HEAD {self.sha[:12]})")
        dirty = self.run(["git", "-C", str(rt), "status", "--porcelain", "--untracked-files=no"]).stdout.strip()
        check(not dirty, f"the runtime's files changed: {dirty[:200]}")
        for out in (first, second):
            check("first time" not in out and "gateway restart" not in out,
                  f"update with nothing to do still says: {out.strip()[-200:]}")
        check("Relay   : registered" in self.emporia(PLAYER, "status"), "status after update")

    # ── teardown ────────────────────────────────────────────────────────────
    def teardown(self) -> None:
        if self._torn_down:
            return
        self._torn_down = True
        t0 = time.monotonic()
        for name, proc in list(self.workers.items()):
            try:
                proc.stdin.close()
            except OSError:
                pass
        for pid in reversed(self.pids):
            proc = self.procs[pid]
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=5)
        alive = [pid for pid in self.pids if self.procs[pid].poll() is None]
        left = self._left_running()
        self.note(f"\nstopped: {self.pids}; still alive: {alive}; others naming the run's folder: {left}")
        ok = not alive and not left
        try:
            self.log.close()
        except OSError:
            pass
        kept = self.keep or self.failed or not ok
        if self.keep:
            pass
        elif kept:
            for d in (self.hh, self.home, self.relay_dir):
                shutil.rmtree(d, ignore_errors=True)
        else:
            shutil.rmtree(self.root, ignore_errors=True)
        took = time.monotonic() - t0
        what = f"stopped {len(self.pids)} processes" + (f"; still running: {alive + left}" if not ok else "")
        print(f"{'✓' if ok else '✗'} tear down: {what}  ({took:.1f}s)", flush=True)
        if not ok:
            self.failed += 1

    def _left_running(self) -> list[int]:
        """Processes not started here that name this run's folder: reported, never killed."""
        mine, found = set(self.pids) | {os.getpid()}, []
        needle = str(self.root).encode()
        for entry in Path("/proc").glob("[0-9]*"):
            try:
                if int(entry.name) not in mine and needle in (entry / "cmdline").read_bytes():
                    found.append(int(entry.name))
            except (OSError, ValueError):
                continue
        return found


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--repo", default=str(REPO_DEFAULT), help="the Emporia repo to test (default: this one)")
    ap.add_argument("--workdir", default=None, help="where the run's temp folder goes (default: the system temp)")
    ap.add_argument("--keep", action="store_true", help="keep the temp homes, relay and log")
    args = ap.parse_args(argv)
    repo = Path(args.repo).expanduser().resolve()
    if not (repo / "hermes_plugin" / "emporia" / "plugin.yaml").is_file():
        print(f"{repo} is not an Emporia repo")
        return 2
    run = Run(repo, args.workdir, args.keep)
    atexit.register(run.teardown)

    def on_term(signum, _frame):
        raise KeyboardInterrupt(f"signal {signum}")
    signal.signal(signal.SIGTERM, on_term)
    signal.signal(signal.SIGHUP, on_term)

    run.sha = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True,
                             text=True).stdout.strip()
    dirty = subprocess.run(["git", "-C", str(repo), "status", "--porcelain", "--untracked-files=no"],
                           capture_output=True, text=True).stdout.strip()
    print(f"Emporia end-to-end run: {repo} @ {run.sha[:12]}  (folder {run.root})")
    if dirty:
        print("  note: uncommitted changes are not in this run; it installs HEAD")
    t0 = time.monotonic()
    interrupted = False
    try:
        run.step("temp HERMES_HOME and HOME", lambda: None)
        run.step("throwaway relay", run.start_relay)
        run.step("judge profile with the plugin", run.make_judge)
        run.step(f"setup {PLAYER} (player)", run.setup_agent, PLAYER, "--playbook", "player")
        run.step(f"setup {MERCHANT} (merchant)", run.setup_agent, MERCHANT, "--playbook", "merchant")
        run.step(f"{MERCHANT} plays set plays", run.emporia, MERCHANT, "agent", "playbook", MERCHANT,
                 "--games", "set-plays")
        for name in (PLAYER, MERCHANT):
            run.step(f"{name}: role, keys, plugin, registered, doctor", run.check_agent, name)
        run.step("playbook refusals name their fix", run.refusals)
        sid = run.step("free blotto, end to end", run.blotto)
        run.step("illegal commit and missing session refused", run.illegal_commit)
        run.step("likes", run.like, sid)
        run.step("quests, sheet, season, activity", run.standing)
        run.step(f"setup {HOUSE} (house)", run.setup_agent, HOUSE, "--playbook", "host")
        run.step("house adopts a waiting blotto and finishes it", run.house)
        run.step("update --runtime-from is idempotent", run.update)
    except StepFailed:
        pass
    except KeyboardInterrupt:
        interrupted = True
        print("✗ interrupted", flush=True)
        run.failed += 1
    finally:
        run.teardown()
    total = time.monotonic() - t0
    if run.failed:
        print(f"FAILED ({'interrupted; ' if interrupted else ''}{total:.0f}s). Log: {run.log_path}")
        return 1
    print(f"All steps passed in {total:.0f}s." + (f" Kept: {run.root}" if args.keep else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
