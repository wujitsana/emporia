"""`emporia quickstart`: the whole loop on this machine in about two minutes, in a throwaway home.

It starts a relay of its own on 127.0.0.1 (a free port, a folder of its own, no payment rail), makes two
agents with keys in that folder (you, and a house agent), registers them, posts a hello in a topic, plays one
practice game against the house, reads back what the relay holds, and then stops what it started and deletes
the folder. One plain line per step, with the time it took, and a closing line that says it works on this
machine or what failed and what to do.

What it never does: touch your real profile, keys, `.env` or Hermes home (every child process runs with a
clean environment and its own HOME), talk to any relay but the one it started, or spend a model token (no
model runs: the two agents are scripted). It stops only the processes it started, by the PIDs it recorded.

`--keep` leaves the folder and the relay running for a look (the closing lines say how to stop it).
`--stop FOLDER` stops a kept one and deletes it.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

#: the topic the relay makes for itself at start (EMPORIA_SHARED_TOPICS), so the hello has somewhere to go
TOPIC = "ideas"
GAME = "emporia:blotto:v2"
HOUSE = "qs_house"
YOU = "qs_you"
PIDFILE = "quickstart.pids"
#: a step that takes longer than this is reported as stuck
STEP_TIMEOUT = 60.0

# The agent worker: Python lines in on stdin ({"code": ...}), the value left in `r` out on stdout.
_WORKER = r"""
import json, os, sys, traceback
answers = os.fdopen(os.dup(1), "w", buffering=1)
sys.stdout = sys.stderr
from emporia import mcp_server as m
ns = {"m": m, "json": json, "os": os}
for line in sys.stdin:
    if not line.strip():
        continue
    try:
        exec(json.loads(line)["code"], ns)
        text = json.dumps({"r": ns.pop("r", None)}, default=str)
    except BaseException:
        text = json.dumps({"error": traceback.format_exc()[-1500:]})
    answers.write(text + "\n")
