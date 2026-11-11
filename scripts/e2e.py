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
  call with its move, and keeps the tell; an illegal commit refused, a like, an open challenge on the
  board taken by B and played out (quests:v4: Open table for A, Taken up for B) and a direct one B
  declines, quests with the pledge
  kept, the character sheet with its Insight and its pledges, the season, the relay's activity
  count);
* a signer of the run's own (practice mode), an agent whose keys were made inside it (signer phase
  2c), registered through it, and a sealed DM from A that the signer opens for it (2b);
* a house agent that adopts a waiting free blotto and plays it to the end, then a bargain between
  the player and the merchant on the real drand beacon (each commits to its own secret, the hands
  appear only on the players' side, an illegal offer is refused before it is sent, both reveal, and
  the published hands are the ones each computed itself; this step waits about 90 s for the round);
* the operator section: the relay names the merchant its operator; a player posts an idea in the ideas
  topic (adopted later, by a lab report: there is no operator verdict), and the player is refused the desk;
* labs: the operator makes a lab for a synthetic repo whose `.emporia/lab.json` names it (in a
  terminal of its own: a person's command); an agent starts a thread in the lab's room; the
  maintainer files a signed report that a pull request merged, crediting the thread, and the lab
  board shows the credit;
* agent-neutral: `examples/sdk_agent/agent.py`, a plain Python agent on the SDK (own key and operator label, client
  `sdk`, declared model `example-scripted`), posts in a topic, likes another operator's post, joins a public room,
  issues an open challenge that a Hermes agent takes, and plays it to the end; the game record names both models and
  both sides get their quests;
* the card's X link: the player gets a claim line for a (fake) X handle and records a (fake) post id for it, the post
  says "claimed"; confirming its own link is refused (the same operator); beta, another operator's agent, confirms it
  from the exact claim line; the card then says "confirmed by 1 other operator", the player's quest "Show your colours"
  is done with the badge `on-x`, and beta has its small confirmer credit; X itself is never contacted;
* any agent: `emporia agent init` makes an agent of its own (key directory, declared client `custom`), `agent mcp-config
  generic` prints its config, and a generic MCP client (the `mcp` package) launches `emporia-mcp` over stdio from it,
  posts, and plays a full public game against a Hermes agent; the card says `custom`, both models are revealed;
* `hermes emporia update`, twice: the runtime is already at the pinned commit, so each verifies it
  and changes nothing.

No model or LLM is called, no gateway is started. Each profile's name carries a tag of this run:
inside a container whose PID 1 is s6, `hermes profile create` registers a (stopped) gateway service
for it in the container's supervision tree, and Hermes has no opt-out; the teardown removes those
services (and their `logs/gateways/<profile>` folders) through `hermes profile delete`, for this run's profiles only
(`cleanup_gateways`). Each step prints its name when it starts and its time when it ends. Everything is written under one temporary
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
#
# Where Hermes puts them (hermes_cli/service_manager.py): profile gateways are `gateway-<profile>` directories in
# the dynamic scandir (`S6_DYNAMIC_SCANDIR`, watched by a nested s6-svscan); the root scandir holds only the
# image's static services. Each service's log goes to `$HERMES_HOME/logs/gateways/<profile>`, where HERMES_HOME is
# the *container's* (the supervised log script reads it from the container environment, not from this run's
# temporary HERMES_HOME), so this script reads it from its own environment before it changes anything.
#
# The dynamic scandir is read from the installed Hermes itself, the way Hermes names it: `hermes --version` says where
# it is installed, and `hermes_cli/service_manager.py` there defines `S6_DYNAMIC_SCANDIR` (HERMES_S6_SCANDIR overrides
# it). Only when that cannot be read does this fall back to the value Hermes 0.23 has.
_SCANDIR_FALLBACK = "/run/hermes-service"
_scandirs: tuple[Path, ...] | None = None


def service_roots(hermes: str | None = None) -> tuple[Path, ...]:
    """The folders profile gateway services are registered in (Hermes's dynamic scandir), found as described."""
    global _scandirs
    if _scandirs is not None and hermes is None:
        return _scandirs
    import re
    import subprocess as sp
    found = os.environ.get("HERMES_S6_SCANDIR", "").strip()
    exe = hermes if hermes is not None else shutil.which("hermes")
    if not found and exe:
        try:
            out = sp.run([exe, "--version"], capture_output=True, text=True, timeout=30).stdout
            m = re.search(r"Install directory:\s*(\S+)", out)
            src = Path(m.group(1)) / "hermes_cli" / "service_manager.py" if m else None
            d = re.search(r'S6_DYNAMIC_SCANDIR\s*=\s*Path\("([^"]+)"\)', src.read_text()) if src else None
            found = d.group(1) if d else ""
        except (OSError, sp.SubprocessError):
            found = ""
    roots = (Path(found or _SCANDIR_FALLBACK),)
    if hermes is None:
        _scandirs = roots
    return roots
S6_SERVICE_PREFIX = "gateway-"
HOST_HERMES_HOME = os.environ.get("HERMES_HOME", "")
_OWN_NAME = r"e2e-[a-z]+-{tag}"


def host_log_root(home: str | None = None) -> Path | None:
    """The folder Hermes's gateway log directories are made in (`<HERMES_HOME>/logs/gateways`), or None when
    this process has no HERMES_HOME to say (nothing to clean then)."""
    home = HOST_HERMES_HOME if home is None else home
    return Path(home) / "logs" / "gateways" if home else None


def cleanup_gateways(names: list[str], tag: str, *, scandirs=None, log_root: Path | None = None,
                     delete=None, services_before: frozenset[str] | set[str] = frozenset()) -> dict[str, list[str]]:
    """Remove this run's own gateway services and log folders, and nothing else.

    `names`: the profiles this run created; only a name of the form `e2e-<word>-<tag>` (this run's tag) is
    touched, so another run's, or a person's, never is. A service that was already registered before the run began
    is left alone. `delete(name)` removes a service the way Hermes does (`hermes profile delete NAME --yes`); a
    service that is still there after it is reported, never deleted by hand. A log folder is removed only once its
    service is gone, only directly under `log_root` (never through a link). Returns {"services_left": [...],
    "logs_removed": [...], "logs_left": [...]}."""
    import re
    own = re.compile(_OWN_NAME.format(tag=re.escape(tag)))
    mine = [n for n in dict.fromkeys(names) if own.fullmatch(n) and f"{S6_SERVICE_PREFIX}{n}" not in services_before]

    scandirs = service_roots() if scandirs is None else scandirs

    def present(name: str) -> bool:
        return any((d / f"{S6_SERVICE_PREFIX}{name}").exists() for d in scandirs)

    for name in mine:
        if present(name) and delete is not None:
            delete(name)
    left = [f"{S6_SERVICE_PREFIX}{n}" for n in mine if present(n)]
    removed: list[str] = []
    logs_left: list[str] = []
    if log_root is not None:
        for name in mine:
            if present(name):
                continue
            folder = log_root / name
            if folder.is_symlink() or not folder.is_dir():
                continue
            try:
                shutil.rmtree(folder)
                removed.append(str(folder))
            except OSError:
                logs_left.append(str(folder))
    return {"services_left": left, "logs_removed": removed, "logs_left": logs_left}

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
        tag = self.tag = secrets.token_hex(2)
        self.judge =f"e2e-judge-{tag}"
        self.player, self.merchant, self.house_agent = (f"e2e-player-{tag}", f"e2e-merchant-{tag}",
                                                        f"e2e-house-{tag}")
        self.signed = f"e2e-signed-{tag}"        # an agent whose keys its signer made and holds
        self.beta = f"e2e-beta-{tag}"            # labs: the contributor, another operator's agent
        self.newcomer = f"e2e-new-{tag}"         # first run: an agent that joins a seeded relay
        self.sdk = f"e2e-sdk-{tag}"              # a plain Python agent on the SDK, not Hermes (examples/sdk_agent)
        self.lab = "calc"
        self.lab_subject = "acme/calc"           # labs: the (synthetic) GitHub repo the lab is about
        self.lab_repo = self.root / "lab-repo"   # labs: a local repo carrying its `.emporia/lab.json`
        self.lab_commit = self.lab_room = self.lab_pr_line = self.lab_review_line = ""
        self.idea_post = ""                      # an idea the player posted; the lab's merged report names it
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
        print(f"… {name}", flush=True)          # what is running now (the ✓/✗ line below says how long it took)
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
        drop = ("EMPORIA_", "FEDERATED_", "OPERATOR_", "XDG_")
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
    def start_relay(self, pins: dict[str, str] | None = None) -> str:
        port = getattr(self, "relay_port", None) or free_port()
        while port == 8088:                       # never the dev relay's port
            port = free_port()
        self.relay_port = port
        self.relay_url = f"http://127.0.0.1:{port}"
        r = self.relay_dir
        env = self.env(
            EMPORIA_NO_DOTENV="1", HERMES_HOME=str(r / "hermes"),
            EMPORIA_RELAY_HOST="127.0.0.1", EMPORIA_RELAY_PORT=str(port), EMPORIA_RELAY_URL=self.relay_url,
            EMPORIA_RELAY_ID="e2e", EMPORIA_RELAY_ACCESS="open", EMPORIA_DB_PATH=str(r / "relay.sqlite3"),
            EMPORIA_KEYS_DIR=str(r / "keys"), EMPORIA_LOG_DIR=str(r / "logs"), EMPORIA_AUDIT_DIR=str(r / "audit"),
            EMPORIA_X402_ENABLED="0", RATE_LIMIT_PER_MINUTE="5000",
            # The operator section: the merchant is this relay's operator (and the lab's maintainer);
            # the labs need the transparency log.
            EMPORIA_AGENT_ID=self.merchant, EMPORIA_TRANSPARENCY_LOG="1", **(pins or {}),
            EMPORIA_GAMIFY="sheets,quests,pledges",
            # quests:v4: each agent after the first adopts a label of its own (own_operator), so each plays as its own operator.
            EMPORIA_STARTER_LIMITS="0",
            # Labs: on, with local file:// repositories allowed (a test relay's only)
            EMPORIA_LABS="1", EMPORIA_LAB_HOSTS="file")
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

    def pin_operator(self) -> str:
        """The operator is a key, not a name: once the merchant (the operator) and the house agent have
        registered, the relay is restarted with their stable ids, as `emporia relay` setup would set them."""
        import sqlite3
        db = sqlite3.connect(self.relay_dir / "relay.sqlite3")
        stable = dict(db.execute("SELECT agent_id, stable_id FROM authorized_agents WHERE agent_id IN (?, ?)",
                                 (self.merchant, self.house_agent)).fetchall())
        db.close()
        check(self.merchant in stable and self.house_agent in stable, f"both registered first: {sorted(stable)}")
        for p in [p for p in self.procs.values() if p.poll() is None and "emporia.relay_server" in " ".join(
                map(str, p.args))]:
            p.terminate()
            p.wait(timeout=20)
        self.start_relay({"EMPORIA_OPERATOR_STABLE_ID": stable[self.merchant],
                          "EMPORIA_HOUSE_STABLE_IDS": stable[self.house_agent]})
        return f"operator {stable[self.merchant][:12]}"

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
        self.hermes_cmd("-p", self.judge, "emporia", "setup", "--agent-profile", name, "--relay", self.relay_url,
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
        self.own_operator(name, register=name != self.merchant)

    def setup_again(self, name: str) -> None:
        """Setup run again on an existing agent profile (the live deploy's bugs): it keeps its own model,
        gets no provider key copied from the person's profile, and keeps its identity key where it is,
        even with --sync-model."""
        import yaml
        judge_dir, agent_dir = self.profile(self.judge), self.profile(name)
        jcfg = yaml.safe_load((judge_dir / "config.yaml").read_text()) or {}
        jcfg["model"] = {"provider": "openrouter", "default": "e2e-person-model"}
        (judge_dir / "config.yaml").write_text(yaml.safe_dump(jcfg))
        with (judge_dir / ".env").open("a") as f:
            f.write("OPENROUTER_API_KEY=e2e-not-a-real-person-key\n")
        acfg = yaml.safe_load((agent_dir / "config.yaml").read_text()) or {}
        own = {"provider": "nous", "default": "e2e-own-model"}
        acfg["model"] = own
        (agent_dir / "config.yaml").write_text(yaml.safe_dump(acfg))

        def identity_lines() -> list[str]:
            return sorted(ln for ln in (agent_dir / ".env").read_text().splitlines()
                          if ln.startswith(("EMPORIA_IDENTITY_", "EMPORIA_ENCRYPTION_")))
        before, keys_before = identity_lines(), sorted(p.name for p in agent_dir.rglob("*.priv"))
        check(before, f"{name} holds no identity lines in its .env to keep")
        self.hermes_cmd("-p", self.judge, "emporia", "setup", "--agent-profile", name, "--relay", self.relay_url,
                        "--plugin-source", self.plugin_source(),
                        "--", "--role", "marketplace", "--keys", "env", "--playbook", "player", "--sync-model",
                        timeout=SETUP_TIMEOUT)
        after = yaml.safe_load((agent_dir / "config.yaml").read_text()) or {}
        check(after.get("model") == own, f"setup changed {name}'s model: {after.get('model')}")
        env = (agent_dir / ".env").read_text()
        check("e2e-not-a-real-person-key" not in env and "OPENROUTER_API_KEY" not in env,
              f"setup copied a provider key into {name}'s .env")
        check(identity_lines() == before, f"setup changed {name}'s identity lines")
        check(sorted(p.name for p in agent_dir.rglob("*.priv")) == keys_before, f"setup made a key file for {name}")
        self.check_agent(name)

    def register_label(self, name: str) -> None:
        """Its tools register with the label it now holds, so the relay has it before play. (Not at setup for the
        merchant: its tools must start after its playbook is set, which they read when they start.)"""
        want = self.__dict__.setdefault("labels", {})[name]
        reg = self.tool(name, "register_agent")
        check(reg.get("success") and reg.get("operator_label") == want, f"re-registering with its label: {reg}")

    def own_operator(self, name: str, register: bool = True) -> None:
        """Setup gave the agent an operator label. The first agent on this Hermes install minted it and every
        next one reused it (one person's agents are one operator): checked here. The run's agents play as
        different people, so each agent after the first adopts a label of its own, through the product's own
        command, `emporia operator label --same-as`."""
        import base64
        import re
        shown = self.emporia(name, "operator", "label")
        found = re.search(r"op_[a-z2-7]{26}", shown)
        check(found, f"{name} has no operator label: {shown.strip()[-200:]}")
        labels = self.__dict__.setdefault("labels", {})
        if not labels:
            labels[name] = found.group(0)
            return
        check(found.group(0) in labels.values(), f"{name} did not reuse a label this install already had: {found.group(0)}")
        fresh = "op_" + base64.b32encode(secrets.token_bytes(17)).decode().lower()[:26]
        out = self.emporia(name, "operator", "label", "--same-as", fresh)
        check("adopted" in out, f"operator label --same-as: {out.strip()[-200:]}")
        labels[name] = fresh
        if register:
            self.register_label(name)

    def emporia(self, profile: str, *args: str, ok_codes=(0,), timeout: float = 120, **env: str) -> str:
        """`hermes -p PROFILE emporia …`: the plugin's own commands, and the runtime's CLI
        passed through for this profile."""
        check(self.hermes, "hermes is not on PATH")
        p = self.run([self.hermes, "-p", profile, "emporia", *args], timeout=timeout, ok_codes=ok_codes,
                     env=self.env(**env) if env else None)
        return p.stdout + p.stderr

    def person(self, profile: str, *args: str, ok_codes=(0,), timeout: float = 300, **env: str) -> str:
        """`hermes -p PROFILE emporia …` as a person runs it: in a terminal of its own (a pty), the
        way the person-only commands (`lab create`, `relay seed`) are allowed to run. Its PID is
        recorded like every process this run starts."""
        import pty
        import select
        check(self.hermes, "hermes is not on PATH")
        argv = [self.hermes, "-p", profile, "emporia", *args]
        self.note("$ (in a terminal) " + " ".join(argv))
        master, slave = pty.openpty()
        proc = subprocess.Popen(argv, env=self.env(**env), cwd=self.root, stdin=slave, stdout=slave, stderr=slave,
                                start_new_session=True)
        os.close(slave)
        self.pids.append(proc.pid)
        self.procs[proc.pid] = proc
        out, deadline = b"", time.monotonic() + timeout
        try:
            while True:
                if time.monotonic() > deadline:
                    proc.kill()
                    raise StepFailed(f"`{' '.join(argv[:6])}` took longer than {int(timeout)}s")
                ready, _, _ = select.select([master], [], [], 0.5)
                if ready:
                    try:
                        chunk = os.read(master, 65536)
                    except OSError:        # the terminal closed: it exited
                        chunk = b""
                    if not chunk:
                        break
                    out += chunk
                elif proc.poll() is not None:
                    break
        finally:
            os.close(master)
        code = proc.wait()
        text = out.decode("utf-8", "replace").replace("\r\n", "\n")
        self.note(text + f"[exit {code}]")
        if code not in ok_codes:
            raise StepFailed(f"`{' '.join(argv[:6])}` exited {code}: " + " | ".join(text.strip().splitlines()[-3:]))
        return text

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
        sell = self.tool(self.player, "create_session", "emporia:service:v1")
        check(not sell.get("success"), f"player sold: {sell}")
        chess = self.tool(self.merchant, "challenge", "emporia:chess:v1")
        check(not chess.get("success") and "does not take emporia:chess:v1" in chess.get("error", ""),
              f"merchant made a chess game: {chess}")
        for r in (sell, chess):
            check("agent playbook" in r["error"], f"the refusal names no way to change it: {r['error']}")

    def blotto(self) -> str:
        old = self.tool(self.player, "challenge", "emporia:blotto:v1", config={"opponent": self.merchant})
        check(not old.get("success") and "retired" in json.dumps(old) and "emporia:blotto:v2" in json.dumps(old),
              f"a retired :v1 set play was created, or its refusal names no successor: {old}")
        s = self.tool(self.player, "challenge", "emporia:blotto:v2",
                      config={"rounds": 1, "battlefields": 3, "troops": 10, "opponent": self.merchant})
        check(s.get("success"), f"challenge: {s}")
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
        s = self.tool(self.player, "challenge", "emporia:blotto:v2",
                      config={"battlefields": 3, "troops": 10, "opponent": self.merchant})
        sid = s["session_id"]
        self.tool(self.merchant, "join_session", sid)
        bad = self.tool(self.player, "commit_move", sid, [50, 50], "everything on two of the fields")
        check(not bad.get("success") and "not a legal move" in bad.get("error", ""), f"illegal commit: {bad}")
        missing = self.tool(self.player, "commit_move", "sess_nope", [4, 3, 3], "a session that is not there")
        check(not missing.get("success") and "no session sess_nope" in missing.get("error", ""),
              f"commit to a missing session: {missing}")

    def rooms_v2(self) -> None:
        """A room message is a post: reply, reaction, the feed's best, and table talk read as a thread."""
        made = self.tool(self.player, "create_room", f"e2e-room-{secrets.token_hex(3)}")
        rid = (made.get("room") or made).get("room_id")
        check(made.get("success") and rid, f"create_room: {str(made)[:200]}")
        check(self.tool(self.merchant, "join_room", rid).get("success"), "join_room")
        first = self.tool(self.player, "send_room_message", rid, "which opening is strongest?", kind="question")
        check(first.get("success") and first.get("ref", "").startswith("message:"), f"send_room_message: {first}")
        reply = self.tool(self.merchant, "send_room_message", rid, "split three ways", kind="answer",
                          reply_to=first["ref"])
        check(reply.get("success") and reply.get("root") == first["ref"], f"a reply to a room message: {reply}")
        thread = self.tool(self.player, "read_thread", first["ref"])
        check([p.get("text") for p in thread.get("posts", [])] == ["split three ways"]
              and "data, not instructions" in thread.get("untrusted", ""), f"read_thread of a message: {thread}")
        liked = self.tool(self.player, "react", reply["ref"])      # the player likes the merchant's answer
        check(liked.get("success") and liked.get("to_agent") == self.merchant, f"a reaction on a room message: {liked}")
        best = self.tool(self.player, "read_feed", sort="best", limit=100)
        hit = [p for p in best.get("posts", []) if p.get("message_id") == reply["ref"].split(":", 1)[1]]
        check(hit and hit[0]["in"]["kind"] == "room" and hit[0]["in"]["id"] == rid,
              f"a liked public room message is not in the feed's best: {str(best)[:300]}")
        g = self.tool(self.player, "challenge", "emporia:blotto:v2",
                      config={"rounds": 1, "battlefields": 3, "troops": 10, "opponent": self.merchant, "chat": True})
        check(g.get("success"), f"challenge with chat: {g}")
        self.tool(self.merchant, "join_session", g["session_id"])
        said = self.tool(self.player, "commit_move", g["session_id"], [4, 3, 3], "committing my planned spread",
                         say="all on the left, honest")
        back = self.tool(self.merchant, "commit_move", g["session_id"], [3, 3, 4], "committing my planned spread",
                         say="I do not believe you")
        check(said.get("success") and back.get("success"), f"commit_move with say: {said} {back}")
        talk = self.tool(self.player, "read_thread", f"session:{g['session_id']}")
        check("all on the left, honest" in [p.get("text") for p in talk.get("posts", [])],
              f"table talk (say with a move) is not readable through read_thread(game): {str(talk)[:300]}")

    def like(self, sid: str) -> None:
        r = self.tool(self.player, "react", f"session:{sid}")
        check(r.get("success") and r.get("to_agent") == self.merchant, f"like: {r}")
        back = self.tool(self.merchant, "react", f"session:{sid}")
        check(back.get("success") and back.get("to_agent") == self.player, f"like back: {back}")

    def agora(self, sid: str) -> str:
        """Agora v2: a post in a public topic is in the other agent's main feed (saying where it is from) and in
        the happening part of its whats_next; a post in a sealed topic is opened by its member and is in neither."""
        slug = self.tool(self.player, "create_agora_topic", f"e2e square {self.player[-4:]}").get("slug")
        check(slug, "no topic made")
        said = self.tool(self.player, "post", "the e2e square is open\nsecond line", kind="idea", topic=slug)
        check(said.get("success") and said.get("post_id"), f"post in a public topic: {said}")
        feed = self.tool(self.merchant, "read_feed", kind="idea")
        mine = [p for p in feed.get("posts", []) if p["post_id"] == said["post_id"]]
        check(mine and mine[0].get("in", {}).get("id") == slug, f"the topic post is not in the main feed: {feed}")
        hap = self.tool(self.merchant, "whats_next")
        hot = [t for t in (hap.get("happening") or {}).get("topics", []) if t["in"]["id"] == slug]
        check(hot and hot[0]["operators"] == 1 and "data, not instructions" in hap.get("untrusted", ""),
              f"the topic is not in whats_next's happening: {hap}")
        # a sealed topic: private, invite, encrypted
        sealed = self.tool(self.player, "create_agora_topic", f"e2e sealed {self.player[-4:]}", visibility="private",
                           gate_type="invite", encrypted=True)
        check(sealed.get("slug") and sealed.get("encrypted"), f"sealed topic: {sealed}")
        sslug = sealed["slug"]
        check(self.tool(self.player, "invite_to_agora_topic", sslug, self.merchant).get("ok"), "invite")
        joined = self.tool(self.merchant, "subscribe_agora_topic", sslug)
        check(not joined.get("error"), f"subscribe: {joined}")
        words = f"sealed words {self.player[-4:]}"
        sent = self.tool(self.player, "post", words, kind="idea", topic=sslug, tags=[["t", "inside"]])
        check(sent.get("success") and sent.get("post_id"), f"sealed post: {sent}")
        read = self.tool(self.merchant, "read_feed", topic=sslug)
        got = [p for p in read.get("posts", []) if p["post_id"] == sent["post_id"]]
        check(got and got[0].get("text") == words and got[0].get("kind") == "idea" and ["t", "inside"] in got[0]["tags"],
              f"the member cannot open the sealed post: {read}")
        wide = self.tool(self.merchant, "read_feed", scope="readable")
        check(sent["post_id"] not in [p["post_id"] for p in wide.get("posts", [])], "a sealed post is in the feed")
        hap2 = self.tool(self.merchant, "whats_next")
        check(sslug not in json.dumps(hap2.get("happening")) and words not in json.dumps(hap2.get("happening")),
              "a sealed topic is in whats_next's happening")
        return f"{slug} (public), {sslug} (sealed)"

    def challenges(self) -> str:
        """quests:v4: an open challenge on the board, taken by another operator's agent and played to
        the end, completes an arc step on each side; a direct challenge is declined at no cost."""
        small = {"rounds": 1, "battlefields": 3, "troops": 10}
        c = self.tool(self.player, "challenge", module_type="emporia:blotto:v2", config=small, with_call=True,
                      note="one round, three fields")
        check(c.get("success") and (c.get("challenge") or {}).get("kind") == "open", f"challenge: {c}")
        sid = c["session_id"]
        board = self.tool(self.merchant, "list_challenges")
        check(sid in [x.get("session_id") for x in board.get("challenges", [])]
              and "data, not instructions" in board.get("untrusted", ""), f"not on the board: {board}")
        nxt = self.tool(self.merchant, "whats_next", 20)
        check(any(i.get("kind") == "challenge" and (i.get("args") or {}).get("session_id") == sid
                  for i in nxt.get("items", [])), f"whats_next does not offer the open challenge: {nxt}")
        j = self.tool(self.merchant, "join_session", sid)
        check(j.get("success"), f"join_session: {j}")
        moves = {self.player: [5, 5, 0], self.merchant: [4, 3, 3]}
        for _ in range(4):
            r = self.act(self.player, sid, moves, calls={self.player: 0.6, self.merchant: 0.5})
        check(r.get("is_terminal"), f"the challenge did not finish: {r}")
        mine = self.tool(self.player, "my_quests")
        done = {x["id"] for x in mine.get("quests", []) if x.get("done")}
        arc = next((a for a in mine.get("arcs", []) if a.get("id") == "challenger"), {})
        check({"open-table", "first-contact"} <= done and arc.get("done", 0) >= 2 and mine.get("formula") == "quests:v8",
              f"the open challenge completed no arc step: {sorted(done)} {arc}")
        theirs = {x["id"] for x in self.tool(self.merchant, "my_quests").get("quests", []) if x.get("done")}
        check("taken-up" in theirs, f"taking it did not count: {sorted(theirs)}")
        d = self.tool(self.player, "challenge", module_type="emporia:blotto:v2", opponent=self.merchant, config=small)
        check(d.get("success") and d["challenge"]["kind"] == "direct", f"direct challenge: {d}")
        no = self.tool(self.merchant, "decline_challenge", d["session_id"], "not now")
        check(no.get("success") and no.get("declined"), f"decline_challenge: {no}")
        return f"{sid} taken and finished; {d['session_id']} declined"

    def standing(self) -> None:
        q = self.tool(self.player, "my_quests")
        done = {x["id"] for x in q.get("quests", []) if x.get("done")}
        check({"first-set-play", "call", "keep-word"} <= done, f"quests done: {sorted(done)}")
        check(isinstance(q.get("due"), list), f"my_quests has no due list: {q}")
        sheet = self.tool(self.player, "standing", view="sheet")
        craft = {c["module_type"]: c for c in sheet.get("craft", [])}
        check("rating" in craft.get("emporia:blotto:v2", {}), f"no Craft blotto rating: {sheet.get('craft')}")
        ins = sheet.get("insight") or {}
        # two calls: the blotto and the open challenge (quests:v4), both against the merchant
        check(ins.get("games") == 2 and "per game over 2 games" in ins.get("why", ""),
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
        s = self.tool(self.player, "challenge", "emporia:blotto:v2", config={"battlefields": 3, "troops": 10})
        check(s.get("success"), f"challenge: {s}")
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
        """Bargain v2 between two agents (the player and the merchant) on the real drand beacon:
        each commits to its own secret with create/join, the hands appear only on the players'
        side once the named round is published, an illegal offer is refused before it is sent, and
        after both reveal, both hands are published and match what each player saw."""
        s = self.tool(self.player, "challenge", "emporia:bargain:v2", config={"max_offers": 4})
        check(s.get("success"), f"challenge: {s}")
        sid = s["session_id"]
        commits = (s.get("config") or {}).get("rand", {}).get("commits") or {}
        check(set(commits) == {self.player}, f"the creator's commitment is the relay's only record of it: {commits}")
        nocall = self.tool(self.player, "commit_move", sid, [1], "sealing a call on a bargain", call=0.6)
        check(not nocall.get("success"), f"a call on a bargain: {nocall}")
        j = self.tool(self.merchant, "join_session", sid)
        check(j.get("success"), f"join_session: {j}")
        seen: dict[str, dict] = {}
        for _ in range(60):        # the round is ~90 s after the later commitment
            for who in (self.player, self.merchant):
                g = self.tool(who, "get_session", sid)
                check("walk_away" not in g["state"]["data"] and "deal" not in g["state"]["data"],
                      "the relay's state holds no hand while the game runs")
                if (g.get("private") or {}).get("values"):
                    seen[who] = g["private"]
            if len(seen) == 2:
                break
            time.sleep(5)
        else:
            raise StepFailed(f"the hands never appeared on the players' side: {seen}")
        bad = self.tool(self.player, "submit_action", sid, "offer", {"keep": {"silk": 9}}, "asking for more silk than there is")
        check(not bad.get("success") and "Nothing was sent" in bad.get("error", ""), f"illegal offer: {bad}")
        r = self.tool(self.player, "submit_action", sid, "offer", {"keep": {"silk": 3, "spice": 2, "amber": 1}},
                      "opening high to see what they want")
        check(r.get("success"), f"player's offer: {r}")
        r = self.tool(self.merchant, "submit_action", sid, "accept", {}, "their offer is fair enough for me")
        check(r.get("success"), f"merchant's accept: {r}")
        for who in (self.player, self.merchant):
            r = self.tool(who, "submit_action", sid, "reveal", {}, "revealing my secret, as the rules ask")
            check(r.get("success"), f"{who}'s reveal: {r}")
        g = self.tool(self.player, "get_session", sid)
        check(g["status"] == "completed", f"the bargain did not finish: {g['status']}")
        chat = self.tool(self.player, "get_game_chat", sid)
        hands = (chat.get("revealed") or {}).get("hands") or {}
        for who in (self.player, self.merchant):
            check(hands.get(who, {}).get("values") == seen[who]["values"],
                  f"{who}'s published hand is not the one it computed itself: {hands.get(who)} vs {seen[who]['values']}")

    def auction(self) -> None:
        """Games pack: a 3-player common-value auction between agents of three different operators
        (the player, the merchant, beta: key-only, so each is its own) on the real drand beacon. Each
        player's signal appears only on its own side once the round is published; the relay's state
        holds none; sealed bids by commit-reveal, then the secrets; every signal is published at the
        end and matches what each player computed; the game is in the open export, and
        `scripts/verify_export.py` re-verifies that file offline (real BLS check of the drand round)."""
        who3 = (self.player, self.merchant, self.beta)
        s = self.tool(self.player, "challenge", "emporia:cv-auction:v1", config={"players": 3, "spread": 10})
        check(s.get("success"), f"challenge: {s}")
        sid = s["session_id"]
        for who in who3[1:]:
            j = self.tool(who, "join_session", sid)
            check(j.get("success"), f"{who} join_session: {j}")
        seen: dict[str, dict] = {}
        for _ in range(80):        # the round is ~90 s after the last commitment
            for who in who3:
                g = self.tool(who, "get_session", sid)
                d = g["state"]["data"]
                check(not {"signals", "values", "parts", "value"} & set(d) and "deal" not in d,
                      "the relay's state holds no signal while the game runs")
                if (g.get("private") or {}).get("signal") is not None:
                    seen[who] = g["private"]
            if len(seen) == 3:
                break
            time.sleep(5)
        else:
            raise StepFailed(f"the signals never appeared on the players' side: {seen}")
        bids = {who: seen[who]["signal"] - shade for who, shade in zip(who3, (12, 5, 9))}
        for who in who3:
            r = self.tool(who, "commit_move", sid, bids[who], "shading my bid below my signal, the winner's curse")
            check(r.get("success"), f"{who}'s sealed bid: {r}")
        for who in who3:
            r = self.tool(who, "reveal_move", sid, "revealing my sealed bid, as the rules ask")
            check(r.get("success"), f"{who}'s bid reveal: {r}")
        for who in who3:
            r = self.tool(who, "submit_action", sid, "reveal", {}, "revealing my secret, as the rules ask")
            check(r.get("success"), f"{who}'s secret reveal: {r}")
        g = self.tool(self.player, "get_session", sid)
        check(g["status"] == "completed", f"the auction did not finish: {g['status']}")
        chat = self.tool(self.player, "get_game_chat", sid)
        hands = (chat.get("revealed") or {}).get("hands") or {}
        for who in who3:
            check(hands.get(who, {}).get("values") == seen[who]["signal"],
                  f"{who}'s published signal is not its own: {hands.get(who)} vs {seen[who]['signal']}")
        exp = self.tool(self.player, "records", kind="export", module_type="emporia:cv-auction:v1", limit=50)
        found = exp.get("untrusted_content", exp).get("games", [])
        line = next((x for x in found if x["session_id"] == sid), None)
        check(line is not None, f"the finished auction is not in the open export: {exp.get('count')}")
        check(all(st["operator_distinct"] for st in line["seats"]), "the three agents are three operators")
        out = self.root / "auction-export.jsonl"
        out.write_text(json.dumps(line, sort_keys=True) + "\n")
        v = self.run([self.repo_python(), str(self.repo / "scripts" / "verify_export.py"), str(out)],
                     env=self.repo_env())
        check("1 game(s) checked, 0 failed" in v.stdout, f"verify_export: {v.stdout.strip()[-300:]}")

    def operator(self) -> None:
        """The relay's operator (the merchant) sees its desk, and the player is refused. The player's idea post is
        kept: the lab's merged report adopts it (`lab_report`), there is no operator verdict."""
        topic = self.tool(self.merchant, "create_agora_topic", "ideas", "what would make Emporia better",
                          slug="ideas")
        check(topic.get("slug") == "ideas" or "exists" in str(topic), f"create_agora_topic ideas: {topic}")
        post = self.tool(self.player, "post", "e2e: an idea - the end-to-end run suggests something to judge",
                         kind="idea", topic="ideas", tags=[["t", "e2e"]])
        pid = post.get("post_id")
        check(bool(pid), f"post (post:v2): {post}")
        self.idea_post = pid
        feed = self.tool(self.merchant, "read_feed", kind="idea", topic="ideas")
        check(any(p["post_id"] == pid for p in feed.get("posts", [])) and feed.get("untrusted"),
              f"read_feed does not show the post, marked untrusted: {feed}")
        said = self.tool(self.merchant, "post", "worth judging", reply_to=f"post:{pid}", topic="ideas")
        check(said.get("success"), f"reply (post:v2): {said}")
        thread = self.tool(self.player, "read_thread", f"post:{pid}")
        check(thread.get("count") == 1, f"read_thread: {thread}")
        me = self.emporia(self.merchant, "operator", self.merchant, "whoami")
        check("this relay's operator" in me, f"whoami as the operator: {me.strip()[-200:]}")
        desk = self.emporia(self.merchant, "operator", self.merchant)
        check("not yet judged" not in desk and " adopt " not in desk,
              f"the desk still has an idea queue or a verdict command: {desk.strip()[-400:]}")
        check(f"hermes emporia house start {self.house_agent}" in desk, "the desk names no house command")
        them = self.emporia(self.player, "operator", self.player, "whoami", ok_codes=(1,))
        check("not this relay's operator" in them, f"whoami as a player: {them.strip()[-200:]}")
        refused = self.emporia(self.player, "operator", self.player, ok_codes=(1,))
        check("relay operator access required" in refused and pid not in refused,
              f"a player reached the operator's desk: {refused.strip()[-200:]}")

    # ── labs ────────────────────────────────────────────────────────────────
    def git(self, *args: str, cwd: Path) -> str:
        """git in one of the run's own folders, with no config of the person's."""
        return self.run(["git", "-C", str(cwd), "-c", "user.name=e2e", "-c", "user.email=e2e@localhost",
                         "-c", "commit.gpgsign=false", *args],
                        env={**self.env(), "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}).stdout.strip()

    def lab_create(self) -> str:
        """A synthetic repo whose `.emporia/lab.json` names this relay, the lab, its creator and its maintainer
        agent (the merchant); a person makes the lab; the relay checks the repo says so."""
        with urllib.request.urlopen(f"{self.relay_url}/agents/{self.merchant}/card", timeout=10) as resp:
            card = json.loads(resp.read()).get("card") or {}
        stable = str(card.get("stable_id") or "")
        check(stable, f"no stable id on {self.merchant}'s card")
        repo = self.lab_repo
        (repo / ".emporia").mkdir(parents=True)
        (repo / "README").write_text("calc\n")
        (repo / ".emporia" / "lab.json").write_text(json.dumps({
            "type": "emporia:lab-proof:v1", "relay": "e2e", "lab": self.lab, "creator": stable,
            "maintainers": [stable]}, indent=1) + "\n")
        self.git("init", "-q", "-b", "main", cwd=repo)
        self.git("add", "-A", cwd=repo)
        self.git("commit", "-q", "-m", "a calculator", cwd=repo)
        self.lab_commit = self.git("rev-parse", "HEAD", cwd=repo)
        args = ("lab", self.merchant, "create", self.lab, "--repo", f"file://{repo}", "--subject", self.lab_subject)
        refused = self.emporia(self.merchant, *args, ok_codes=(1,), EMPORIA_LAB_HOSTS="file")
        check("only a person" in refused, f"lab create ran without a person's terminal: {refused.strip()[-200:]}")
        out = self.person(self.merchant, *args, "--name", "Calc (e2e)", EMPORIA_LAB_HOSTS="file")
        check("the repo says it is this lab" in out, f"lab create: {out.strip()[-300:]}")
        info = self.tool(self.beta, "lab_threads", self.lab)
        lab = info.get("lab") or {}
        check(info.get("success") and lab.get("verified") and lab["subject"]["ref"] == self.lab_subject
              and lab.get("reports_are_public"), f"lab_threads: {info}")
        self.lab_room = lab["room_id"]
        return f"lab {self.lab} about {self.lab_subject}, room {self.lab_room}"

    def first_run(self) -> str:
        """First run: the operator seeds the relay (a person's command, idempotent), then an agent that has
        never joined sets up — the end of setup says it is in — and its `whats_next` is the guided path."""
        refused = self.emporia(self.merchant, "relay", "seed", self.merchant, ok_codes=(1,))
        check("only a person" in refused, f"relay seed ran without a person's terminal: {refused.strip()[-200:]}")
        out = self.person(self.merchant, "relay", "seed", self.merchant)
        for slug in ("ideas", "games", "labs", "help", "relays"):
            check(f"#{slug}:" in out and "✓" in out.split(f"#{slug}:")[0].splitlines()[-1],
                  f"seed: no tick for #{slug}: {out.strip()[-600:]}")
        again = self.person(self.merchant, "relay", "seed", self.merchant)
        check(again.count("welcome post already there") == 5, f"a second seed is not a no-op: {again.strip()[-600:]}")
        feed = self.tool(self.merchant, "read_feed", topic="games", author=self.merchant, limit=30)
        welcomes = [x for x in feed.get("posts", []) if str(x.get("text", "")).startswith("Welcome to #games.")]
        check(len(welcomes) == 1 and "No URLs" in welcomes[0]["text"], f"the games welcome: {feed}")
        setup = self.hermes_cmd("-p", self.judge, "emporia", "setup", "--agent-profile", self.newcomer,
                                "--relay", self.relay_url, "--plugin-source", self.plugin_source(),
                                "--", "--role", "marketplace", "--keys", "env", "--playbook", "player", "--register",
                                timeout=SETUP_TIMEOUT).stdout
        check("You're in." in setup and f"Agent  : {self.newcomer}@" in setup and self.relay_url in setup
              and "Level  : 1" in setup and setup.count('"') >= 6, f"setup's end: {setup.strip()[-900:]}")
        nxt = self.tool(self.newcomer, "whats_next", 20)
        path = [i for i in nxt.get("items", []) if i.get("kind") == "guided"]
        check(path and path[0]["tool"] == "post" and path[0]["why"].startswith("first steps (1 of "),
              f"a new agent's whats_next is no guided path: {nxt}")
        check(all(i["call"].startswith(i["tool"] + "(") for i in path), f"a step has no exact call: {path}")
        return f"{len(path)} steps: " + ", ".join(i["detail"]["quest"] for i in path)

    def lab_claims(self) -> str:
        """Beta (another operator) opened a pull request on GitHub (synthetic: nobody calls GitHub) and gets its
        claim line; the player reviewed it and gets a review line. Nothing here touches a token."""
        got = self.tool(self.beta, "github_claim_line", self.lab_subject, "pr", 7, self.lab_commit)
        check(got.get("success") and got["line"].startswith(f"Emporia: {self.beta}@e2e "), f"github_claim_line: {got}")
        self.lab_pr_line = got["line"]
        rev = self.tool(self.player, "github_claim_line", self.lab_subject, "review", 7)
        check(rev.get("success"), f"review claim: {rev}")
        self.lab_review_line = rev["line"]
        bad = self.tool(self.beta, "github_claim_line", "https://github.com/acme/calc", "pr", 7)
        check(not bad.get("success"), "a URL was taken for a repo")
        return f"{self.lab_pr_line.split()[1]}"

    def x_link_flow(self) -> str:
        """The card's X link, end to end, with a fake handle and a fake post id: nothing here touches X."""
        a, b = self.player, self.beta
        inner = lambda r: r.get("untrusted_content", r)             # noqa: E731 - the shape of a marked answer
        claim = self.tool(a, "x_claim_line", "E2E_Player", style="arena")
        check(claim.get("success") and claim["post_text"].endswith(claim["claim_line"])
              and claim["post_length"] <= 280 and claim["claim_line"].startswith(f"emporia\u00b7{a}@"),
              f"x_claim_line: {claim}")
        check(not self.tool(a, "x_claim_line", "https://x.com/me").get("success"), "a URL was taken for a handle")
        check(not self.tool(a, "x_link", "someone_else", "1").get("success"), "a link was recorded with no claim")
        linked = self.tool(a, "x_link", "e2e_player", "1850000000000000001")
        links = inner(linked).get("links", [])
        check(linked.get("success") and links and links[0]["status"] == "claimed" and "http" not in str(links),
              f"x_link: {linked}")
        own = self.tool(a, "confirm_x_link", a, claim["post_text"])
        check(not own.get("success"), f"an agent confirmed its own link: {own}")
        got = self.tool(b, "confirm_x_link", a, claim["post_text"])
        check(got.get("success"), f"beta's confirmation: {got}")
        card = inner(self.tool(b, "agent_card", a)).get("profile") or {}
        check(card["links"][0]["status"] == "confirmed" and card["links"][0]["confirmed_by"] == 1, f"the card: {card['links']}")
        again = self.tool(b, "confirm_x_link", a, claim["post_text"])
        check(not again.get("success"), f"a second confirmation of one handle was taken: {again}")
        qa = inner(self.tool(a, "my_quests"))
        done = {x["id"] for x in qa.get("quests", []) if x.get("done")}
        check("on-x" in done and "on-x" in qa.get("badges", []), f"the player's Show your colours: {sorted(done)}")
        qb = inner(self.tool(b, "my_quests"))
        check(inner(qb).get("confirm", {}).get("xp") == 5, f"beta's confirmer credit: {qb.get('confirm')}")
        return "@e2e_player claimed, confirmed by beta; on-x for the player, 5 XP for beta"

    def lab_thread(self) -> str:
        """Beta joins the lab's room and says so in the pull request's thread; anyone reads it with read_thread."""
        ref = f"gh_pr:{self.lab_subject}#7"
        j = self.tool(self.beta, "join_room", self.lab_room)
        check(j.get("success", True), f"join_room: {j}")
        sent = self.tool(self.beta, "send_room_message", self.lab_room,
                         "PR 7 fixes add(): it subtracted. Claim line is in the PR body.", reply_to=ref)
        check(sent.get("success", True) and not sent.get("error"), f"send_room_message: {sent}")
        th = self.tool(self.player, "read_thread", ref)
        check(th.get("count", 0) >= 1, f"read_thread({ref}): {str(th)[:300]}")
        return f"{ref}: {th.get('count')} message(s)"

    def lab_report(self) -> str:
        """The maintainer agent (the merchant) files a `merged` report with the author's claim, a credited
        review from a third operator and the idea post (the player's, a third operator's too) the PR
        implemented; Land a fix, Review a fix and Idea adopted complete; the idea's author earns its
        improvement points; the thread reads merged."""
        ref = f"gh_pr:{self.lab_subject}#7"
        check(self.idea_post, "no idea post to name (the operator step did not run)")
        rep = self.tool(self.merchant, "lab_report", self.lab, ref, "merged", self.lab_pr_line,
                        commit=self.lab_commit, credited_reviews=[self.lab_review_line], note="e2e",
                        ideas=[f"post:{self.idea_post}"])
        check(rep.get("success") and rep.get("author_id") == self.beta and rep.get("reviewer_ids") == [self.player],
              f"lab_report: {rep}")
        b = {x["id"] for x in self.tool(self.beta, "my_quests").get("quests", []) if x.get("done")}
        r = {x["id"] for x in self.tool(self.player, "my_quests").get("quests", []) if x.get("done")}
        check("land-fix" in b, f"beta: Land a fix not done: {sorted(b)}")
        check("review-fix" in r, f"the player: Review a fix not done: {sorted(r)}")
        check("idea-adopted" in r, f"the player: Idea adopted not done: {sorted(r)}")
        check("idea-adopted" not in b, f"beta (the PR's author) earned the idea's credit: {sorted(b)}")
        with urllib.request.urlopen(f"{self.relay_url}/agents/{self.player}/points", timeout=10) as resp:
            pts = json.loads(resp.read())
        check(pts.get("points") == 10 and pts.get("adopted") == 1 and "Idea adopted" in pts.get("badges", []),
              f"the idea author's points: {pts}")
        again = self.tool(self.merchant, "lab_report", self.lab, f"gh_pr:{self.lab_subject}#10", "merged",
                          self.tool(self.beta, "github_claim_line", self.lab_subject, "pr", 10)["line"],
                          ideas=[f"post:{self.idea_post}"])
        check(not again.get("success") and "adopted once" in json.dumps(again), f"an idea was credited twice: {again}")
        m = {x["id"] for x in self.tool(self.merchant, "my_quests").get("quests", []) if x.get("done")}
        check("land-fix" not in m, "the maintainer earned from its own report")
        info = self.tool(self.beta, "lab_threads", self.lab)
        state = {t["ref"]: t["state"] for t in info.get("threads") or []}
        check(state.get(ref) == "merged", f"thread states: {state}")
        check(any(x["agent_id"] == self.beta for x in info.get("board") or []), f"the lab board: {info.get('board')}")
        with urllib.request.urlopen(f"{self.relay_url}/labs/{self.lab}/reports", timeout=10) as resp:
            check(len(json.loads(resp.read()).get("reports") or []) == 1, "the report is not public")
        desk = self.emporia(self.merchant, "operator", self.merchant)
        check("Lab activity" in desk and ref in desk and "Patches" not in desk, f"the desk: {desk.strip()[-500:]}")
        return f"{ref} merged; beta Land a fix, the player Review a fix and Idea adopted (10 points)"

    def sdk_agent(self) -> str:
        """Agent-neutral: `examples/sdk_agent/agent.py` (no Hermes, no model, its own key and operator label, client
        `sdk`, declared model `example-scripted`) posts in a topic another operator made, likes that operator's post,
        joins a public room, issues an open challenge, and plays it to the end against a Hermes-plugin agent (beta,
        another operator). The game record names both models, and quests credit both sides."""
        topic = "sdk-guests"
        made = self.tool(self.beta, "create_agora_topic", "SDK guests", slug=topic)
        check(made.get("slug") == topic, f"create_agora_topic: {made}")
        theirs = self.tool(self.beta, "post", "A Hermes agent says hello to guests on other clients.", topic=topic)
        check(theirs.get("success") and theirs.get("post_id"), f"post: {theirs}")
        keys = self.root / "sdk-keys"
        argv = [self.repo_python(), str(self.repo / "examples" / "sdk_agent" / "agent.py"), "--relay", self.relay_url,
                "--name", self.sdk, "--keys-dir", str(keys), "--topic", topic, "--timeout", "150", "--json"]
        self.note("$ " + " ".join(argv))
        err = (self.root / "sdk-agent.log").open("ab")
        proc = subprocess.Popen(argv, cwd=self.root, env=self.repo_env(), stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=err, text=True)
        self.pids.append(proc.pid)
        self.procs[proc.pid] = proc
        sid, deadline = "", time.monotonic() + 200
        while proc.poll() is None and time.monotonic() < deadline:
            if not sid:
                board = self.tool(self.beta, "list_challenges", module_type="emporia:blotto:v2")
                mine = [c for c in board.get("challenges", []) if c.get("from") == self.sdk]
                if mine:
                    sid = mine[0]["session_id"]
                    joined = self.tool(self.beta, "join_session", sid)
                    check(joined.get("success"), f"beta takes the SDK agent's challenge: {joined}")
            else:
                g = self.tool(self.beta, "get_session", sid)
                data = (g.get("state") or {}).get("data") or {}
                if g["status"] == "active" and g.get("current_agent") == self.beta:
                    if data.get("phase") == "commit":
                        call = {"call": 0.6} if str(data.get("round", 1)) == "1" else {}
                        r = self.tool(self.beta, "commit_move", sid, [5, 5, 0], "committing my planned spread", **call)
                    else:
                        r = self.tool(self.beta, "reveal_move", sid, "revealing the move I committed")
                    check(r.get("success"), f"beta's move: {r}")
            time.sleep(0.4)
        if proc.poll() is None:
            proc.terminate()
            proc.wait(timeout=10)
            raise StepFailed("the SDK agent did not finish within 200 s; see sdk-agent.log")
        out = proc.stdout.read()
        check(proc.returncode == 0, f"the SDK agent exited {proc.returncode}: {out[-300:]}; see sdk-agent.log")
        self.note(f"sdk agent summary: {out}")
        res = json.loads(out.strip().splitlines()[-1])
        check(res["model"] == "example-scripted" and res["talk"]["liked"] == theirs["post_id"], f"talk: {res['talk']}")
        game = res["game"]
        check(sid and game["session_id"] == sid and game["status"] == "completed", f"the game: {game}")
        # the game record: both players, and the SDK agent's model revealed as it declared it
        record = self.tool(self.beta, "model_results", session_id=sid)
        players = record.get("players") or {}
        check((players.get(self.sdk) or {}).get("model") == "example-scripted", f"model reveal: {record}")
        check({self.sdk, self.beta} <= set(players), f"the record names both players: {record}")
        exported = self.tool(self.beta, "records", kind="export", module_type="emporia:blotto:v2", limit=50)
        check(sid in [x.get("session_id") for x in exported.get("games", [])], f"not in the open game records: {exported}")
        # quest credit, on both sides: another operator's open challenge, taken and finished
        done = set(res["standing"]["quests_done"])
        check({"open-table", "first-contact"} <= done and res["standing"]["level"], f"the SDK agent's quests: {res['standing']}")
        theirs_done = {x["id"] for x in self.tool(self.beta, "my_quests").get("quests", []) if x.get("done")}
        check("taken-up" in theirs_done, f"taking the SDK agent's challenge counted for nothing: {sorted(theirs_done)}")
        who = (self.tool(self.beta, "agent_card", self.sdk).get("untrusted_content") or {}).get("profile") or {}
        check((who.get("client") or {}).get("name") == "sdk", f"its card says client sdk: {who.get('client')}")
        return f"{self.sdk} (sdk, example-scripted) and {self.beta}: {sid}; quests {sorted(done)[:4]}"

    def any_agent(self) -> str:
        """Any agent: `emporia agent init` makes an agent for a host that is not Hermes (own key directory, declared
        client `custom`, a declared model), `agent mcp-config generic` prints its config, and a generic MCP client
        (the `mcp` package, over stdio) launches `emporia-mcp` from that config, posts and plays a full public game
        against a Hermes-plugin agent (beta, another operator). The card says client `custom`; both models are
        revealed; the portable skill is current."""
        name = f"e2e-any-{self.sdk.rsplit('-', 1)[-1]}"
        keys = self.root / "any-agent-keys"
        py = self.repo_python()
        init = self.run([py, "-m", "emporia.cli", "agent", "init", str(keys), "--agent", name, "--relay", self.relay_url,
                         "--client", "custom", "--model", "e2e-generic-model", "--json"], env=self.repo_env())
        made = json.loads(init.stdout)
        check(made["registered"] and made["client"] == "custom", f"agent init: {init.stdout[-300:]}")
        cfg = self.run([py, "-m", "emporia.cli", "agent", "mcp-config", "generic", str(keys)], env=self.repo_env())
        text = cfg.stdout
        config = json.loads(text[text.index("{"):])
        check(not any(p.read_bytes().hex() in text for p in keys.glob("*.priv")), "the config printed a key")
        (self.root / "any-agent-config.json").write_text(json.dumps(config))
        stale = self.run([py, str(self.repo / "scripts" / "build_portable_skill.py"), "--check"], ok_codes=(0, 1))
        check(stale.returncode == 0, "the portable skill is out of date")
        topic = "any-agent-guests"
        check(self.tool(self.beta, "create_agora_topic", "Any-agent guests", slug=topic).get("slug") == topic, "topic")
        argv = [py, str(self.repo / "scripts" / "e2e_mcp_client.py"), "--config", str(self.root / "any-agent-config.json"),
                "--topic", topic, "--timeout", "150"]
        self.note("$ " + " ".join(argv))
        err = (self.root / "any-agent.log").open("ab")
        proc = subprocess.Popen(argv, cwd=self.root, env=self.repo_env(), stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=err, text=True)
        self.pids.append(proc.pid)
        self.procs[proc.pid] = proc
        sid, deadline = "", time.monotonic() + 200
        while proc.poll() is None and time.monotonic() < deadline:
            if not sid:
                board = self.tool(self.beta, "list_challenges", module_type="emporia:blotto:v2")
                mine = [c for c in board.get("challenges", []) if c.get("from") == name]
                if mine:
                    sid = mine[0]["session_id"]
                    joined = self.tool(self.beta, "join_session", sid)
                    check(joined.get("success"), f"beta takes the MCP client's challenge: {joined}")
            else:
                g = self.tool(self.beta, "get_session", sid)
                data = (g.get("state") or {}).get("data") or {}
                if g["status"] == "active" and g.get("current_agent") == self.beta:
                    if data.get("phase") == "commit":
                        call = {"call": 0.6} if str(data.get("round", 1)) == "1" else {}
                        r = self.tool(self.beta, "commit_move", sid, [7, 2, 1], "committing my planned spread", **call)
                    else:
                        r = self.tool(self.beta, "reveal_move", sid, "revealing the move I committed")
                    check(r.get("success"), f"beta's move: {r}")
            time.sleep(0.4)
        if proc.poll() is None:
            proc.terminate()
            proc.wait(timeout=10)
            raise StepFailed("the MCP client did not finish within 200 s; see any-agent.log")
        out = proc.stdout.read()
        check(proc.returncode == 0, f"the MCP client exited {proc.returncode}: {out[-300:]}; see any-agent.log")
        res = json.loads(out.strip().splitlines()[-1])
        check(res["registered"] and res["post"] and res["status"] == "completed" and res["session_id"] == sid,
              f"the MCP client: {res}")
        players = (res["models"] or {}).get("players") or {}
        check((players.get(name) or {}).get("model") == "e2e-generic-model", f"model reveal: {res['models']}")
        check({name, self.beta} <= set(players), f"the record names both players: {res['models']}")
        who = (self.tool(self.beta, "agent_card", name).get("untrusted_content") or {}).get("profile") or {}
        check((who.get("client") or {}).get("name") == "custom" and (who.get("client") or {}).get("declared"),
              f"its card says client custom (declared): {who.get('client')}")
        return f"{name} (custom, via {res['tools']} MCP tools) and {self.beta}: {sid}"

    def lab_forged(self) -> str:
        """A claim whose number was edited after signing, and a claim line for another repo, are refused."""
        import base64
        blob = self.lab_pr_line.split()[-1]
        d = json.loads(base64.urlsafe_b64decode(blob + "=" * (-len(blob) % 4)))
        d["claim"]["number"] = 8
        forged = base64.urlsafe_b64encode(json.dumps(d, sort_keys=True, separators=(",", ":")).encode()).decode().rstrip("=")
        out = self.tool(self.merchant, "lab_report", self.lab, f"gh_pr:{self.lab_subject}#8", "merged",
                        f"Emporia: x@e2e {forged}")
        check(not out.get("success") and "does not verify" in json.dumps(out), f"a forged claim was taken: {out}")
        other = self.tool(self.beta, "github_claim_line", "elsewhere/repo", "pr", 9)
        out = self.tool(self.merchant, "lab_report", self.lab, f"gh_pr:{self.lab_subject}#9", "merged", other["line"])
        check(not out.get("success") and "not this lab's" in json.dumps(out), f"another repo's claim was taken: {out}")
        return "refused: a forged claim, a claim for another repo"

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
    # ── the signer (phase 2): keys made inside it, a sealed DM opened through it ──
    def repo_python(self) -> str:
        py = self.repo / ".venv" / "bin" / "python"
        return str(py if py.exists() else sys.executable)

    def repo_env(self, **extra: str) -> dict[str, str]:
        env = self.env(EMPORIA_NO_DOTENV="1", **extra)
        env["PYTHONPATH"] = os.pathsep.join(p for p in (str(self.repo / "src"), env.get("PYTHONPATH", "")) if p)
        return env

    def start_signer(self) -> str:
        """A signer of this run's own, in practice mode (this user), on a socket in the run's folder
        (relative: a Unix socket's path has 107 bytes at most, and this folder's is long)."""
        folder = self.root / "signer"
        out = (self.root / "signer.log").open("ab")
        proc = subprocess.Popen([self.repo_python(), "-m", "emporia.cli", "signer", "serve", "--dir", str(folder),
                                 "--socket", "signer.sock"], cwd=self.root, stdin=subprocess.DEVNULL,
                                env=self.repo_env(EMPORIA_SIGNER_DIR=str(folder)), stdout=out, stderr=out)
        self.pids.append(proc.pid)
        self.procs[proc.pid] = proc
        deadline = time.time() + 30
        while time.time() < deadline and not (self.root / "signer.sock").is_socket():
            check(proc.poll() is None, f"the signer exited ({proc.returncode}); see signer.log")
            time.sleep(0.2)
        check((self.root / "signer.sock").is_socket(), "the signer made no socket in 30s")
        return f"pid {proc.pid}"

    def signer_as_agent(self, code: str) -> dict:
        """Run `code` as the signer-held agent: its env names the signer and its markers, no key."""
        token = (self.root / "signer-agent.token").read_text().strip()
        env = self.repo_env(EMPORIA_SIGNER_URL="unix://signer.sock", EMPORIA_SIGNER_TOKEN=token,
                            EMPORIA_AGENT_ID=self.signed, EMPORIA_KEYS_DIR=str(self.root / "signed-keys"),
                            EMPORIA_RELAY_URL=self.relay_url, HERMES_HOME=str(self.root / "signed-home"))
        p = self.run([self.repo_python(), "-c", code], env=env, timeout=120)
        return json.loads(p.stdout.strip().splitlines()[-1])

    def signer_agent(self) -> str:
        """2c: the agent's identity, encryption and wallet keys made inside the signer; its keys
        folder gets only the public halves; it registers, every signature the signer's."""
        folder = self.root / "signer"
        keys = self.root / "signed-keys"
        keys.mkdir()
        (self.root / "signed-home").mkdir()
        made = self.run([self.repo_python(), "-c", (
            "import json, os\nfrom pathlib import Path\nfrom emporia import identity, signer\n"
            f"aid = {self.signed!r}\n"
            "made = signer.make_agent_keys(aid)\n"
            f"signer.save_policy(aid, {{'EMPORIA_RELAY_URL': {self.relay_url!r}}})\n"
            f"t = Path({str(self.root / 'signer-agent.token')!r})\n"
            "identity._write_secret_atomically(t, signer.new_agent_token(aid).encode())\n"
            f"k = Path({str(keys)!r})\n"
            "(k / (aid + '.signer.pub')).write_text(made['identity'] + '\\n')\n"
            "(k / (aid + '.x25519.signer.pub')).write_text(made['encryption'] + '\\n')\n"
            "print(json.dumps(sorted(made)))")], env=self.repo_env(EMPORIA_SIGNER_DIR=str(folder))).stdout
        check(json.loads(made.strip().splitlines()[-1]) == ["encryption", "evm", "identity", "svm"], made[-200:])
        out = self.signer_as_agent(
            "import json\nfrom emporia import mcp_server as m\n"
            "r = m.register_agent(agent_id=m.AGENT_ID)\nprint(json.dumps({'ok': bool(r.get('success')), "
            "'error': str(r.get('error', ''))[:200]}))")
        check(out["ok"], f"registering through the signer failed: {out['error']}")
        # Its records and pins are public data and live here too; a key is any of these.
        loose = [f.name for f in keys.rglob("*") if f.is_file() and not f.name.endswith(".signer.pub")
                 and any(s in f.name for s in (".priv", ".x25519.", ".x402.", ".ed25519", ".secp256k1"))]
        check(not loose, f"key files in the agent's folder: {loose}")
        return "registered, no key in its folder"

    def signer_dm(self) -> str:
        """2b: the player seals a DM to it; the signer opens it for the agent, which holds no key."""
        text = f"sealed for the signer {secrets.token_hex(4)}"
        sent = self.tool(self.player, "send_dm", self.signed, text, encrypted=True)
        check(sent.get("success"), f"send_dm: {str(sent)[:200]}")
        out = self.signer_as_agent(
            "import json\nfrom emporia import mcp_server as m\n"
            "threads = m.list_dm_threads().get('threads') or []\n"
            "tid = threads[0]['thread_id'] if threads else ''\n"
            "msgs = (m.get_dm_messages(tid) if tid else {}).get('messages') or []\n"
            "print(json.dumps([[x.get('content'), x.get('decrypted'), x.get('encrypted')] for x in msgs]))")
        check([text, True, True] in out, f"the DM was not opened through the signer: {str(out)[:300]}")
        return "opened by the signer"

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
        found: set[str] = set()
        for scandir in service_roots():
            try:
                found |= {p.name for p in scandir.iterdir() if p.name.startswith(S6_SERVICE_PREFIX)}
            except OSError:
                continue
        return found

    def _own_names(self) -> list[str]:
        return [self.judge, self.player, self.merchant, self.house_agent, self.signed, self.beta, self.newcomer,
                self.sdk]

    def _delete_profile(self, name: str) -> None:
        """`hermes profile delete NAME --yes` with the temp homes: how Hermes removes a profile's gateway service."""
        if not self.hermes:
            return
        try:
            self.run([self.hermes, "profile", "delete", name, "--yes"], timeout=120, ok_codes=None)
        except (OSError, subprocess.TimeoutExpired) as exc:
            self.note(f"profile delete {name}: {exc}")

    def _remove_own_services(self) -> list[str]:
        """This run's gateway services (registered by `hermes profile create`, which has no opt-out) and their log
        folders, removed: see `cleanup_gateways`. Returns the services still there."""
        res = cleanup_gateways(self._own_names(), self.tag, log_root=host_log_root(), delete=self._delete_profile,
                               services_before=self.services_before)
        self.note(f"log folders removed: {res['logs_removed']}; left: {res['logs_left']}")
        return res["services_left"]

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
        run.step(f"{run.merchant} registers with the operator label of its own", run.register_label, run.merchant)
        for name in (run.player, run.merchant):
            run.step(f"{name}: role, keys, plugin, registered, doctor", run.check_agent, name)
        run.step(f"setup again on {run.player}: it keeps its model, copies no provider key, keeps its identity",
                 run.setup_again, run.player)
        run.step("playbook refusals name their fix", run.refusals)
        sid = run.step("free blotto, end to end", run.blotto)
        run.step("illegal commit and missing session refused", run.illegal_commit)
        run.step("likes", run.like, sid)
        run.step("agora v2: a public topic post is in the main feed and whats_next's happening; a sealed one is not",
                 run.agora, sid)
        run.step("rooms v2: a reply, a reaction, the feed's best, table talk as a thread", run.rooms_v2)
        run.step("an open challenge taken and an arc step done; a direct one declined", run.challenges)
        run.step("quests, sheet, season, activity", run.standing)
        run.step("a signer of the run's own", run.start_signer)
        run.step(f"{run.signed}: keys made in the signer, registered through it", run.signer_agent)
        run.step("a sealed DM opened through the signer", run.signer_dm)
        run.step(f"setup {run.house_agent} (house)", run.setup_agent, run.house_agent, "--playbook", "host")
        run.step("the relay pins its operator and house keys (restart)", run.pin_operator)
        run.step("house adopts a waiting blotto and finishes it", run.house)
        run.step("bargain v2 between two players on the drand beacon: hands only client-side, reveal, hands published", run.bargain)
        run.step("the operator sees its desk; a player is refused", run.operator)
        run.step(f"setup {run.beta} (player)", run.setup_agent, run.beta, "--playbook", "player")
        run.step("games pack: a 3-operator common-value auction on the drand beacon, exported and re-verified offline",
                 run.auction)
        run.step("labs: a person makes a lab for a repo whose file names its maintainer", run.lab_create)
        run.step("labs: another operator's agent gets a claim line for a pull request; a reviewer gets one", run.lab_claims)
        run.step("labs: the contributor posts in the pull request's thread", run.lab_thread)
        run.step("labs: the maintainer reports it merged with a credited review and an idea post; three quests complete",
                 run.lab_report)
        run.step("labs: a forged claim and a claim for another repo are refused", run.lab_forged)
        run.step("the card's X link: a claim line, a recorded fake post, another operator's confirmation, the quest",
                 run.x_link_flow)
        run.step("agent-neutral: a plain Python SDK agent plays a full public game against a Hermes agent; record, model reveal, quests",
                 run.sdk_agent)
        run.step("any agent: `agent init`, `mcp-config`, and a generic MCP client over stdio plays a full public game; "
                 "declared client custom", run.any_agent)
        run.step("first run: the operator seeds the relay; a new agent is told it is in and gets the guided path",
                 run.first_run)
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
