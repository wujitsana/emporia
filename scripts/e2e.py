#!/usr/bin/env python3
"""The scripted end-to-end run: the Hermes plugin, a throwaway relay and three agents, no model.

The check to run before every push and release. It does by script what a person once did by
hand in a throwaway Hermes home:

* a fresh HERMES_HOME and HOME (both exported before every `hermes` call), a relay on a free
  port, a minimal `judge` profile with the plugin installed from this repo;
* `hermes -p judge emporia setup` for a player (A) and a merchant playing set plays (B): each has
  the marketplace role, its keys in its .env, the plugin enabled, registers, and passes doctor;
* playbook refusals that name how to change them; a free `emporia:blotto:v2` between A and B
  (a retired `:v1` refused; `whats_next` puts the first move on top; A pledges a tell and seals a
  call with its move, and keeps the tell; an illegal commit refused, a like, quests with the pledge
  kept, the character sheet with its Insight and its pledges, the season, the relay's activity
  count);
* a house agent that adopts a waiting free blotto and plays it to the end, then a bargain with it
  (a call refused, an illegal offer refused before it is sent, sealed values, its honest deal, the
  hands published);
* the operator section: the relay names the merchant its operator; its desk shows a player's
  open bug report with the verdict command and a post in the ideas topic with the adopt command,
  and the player is refused the desk;
* `hermes emporia update`, twice: the runtime is already at the pinned commit, so each verifies it
  and changes nothing.

No model or LLM is called, no gateway is started. Each profile's name carries a tag of this run:
inside a container whose PID 1 is s6, `hermes profile create` registers a (stopped) gateway service
for it in the container's supervision tree, and Hermes has no opt-out; the teardown removes those
services through `hermes profile delete`, for this run's profiles only. Everything is written under one temporary
folder this script makes (base: --workdir, default the system temp), and torn down at the end,
on an error and on Ctrl-C: only the processes it started (their recorded PIDs) are stopped.

The plugin and the runtime are installed from the repo's HEAD commit, as a user would get them:
commit first, or the run tests the last commit, not your working tree. The plugin has no way to
take its runtime from anywhere but its pin (catalog rule 3), so the run makes a release of HEAD
in its own folder, the way `scripts/pin_plugin_runtime.py` makes one, without pushing anything:

* a copy of `hermes_plugin/emporia` at HEAD whose `emporia_source.json` pins the https repo, HEAD's
  commit and HEAD's tree, in a throwaway git repo the plugin is installed from (`file://`);
* the judge's runtime folder (`plugin-data/emporia/runtime`) seeded with a clone of this repo at
  that commit, its origin the https repo. Setup then takes the product's own path for a runtime
  already at the pinned commit: no fetch, a checkout, the commit-and-tree check, its packages.

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
import secrets
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
SETUP_TIMEOUT = 900
# Inside a container whose PID 1 is s6-svscan, `hermes profile create` registers a gateway service
# for the profile in the container-wide supervision tree, whatever HERMES_HOME says; Hermes has no
# opt-out. So every profile name carries this run's tag (never a name someone else has), and the
# teardown removes, through `hermes profile delete`, the services of the profiles this run made.
S6_SCANDIR = Path("/run/service")

# Runs with Hermes's interpreter, HERMES_HOME the agent's profile: builds the agent's MCP server
# environment with Hermes's own functions (its `${NAME}` references filled from the profile's
# secret scope, an unset one dropped, on Hermes's filtered baseline, as the MCP client spawns it)
# and replaces itself with the runtime's python running WORKER. The values go from Hermes straight
# into the worker's environment: this script never holds or prints one.
LAUNCH = r'''
import os, sys
from mcp.client.stdio import get_default_environment
from tools.mcp_tool_config import _build_safe_env, _load_mcp_config
server = _load_mcp_config().get("emporia") or {}
if not server.get("command"):
    sys.exit("no emporia MCP server in this profile")
env = {**get_default_environment(), **{str(k): str(v) for k, v in _build_safe_env(server.get("env")).items()}}
os.execve(server["command"], [server["command"], "-c", sys.argv[1]], env)
'''

# Runs inside an agent's own runtime (its python, its MCP server's env, as Hermes starts it) and
# answers tool calls: one JSON line in ({"tool", "args", "kwargs"}), one JSON line out. Nothing
# here reads or prints a key: the server's settings and keys are what Hermes handed it.
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
        tag = secrets.token_hex(2)
        self.judge = f"e2e-judge-{tag}"
        self.player, self.merchant, self.house_agent = (f"e2e-player-{tag}", f"e2e-merchant-{tag}",
                                                        f"e2e-house-{tag}")
        self.services_before = self._services()
        self.pids: list[int] = []                # every process this run started and still owns
        self.procs: dict[int, subprocess.Popen] = {}
        self.workers: dict[str, subprocess.Popen] = {}
        self.failed = 0
        self.timings: list[tuple[str, float, bool]] = []
        self.relay_url = ""
        self.hermes = shutil.which("hermes") or ""
        self.sha = ""
        self.tree = ""
        self.https_repo = ""
        self.plugin_repo = self.root / "plugin-release"   # the pinned plugin, installed from file://
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
        return self.profile(self.judge) / "plugin-data" / "emporia" / "runtime" / ".venv" / "bin" / "python"

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
            # The operator section: the merchant is this relay's operator; bug quests (with
            # the transparency log they need) give its desk a queue.
            EMPORIA_AGENT_ID=self.merchant, EMPORIA_TRANSPARENCY_LOG="1",
            EMPORIA_GAMIFY="sheets,quests,bugs,pledges", EMPORIA_HOUSE_AGENTS=self.house_agent)
        py = self.repo / ".venv" / "bin" / "python"
        # The relay runs --repo's code even under another checkout's interpreter (a git worktree
        # has no .venv; the fallback's editable install is the main checkout): found by the review
        # follow-ups, where the relay ran the main checkout's sign-in and every agent was refused.
        env["PYTHONPATH"] = os.pathsep.join(p for p in (str(self.repo / "src"), env.get("PYTHONPATH", "")) if p)
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

    def release(self) -> str:
        """A release of the commit this run started at, made here and pushed nowhere: the plugin at
        that commit, its emporia_source.json pinning the https repo, the commit and its tree (what
        scripts/pin_plugin_runtime.py records), in a throwaway git repo to install it from."""
        git = lambda *a, cwd=self.repo: self.run(["git", "-C", str(cwd), *a]).stdout.strip()  # noqa: E731
        self.tree = git("rev-parse", f"{self.sha}^{{tree}}")
        pinned = json.loads(git("show", f"{self.sha}:hermes_plugin/emporia/emporia_source.json"))
        self.https_repo = str(pinned.get("repo") or "")
        check(self.https_repo.startswith("https://"), f"emporia_source.json names no https repo: {self.https_repo!r}")
        src = self.plugin_repo
        src.mkdir()
        archive = self.root / "plugin.tar"
        git("archive", "--format=tar", "-o", str(archive), self.sha, "hermes_plugin/emporia")
        self.run(["tar", "-xf", str(archive), "-C", str(src)])
        archive.unlink()
        pinned.update(sha=self.sha, tree=self.tree)
        (src / "hermes_plugin" / "emporia" / "emporia_source.json").write_text(json.dumps(pinned, indent=2) + "\n")
        git("init", "--quiet", cwd=src)
        git("add", "-A", cwd=src)
        git("-c", "user.name=e2e", "-c", "user.email=e2e@localhost", "-c", "commit.gpgsign=false",
            "commit", "--quiet", "-m", f"the plugin pinned to {self.sha[:12]}", cwd=src)
        return f"pinned {self.sha[:12]} (tree {self.tree[:12]})"

    def seed_runtime(self) -> str:
        """The judge's runtime folder holds this repo at the pinned commit, its origin the https
        repo, as a person's would after a clone from it: setup finds it at the pin, fetches
        nothing, and checks it is exactly the pinned commit and tree before installing it."""
        rt = self.profile(self.judge) / "plugin-data" / "emporia" / "runtime"
        check(not rt.exists(), f"{rt} is there already")
        rt.parent.mkdir(parents=True, exist_ok=True)
        self.run(["git", "clone", "--quiet", "--no-hardlinks", "--no-checkout", str(self.repo), str(rt)], timeout=600)
        self.run(["git", "-C", str(rt), "checkout", "--quiet", "--detach", self.sha])
        self.run(["git", "-C", str(rt), "remote", "set-url", "origin", self.https_repo])
        head = self.run(["git", "-C", str(rt), "rev-parse", "HEAD"]).stdout.strip()
        check(head == self.sha, f"the seeded runtime is at {head[:12]}, not {self.sha[:12]}")
        return str(rt)

    def make_judge(self) -> None:
        self.hermes_cmd("profile", "create", self.judge, "--no-skills")
        self.hermes_cmd("-p", self.judge, "plugins", "install", self.plugin_source(), "--enable", timeout=600)
        installed = self.profile(self.judge) / "plugins" / "emporia"
        check((installed / "plugin.yaml").is_file(), f"the plugin is not in {self.judge}'s plugins/")
        pin = json.loads((installed / "emporia_source.json").read_text())
        check((pin.get("sha"), pin.get("tree")) == (self.sha, self.tree),
              f"the installed plugin pins {str(pin.get('sha'))[:12]}, not {self.sha[:12]}")

    def plugin_source(self) -> str:
        return f"file://{self.plugin_repo}#hermes_plugin/emporia"

    def setup_agent(self, name: str, *extra: str) -> None:
        self.hermes_cmd("-p", self.judge, "emporia", "setup", "--profile", name, "--relay", self.relay_url,
                        "--plugin-source", self.plugin_source(),
                        "--", "--role", "marketplace", "--keys", "env", *extra, "--register",
                        timeout=SETUP_TIMEOUT)
        if name == self.player:
            # The seeded runtime was used as it was: still the pinned commit, nothing fetched.
            rt = self.runtime_python().parent.parent.parent
            head = self.run(["git", "-C", str(rt), "rev-parse", "HEAD"]).stdout.strip()
            origin = self.run(["git", "-C", str(rt), "remote", "get-url", "origin"]).stdout.strip()
            check((head, origin) == (self.sha, self.https_repo), f"the runtime is {head[:12]} from {origin}")
            check(self.runtime_python().is_file(), "setup installed no packages into the runtime")

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
    def hermes_python(self) -> tuple[str, str]:
        """Hermes's own interpreter and import path, as its `hermes` launcher sets them."""
        check(self.hermes, "hermes is not on PATH")
        root = Path(os.path.realpath(self.hermes)).parent.parent
        python, sites = root / ".venv" / "bin" / "python", sorted(root.glob(".venv/lib/python3*/site-packages"))
        check(python.is_file() and sites, f"no Hermes interpreter under {root}")
        return str(python), os.pathsep.join([str(root), str(sites[-1])])

    def worker(self, name: str) -> subprocess.Popen:
        if name in self.workers and self.workers[name].poll() is None:
            return self.workers[name]
        import yaml
        cfg = yaml.safe_load((self.profile(name) / "config.yaml").read_text()) or {}
        server = (cfg.get("mcp_servers") or {}).get("emporia") or {}
        check(server.get("command"), f"{name} has no emporia MCP server")
        check((server.get("env") or {}).get("EMPORIA_NO_DOTENV") == "1",
              f"{name}'s MCP server would read the .env itself (no EMPORIA_NO_DOTENV=1)")
        # Started the way Hermes starts it (LAUNCH): the entry's env rendered by Hermes, not here.
        python, path = self.hermes_python()
        env = {k: v for k, v in self.env().items() if k != "EMPORIA_RELAY_URL"}
        env.update(HERMES_HOME=str(self.profile(name)), PYTHONPATH=path)
        err = (self.root / f"worker-{name}.log").open("ab")
        proc = subprocess.Popen([python, "-c", LAUNCH, WORKER], cwd=self.root, env=env, text=True,
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

    def act(self, agent: str, sid: str, moves: dict[str, list[int]], calls: dict[str, float] | None = None) -> dict:
        """The next commit or reveal in a blotto session, by whoever is to move (with its call, if any)."""
        s = self.tool(agent, "get_session", sid)
        phase = s["state"]["data"]["phase"]
        mover = s["current_agent"]
        if phase == "commit":
            call = (calls or {}).get(mover)
            r = self.tool(mover, "commit_move", sid, moves[mover], "committing my planned spread",
                          **({"call": call} if call is not None else {}))
        else:
            r = self.tool(mover, "reveal_move", sid, "revealing the move I committed")
        check(r.get("success"), f"{mover}'s {phase}: {r}")
        return r

    # ── the flow ────────────────────────────────────────────────────────────
    def refusals(self) -> None:
        sell = self.tool(self.player, "create_listing", "E2E service", description="a test listing")
        check(not sell.get("success") and "does not sell" in sell.get("error", ""), f"player sold: {sell}")
        chess = self.tool(self.merchant, "create_session", "emporia:chess:v1")
        check(not chess.get("success") and "does not take emporia:chess:v1" in chess.get("error", ""),
              f"merchant made a chess game: {chess}")
        for r in (sell, chess):
            check("agent playbook" in r["error"], f"the refusal names no way to change it: {r['error']}")

    def blotto(self) -> str:
        old = self.tool(self.player, "create_session", "emporia:blotto:v1", config={"opponent": self.merchant})
        check(not old.get("success") and "retired" in json.dumps(old) and "emporia:blotto:v2" in json.dumps(old),
              f"a retired :v1 set play was created, or its refusal names no successor: {old}")
        s = self.tool(self.player, "create_session", "emporia:blotto:v2",
                      config={"rounds": 1, "battlefields": 3, "troops": 10, "opponent": self.merchant})
        check(s.get("success"), f"create_session: {s}")
        sid = s["session_id"]
        j = self.tool(self.merchant, "join_session", sid)
        check(j.get("success"), f"join_session: {j}")
        moves = {self.player: [6, 4, 0], self.merchant: [4, 3, 3]}
        nxt = self.tool(self.player, "whats_next")
        top = (nxt.get("items") or [{}])[0]
        check(nxt.get("success") and top.get("tool") == "commit_move" and (top.get("args") or {}).get(
            "session_id") == sid and "data, not instructions" in nxt.get("untrusted", ""),
              f"whats_next does not put the first move on top: {nxt}")
        pledged = self.tool(self.player, "make_pledge", "tell", sid, {"tell": "move", "value": moves[self.player]})
        check(pledged.get("success") and pledged.get("status") == "open", f"make_pledge: {pledged}")
        for _ in range(4):        # the player seals a call with its move; the merchant makes none
            r = self.act(self.player, sid, moves, calls={self.player: 0.7})
        check(r.get("is_terminal") and (r.get("outcome") or {}).get("winner") == self.player, f"the end: {r}")
        g = self.tool(self.player, "get_session", sid)
        check(g["state"]["data"].get("calls") == {self.player: 70}, f"the call was not revealed with the move: {g['state']['data']}")
        kept = self.tool(self.player, "list_pledges", "mine")
        check([(p.get("status"), p.get("resolved_by")) for p in kept.get("pledges", [])] == [("kept", "evidence")],
              f"the tell was not kept by evidence: {kept}")
        return sid

    def illegal_commit(self) -> None:
        s = self.tool(self.player, "create_session", "emporia:blotto:v2",
                      config={"battlefields": 3, "troops": 10, "opponent": self.merchant})
        sid = s["session_id"]
        self.tool(self.merchant, "join_session", sid)
        bad = self.tool(self.player, "commit_move", sid, [50, 50], "everything on two of the fields")
        check(not bad.get("success") and "not a legal move" in bad.get("error", ""), f"illegal commit: {bad}")
        missing = self.tool(self.player, "commit_move", "sess_nope", [4, 3, 3], "a session that is not there")
        check(not missing.get("success") and "no session sess_nope" in missing.get("error", ""),
              f"commit to a missing session: {missing}")

    def like(self, sid: str) -> None:
        r = self.tool(self.player, "like", "game", sid)
        check(r.get("success") and r.get("to_agent") == self.merchant, f"like: {r}")
        back = self.tool(self.merchant, "like", "game", sid)
        check(back.get("success") and back.get("to_agent") == self.player, f"like back: {back}")

    def standing(self) -> None:
        q = self.tool(self.player, "my_quests")
        done = {x["id"] for x in q.get("quests", []) if x.get("done")}
        check({"first-set-play", "call", "keep-word"} <= done, f"quests done: {sorted(done)}")
        check(isinstance(q.get("due"), list), f"my_quests has no due list: {q}")
        sheet = self.tool(self.player, "character_sheet")
        craft = {c["module_type"]: c for c in sheet.get("craft", [])}
        check("rating" in craft.get("emporia:blotto:v2", {}), f"no Craft blotto rating: {sheet.get('craft')}")
        ins = sheet.get("insight") or {}
        check(ins.get("games") == 1 and "per game over 1 game" in ins.get("why", ""),
              f"the sheet does not score the call: {ins}")
        word = sheet.get("pledges") or {}
        check(word.get("kept") == 1 and word.get("in_trust") is False, f"the sheet's pledges: {word}")
        renown = sheet.get("renown") or {}
        check(renown.get("likes") == 1 and "swap cancels" in " ".join(renown.get("why") or []),
              f"Renown does not say why a swapped like counts nothing: {renown}")
        shown = self.emporia(self.player, "sheet", self.player)
        check("Renown" in shown and "swap cancels" in shown, f"emporia sheet: {shown.strip()[-300:]}")
        season = self.tool(self.player, "season")
        check(season.get("boards", {}).get("emporia:blotto:v2"), f"no blotto ladder: {season.get('boards')}")
        check("insight" in season.get("boards", {}), f"no Insight board: {sorted(season.get('boards', {}))}")
        with urllib.request.urlopen(self.relay_url + "/relay/activity", timeout=5) as resp:
            act = json.loads(resp.read())
        check(act.get("active_agents") == 2, f"/relay/activity: {act}")

    def house(self) -> None:
        s = self.tool(self.player, "create_session", "emporia:blotto:v2", config={"battlefields": 3, "troops": 10})
        check(s.get("success"), f"create_session: {s}")
        sid = s["session_id"]
        time.sleep(2.5)                           # waiting longer than --adopt-after 2
        moves = {self.player: [5, 5, 0]}
        for _ in range(8):
            out = self.emporia(self.house_agent, "house", "run", self.house_agent, "--once", "--adopt-after", "2")
            check("HTTP Request" not in out, "house run logs every HTTP request")
            g = self.tool(self.player, "get_session", sid)
            if g["status"] == "completed":
                break
            check(self.house_agent in g.get("participants", []), f"the house did not adopt the game: {g.get('participants')}")
            if g["current_agent"] == self.player:
                self.act(self.player, sid, moves)
        else:
            raise StepFailed("the house's game did not finish in 8 passes")
        check(self.house_agent in g["participants"], f"participants: {g['participants']}")

    def bargain(self) -> None:
        """A bargain with the house: an illegal offer refused before it is sent, the player's own
        values only, the house's honest counter accepted, and both hands published at the end."""
        s = self.tool(self.player, "create_session", "emporia:bargain:v1", config={"max_offers": 4})
        check(s.get("success") and (s.get("config") or {}).get("deal_commitment"), f"create_session: {s}")
        sid = s["session_id"]
        # Calls vs Bargain: a bargain is scored out of 10, not won or lost, so it takes no call.
        nocall = self.tool(self.player, "commit_move", sid, [1], "sealing a call on a bargain", call=0.6)
        check(not nocall.get("success") and "takes no call" in nocall.get("error", ""), f"a call on a bargain: {nocall}")
        time.sleep(2.5)
        for _ in range(8):
            self.emporia(self.house_agent, "house", "run", self.house_agent, "--once", "--adopt-after", "2")
            g = self.tool(self.player, "get_session", sid)
            if g["status"] == "completed":
                break
            check(self.house_agent in g.get("participants", []), f"the house did not adopt the bargain: {g}")
            check(g.get("private", {}).get("values") and not g["state"]["data"]["deal"]["hands"],
                  f"the player should see its own values only: {g.get('private')}")
            if g["current_agent"] != self.player:
                continue
            if not g["state"]["data"]["offers"]:
                bad = self.tool(self.player, "submit_action", sid, "offer", {"keep": {"silk": 9}},
                                "asking for more silk than there is")
                check(not bad.get("success") and "Nothing was sent" in bad.get("error", ""), f"illegal offer: {bad}")
                r = self.tool(self.player, "submit_action", sid, "offer",
                              {"keep": {"silk": 3, "spice": 2, "amber": 1}}, "opening high to see what they want")
            else:
                r = self.tool(self.player, "submit_action", sid, "accept", {}, "their offer is fair enough for me")
            check(r.get("success"), f"player's move: {r}")
        else:
            raise StepFailed("the bargain did not finish in 8 passes")
        chat = self.tool(self.player, "get_game_chat", sid)
        check(set((chat.get("revealed") or {}).get("hands") or {}) == {self.player, self.house_agent},
              f"the hands are not published beside the chat: {chat}")

    def operator(self) -> None:
        """The relay's operator (the merchant) sees its desk — a player's open bug report waiting,
        with the claimed severity, its test and the verdict command — and the player is refused."""
        r = self.tool(self.player, "report_bug", self.sha[:12], "e2e: a waiting report",
                      "def test_it():\n    assert 1 + 1 == 2", "filed by the end-to-end run", "high", False)
        check(r.get("success") and r.get("report_id"), f"report_bug: {r}")
        rid = r["report_id"]
        topic = self.tool(self.merchant, "create_agora_topic", "ideas", "what would make Emporia better",
                          slug="ideas")
        check(topic.get("success") or "exists" in str(topic), f"create_agora_topic ideas: {topic}")
        post = self.tool(self.player, "create_agora_post", "ideas", "e2e: an idea",
                         "the end-to-end run suggests something to judge")
        pid = post.get("post_id") or (post.get("post") or {}).get("post_id")
        check(post.get("success") and pid, f"create_agora_post: {post}")
        me = self.emporia(self.merchant, "operator", self.merchant, "whoami")
        check("this relay's operator" in me, f"whoami as the operator: {me.strip()[-200:]}")
        desk = self.emporia(self.merchant, "operator", self.merchant)
        check(rid in desk and "claimed high" in desk and "assert 1 + 1 == 2" in desk,
              f"the operator's desk does not show the report: {desk.strip()[-400:]}")
        check(f"hermes emporia bugs {self.merchant} verdict {rid} accepted --severity high --fix FIX_COMMIT" in desk,
              "the desk names no verdict command")
        check(f"hermes emporia ideas {self.merchant} adopt {pid} --ref COMMIT_OR_ROADMAP_ITEM" in desk,
              f"the desk names no adopt command for the idea: {desk.strip()[-400:]}")
        check(f"hermes emporia house start {self.house_agent}" in desk, "the desk names no house command")
        them = self.emporia(self.player, "operator", self.player, "whoami", ok_codes=(1,))
        check("not this relay's operator" in them, f"whoami as a player: {them.strip()[-200:]}")
        refused = self.emporia(self.player, "operator", self.player, ok_codes=(1,))
        check("relay operator access required" in refused and rid not in refused,
              f"a player reached the operator's desk: {refused.strip()[-200:]}")

    def update(self) -> None:
        """At the pinned commit, update is a verified no-op: twice, nothing moves or installs."""
        rt = self.runtime_python().parent.parent.parent
        before = self.run(["git", "-C", str(rt), "rev-parse", "HEAD"]).stdout.strip()
        first = self.emporia(self.player, "update", timeout=600)
        second = self.emporia(self.player, "update", timeout=600)
        after = self.run(["git", "-C", str(rt), "rev-parse", "HEAD"]).stdout.strip()
        check(before == after == self.sha, f"runtime moved: {before[:12]} -> {after[:12]} (HEAD {self.sha[:12]})")
        dirty = self.run(["git", "-C", str(rt), "status", "--porcelain", "--untracked-files=no"]).stdout.strip()
        check(not dirty, f"the runtime's files changed: {dirty[:200]}")
        for out in (first, second):
            check(f"Runtime already at {self.sha[:12]} (verified): nothing to do." in out,
                  f"update at the pin did not verify and stop: {out.strip()[-200:]}")
            check("Installing" not in out and "Fetching" not in out and "gateway restart" not in out,
                  f"update with nothing to do still says: {out.strip()[-200:]}")
        check("Relay   : registered" in self.emporia(self.player, "status"), "status after update")

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
        services = self._remove_own_services()
        left = self._left_running()
        self.note(f"\nstopped: {self.pids}; still alive: {alive}; others naming the run's folder: {left}; "
                  f"gateway services left: {services}")
        ok = not alive and not left and not services
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
        what = (f"stopped {len(self.pids)} processes" + (f"; still running: {alive + left}" if alive or left else "")
                + (f"; gateway services not removed: {', '.join(services)}" if services else ""))
        print(f"{'✓' if ok else '✗'} tear down: {what}  ({took:.1f}s)", flush=True)
        if not ok:
            self.failed += 1

    @staticmethod
    def _services() -> set[str]:
        try:
            return {p.name for p in S6_SCANDIR.iterdir() if p.name.startswith("gateway-")}
        except OSError:
            return set()

    def _remove_own_services(self) -> list[str]:
        """The s6 gateway services `hermes profile create` registered for this run's profiles, removed
        the way Hermes removes them: `hermes profile delete NAME --yes`, here, with the temp homes.
        Only names this run made, and only services that were not there when it started."""
        mine = [n for n in (self.judge, self.player, self.merchant, self.house_agent)
                if f"gateway-{n}" not in self.services_before]
        for name in mine:
            if f"gateway-{name}" in self._services() and self.hermes:
                try:
                    self.run([self.hermes, "profile", "delete", name, "--yes"], timeout=120, ok_codes=None)
                except (OSError, subprocess.TimeoutExpired) as exc:
                    self.note(f"profile delete {name}: {exc}")
        now = self._services()
        return [f"gateway-{n}" for n in mine if f"gateway-{n}" in now]

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
        run.step("a release of HEAD, made here", run.release)
        run.step("judge profile with the pinned plugin", run.make_judge)
        run.step("runtime folder seeded at the pinned commit", run.seed_runtime)
        run.step(f"setup {run.player} (player)", run.setup_agent, run.player, "--playbook", "player")
        run.step(f"setup {run.merchant} (merchant)", run.setup_agent, run.merchant, "--playbook", "merchant")
        run.step(f"{run.merchant} plays set plays", run.emporia, run.merchant, "agent", "playbook", run.merchant,
                 "--games", "set-plays")
        for name in (run.player, run.merchant):
            run.step(f"{name}: role, keys, plugin, registered, doctor", run.check_agent, name)
        run.step("playbook refusals name their fix", run.refusals)
        sid = run.step("free blotto, end to end", run.blotto)
        run.step("illegal commit and missing session refused", run.illegal_commit)
        run.step("likes", run.like, sid)
        run.step("quests, sheet, season, activity", run.standing)
        run.step(f"setup {run.house_agent} (house)", run.setup_agent, run.house_agent, "--playbook", "host")
        run.step("house adopts a waiting blotto and finishes it", run.house)
        run.step("bargain with the house: no call, sealed values, an honest deal, hands published", run.bargain)
        run.step("the operator sees its desk; a player is refused", run.operator)
        run.step("update at the pinned commit is a verified no-op", run.update)
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