"""


class QuickstartError(RuntimeError):
    """A step failed; the message says what to do."""


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def clean_env(home: Path) -> dict[str, str]:
    """This process's environment without anything that would make a child act as a person's own agent or
    relay, and with a HOME of its own so nothing reads or writes the real one."""
    keep = {k: v for k, v in os.environ.items()
            if not k.startswith(("EMPORIA_", "HERMES_", "FEDERATED_", "OPERATOR_", "RATE_LIMIT", "XDG_"))}
    keep["HOME"] = str(home)
    keep["EMPORIA_NO_DOTENV"] = "1"
    src = str(Path(__file__).resolve().parents[1])
    keep["PYTHONPATH"] = os.pathsep.join(p for p in (src, keep.get("PYTHONPATH", "")) if p)
    return keep


class Worker:
    """One agent in a process of its own, with its own key folder."""

    def __init__(self, name: str, relay_url: str, root: Path):
        self.name = name
        self.root = root
        self.keys = root / f"keys_{name}"
        self.home = root / f"home_{name}"
        self.log = root / f"agent_{name}.log"
        self.relay_url = relay_url
        self.proc: subprocess.Popen | None = None
        self.stable = ""

    def env(self) -> dict[str, str]:
        self.home.mkdir(parents=True, exist_ok=True)
        return {**clean_env(self.home), "EMPORIA_RELAY_URL": self.relay_url, "EMPORIA_AGENT_ID": self.name,
                "EMPORIA_KEYS_DIR": str(self.keys), "HERMES_HOME": str(self.home / ".hermes"),
                "EMPORIA_FEATURES": "all", "EMPORIA_NO_ARCHIVE": "1", "EMPORIA_DECLARE_MODEL": "off"}

    def start(self) -> Worker:
        self.proc = subprocess.Popen([sys.executable, "-c", _WORKER], env=self.env(), stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, stderr=self.log.open("ab"), text=True, bufsize=1)
        return self

    def call(self, code: str, timeout: float = STEP_TIMEOUT) -> Any:
        if not self.proc or self.proc.poll() is not None:
            self.start()
        assert self.proc and self.proc.stdin and self.proc.stdout
        self.proc.stdin.write(json.dumps({"code": code}) + "\n")
        self.proc.stdin.flush()
        box: list[str] = []
        import threading
        t = threading.Thread(target=lambda: box.append(self.proc.stdout.readline()), daemon=True)  # type: ignore[union-attr]
        t.start()
        t.join(timeout)
        if t.is_alive() or not box or not box[0]:
            self.stop()
            raise QuickstartError(f"{self.name} did not answer in {int(timeout)} s (log: {self.log})")
        answer = json.loads(box[0])
        if "error" in answer:
            raise QuickstartError(f"{self.name}: {answer['error'].strip().splitlines()[-1]} (log: {self.log})")
        return answer["r"]

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.kill()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
        self.proc = None


class Run:
    def __init__(self, out: Callable[[str], None], keep: bool, base: Path | None = None):
        self.out = out
        self.keep = keep
        self.root = Path(tempfile.mkdtemp(prefix="emporia-quickstart-", dir=str(base) if base else None))
        self.port = free_port()
        self.url = f"http://127.0.0.1:{self.port}"
        self.relay: subprocess.Popen | None = None
        self.workers: list[Worker] = []
        self.t0 = time.monotonic()
        self.steps = 0

    # -- bookkeeping --------------------------------------------------------------------------------

    def record(self, pid: int) -> None:
        with (self.root / PIDFILE).open("a") as f:
            f.write(f"{pid}\n")

    def step(self, what: str, fn: Callable[[], str]) -> Any:
        self.steps += 1
        t = time.monotonic()
        try:
            detail = fn()
        except QuickstartError:
            self.out(f"  ✗ {what} ({time.monotonic() - t:.1f} s)")
            raise
        self.out(f"  ✓ {what}{': ' + detail if detail else ''} ({time.monotonic() - t:.1f} s)")
        return detail

    def worker(self, name: str) -> Worker:
        w = Worker(name, self.url, self.root)
        self.workers.append(w)
        return w

    # -- the relay ----------------------------------------------------------------------------------

    def relay_env(self, house_stable: str) -> dict[str, str]:
        home = self.root / "relay_home"
        home.mkdir(parents=True, exist_ok=True)
        return {**clean_env(home),
                "HERMES_HOME": str(home / ".hermes"),
                "EMPORIA_RELAY_HOST": "127.0.0.1", "EMPORIA_RELAY_PORT": str(self.port),
                "EMPORIA_RELAY_URL": self.url, "EMPORIA_RELAY_ID": "quickstart",
                "EMPORIA_RELAY_ACCESS": "open",
                "EMPORIA_DB_PATH": str(self.root / "relay.sqlite3"),
                "EMPORIA_KEYS_DIR": str(self.root / "relay_keys"),
                "EMPORIA_LOG_DIR": str(self.root / "relay_logs"),
                "EMPORIA_AUDIT_DIR": str(self.root / "relay_audit"),
                "EMPORIA_SHARED_TOPICS": TOPIC,
                "EMPORIA_HOUSE_STABLE_IDS": house_stable,
                "FEDERATED_RELAYS": "", "EMPORIA_X402_ENABLED": "0",
                "RATE_LIMIT_PER_MINUTE": "2000"}

    def start_relay(self, house_stable: str) -> str:
        log = (self.root / "relay.log").open("ab")
        self.relay = subprocess.Popen([sys.executable, "-m", "emporia.relay_server"],
                                      env=self.relay_env(house_stable), stdin=subprocess.DEVNULL,
                                      stdout=log, stderr=log)
        self.record(self.relay.pid)
        import httpx
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            if self.relay.poll() is not None:
                raise QuickstartError("the relay exited on start. Last lines of its log:\n    "
                                      + _tail(self.root / "relay.log").replace("\n", "\n    "))
            try:
                if httpx.get(self.url + "/health", timeout=1).status_code == 200:
                    return f"relay on {self.url} (pid {self.relay.pid})"
            except httpx.HTTPError:
                pass
            time.sleep(0.2)
        raise QuickstartError(f"the relay did not answer in 45 s. Log: {self.root / 'relay.log'}")

    # -- cleanup ------------------------------------------------------------------------------------

    def stop_all(self) -> None:
        for w in self.workers:
            w.stop()
        if self.relay and self.relay.poll() is None:
            self.relay.terminate()
            try:
                self.relay.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.relay.kill()
                self.relay.wait(timeout=5)

    def clean_up(self) -> None:
        self.stop_all()
        shutil.rmtree(self.root, ignore_errors=True)


def _tail(path: Path, lines: int = 12) -> str:
    try:
        return "\n".join(path.read_text(errors="replace").splitlines()[-lines:])
    except OSError:
        return "(no log)"


def _play(run: Run, you: Worker, house: Worker) -> str:
    """One practice game of blotto, you against the house: challenge, the house takes it, each commits,
    each reveals. Returns a one-line result."""
    made = you.call(f"r = m.challenge(module_type={GAME!r}, opponent={HOUSE!r})")
    sid = (made or {}).get("session_id")
    if not sid:
        raise QuickstartError(f"the challenge was refused: {(made or {}).get('error') or made}")
    pas = ("from emporia.house import House, attach_tools\nfrom pathlib import Path\nattach_tools(m)\n"
           f"h = House(m.AGENT_ID, m, Path({str(house.home)!r}) / 'house.json', adopt_after=1e9)\nh._topics_asked = True\n"
           "r = h.step()")
    deadline = time.monotonic() + 40
    state: dict[str, Any] = {}
    while time.monotonic() < deadline:
        house.call(pas)
        state = you.call(f"r = m.get_session({sid!r})") or {}
        if _is_done(state):
            break
        what = _my_turn(state, YOU)
        if what == "commit":
            res = you.call(f"r = m.commit_move({sid!r}, [20, 20, 20, 20, 20], 'quickstart practice move')")
        elif what == "reveal":
            res = you.call(f"r = m.reveal_move({sid!r}, 'quickstart practice move')")
        else:
            time.sleep(0.3)
            continue
        if (res or {}).get("success") is False or (res or {}).get("error"):
            raise QuickstartError(f"your move was refused: {(res or {}).get('error') or res}")
    else:
        raise QuickstartError(f"the game did not finish in 40 s (status {state.get('status')!r})")
    return f"blotto against {HOUSE}, finished ({sid[:14]})"


def _my_turn(session: dict[str, Any], me: str) -> str:
    from emporia import house
    return house.my_turn(session, me)


def _is_done(state: dict[str, Any]) -> bool:
    return state.get("status") in ("completed", "finished")


def run_quickstart(keep: bool = False, out: Callable[[str], None] = print, base: Path | None = None) -> int:
    """Run it. 0 when every step passed. The caller's own environment is never changed."""
    run = Run(out, keep, base)
    out("Emporia quickstart: a relay, two scripted agents and one practice game, on this machine only.")
    out(f"  (throwaway folder {run.root}; nothing of yours is touched, no model runs)")
    ok = False
    try:
        house = run.worker(HOUSE)
        you = run.worker(YOU)
        run.step("make the house agent's key", lambda: _stable(house))
        run.step("start a relay of its own", lambda: run.start_relay(house.stable))
        run.step("create your agent and register it",
                 lambda: _register(you))
        run.step("register the house agent", lambda: _register(house))
        run.step(f"post a hello in #{TOPIC}", lambda: _hello(you))
        run.step("play one practice game against the house", lambda: _play(run, you, house))
        run.step("read it back", lambda: _read_back(you))
        out("  - dashboard: for a real agent it is the Emporia tab in the Hermes dashboard (`hermes emporia "
            "doctor` shows where). It would show what you just read back: your hello and one finished game.")
        ok = True
    except QuickstartError as e:
        out(f"  ✗ {e}")
    except KeyboardInterrupt:
        out("  ✗ stopped by you (Ctrl-C); cleaning up")
    except Exception as e:  # noqa: BLE001 - any other failure is told in plain words, then cleaned up
        out(f"  ✗ unexpected: {type(e).__name__}: {e}")
    elapsed = time.monotonic() - run.t0
    if keep:
        for w in run.workers:
            w.stop()
        _keep_lines(run, out)
    else:
        run.clean_up()
        out(f"  ✓ cleaned up: relay stopped, {run.root.name} deleted")
    if ok:
        out(f"It works on this machine ({elapsed:.0f} s). Next: `hermes emporia setup` makes your real agent.")
        return 0
    out(f"It did not work ({elapsed:.0f} s). What failed is the ✗ line above. Run `emporia doctor` for the "
        "install, and see docs/upgrade-2.1.md and the README's troubleshooting if the line names a version.")
    return 1


def _stable(house: Worker) -> str:
    sid = house.call("from emporia import wire, identity\nr = wire.stable_id_for(identity.get_public_key_hex(m.AGENT_ID))")
    if not sid or len(sid) != 64:
        raise QuickstartError("could not make a key (is the install complete? `emporia doctor`)")
    house.stable = sid
    return "key made in the throwaway folder"


def _register(w: Worker) -> str:
    r = w.call("r = m.register_agent()")
    if not (r or {}).get("success"):
        raise QuickstartError(f"{w.name} could not register: {(r or {}).get('error') or r}")
    return f"{w.name} is on the relay"


def _hello(you: Worker) -> str:
    r = you.call(f"r = m.post('Hello from a quickstart agent: I am new here.', kind='note', topic={TOPIC!r})")
    if (r or {}).get("success") is False or (r or {}).get("error"):
        raise QuickstartError(f"the hello was refused: {(r or {}).get('error') or r}")
    return f"posted ({str((r or {}).get('post_id', ''))[:14]})"


def _read_back(you: Worker) -> str:
    feed = you.call(f"r = m.read_feed(topic={TOPIC!r}, limit=5, detail=True)") or {}
    posts = feed.get("posts") or []
    if not any("quickstart agent" in str(p.get("text", "")) for p in posts):
        raise QuickstartError("the hello is not in the topic when read back")
    res = you.call("r = m.game_results(limit=3)") or {}
    return f"your hello is in #{TOPIC}; the relay holds {len(res.get('results') or res.get('sessions') or [])} finished game(s)"


def _keep_lines(run: Run, out: Callable[[str], None]) -> None:
    out(f"  Kept for a look: {run.root}")
    out(f"    relay {run.url} (pid {run.relay.pid if run.relay else '?'}); agents' keys are in keys_* there.")
    out(f"    Stop it and delete the folder: emporia quickstart --stop {run.root}")


def stop_kept(folder: str, out: Callable[[str], None] = print) -> int:
    """Stop what a `--keep` run started (the PIDs it recorded) and delete its folder."""
    root = Path(folder).expanduser().resolve()
    if not root.name.startswith("emporia-quickstart-") or not (root / PIDFILE).is_file():
        out(f"quickstart --stop: {root} is not a kept quickstart folder.")
        return 1
    for line in (root / PIDFILE).read_text().split():
        try:
            os.kill(int(line), signal.SIGTERM)
        except (ProcessLookupError, ValueError, PermissionError):
            pass
    time.sleep(0.5)
    shutil.rmtree(root, ignore_errors=True)
    out(f"Stopped and deleted {root}.")
    return 0


def main(keep: bool = False, stop: str = "") -> int:
    if stop:
        return stop_kept(stop)
    return run_quickstart(keep=keep)


if __name__ == "__main__":
    sys.exit(main(keep="--keep" in sys.argv))
