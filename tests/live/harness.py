"""The pieces of the live suite (P248): real relay processes, agents with key
folders of their own, and a hostile operator's proxy.

Nothing here patches the code under test. A relay is `python -m emporia.relay_server`
with its own port, database, key folder and relay key; an agent is a process that
imports `emporia.mcp_server` and talks to its relay over HTTP; the rogue relay
is a real HTTP hop in front of one relay.
"""
from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import unquote

import httpx
from cryptography.hazmat.primitives.asymmetric import ed25519

from emporia.identity import federation_signable_bytes, sign_payload
from emporia.wire import signed_encryption_key_payload

WORKER = Path(__file__).with_name("agent_worker.py")
OPERATOR = "relay_ops"       # the agent each relay's EMPORIA_AGENT_ID names: it may run a pull


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def clean_env() -> dict[str, str]:
    """This process's environment without anything that would make a child act
    as a person's own relay or agent."""
    return {k: v for k, v in os.environ.items()
            if not k.startswith(("EMPORIA_", "STRIPE_", "HERMES_", "FEDERATED_", "OPERATOR_", "RATE_LIMIT"))}


def tail(path: Path, lines: int = 25) -> str:
    try:
        return "\n".join(path.read_text(errors="replace").splitlines()[-lines:])
    except OSError:
        return "(no log)"


# --- relays -------------------------------------------------------------------

class Relay:
    """A relay process on 127.0.0.1, with a folder of its own for everything it keeps."""

    def __init__(self, name: str, root: Path, port: int | None = None):
        self.name = name
        self.root = root / f"relay_{name}"
        self.root.mkdir(parents=True)
        self.port = port or free_port()
        self.url = f"http://127.0.0.1:{self.port}"
        self.host = f"127.0.0.1:{self.port}"
        self.log = self.root / "relay.log"
        self.keys = self.root / "keys"
        self.links: list[str] = []
        self.extra: dict[str, str] = {}
        self.proc: subprocess.Popen | None = None
        self.operator: Agent | None = None

    def env(self) -> dict[str, str]:
        """What `emporia relay start` gives a relay, plus the folders: its URL as others reach it,
        open access, no payment rail (so no money can move), links as `emporia relay link` writes them."""
        return {
            **clean_env(),
            "EMPORIA_NO_DOTENV": "1",
            "HERMES_HOME": str(self.root / "hermes"),
            "EMPORIA_RELAY_HOST": "127.0.0.1",
            "EMPORIA_RELAY_PORT": str(self.port),
            "EMPORIA_RELAY_URL": self.url,
            "EMPORIA_RELAY_ID": f"live-{self.name}",
            "EMPORIA_RELAY_ACCESS": "open",
            "EMPORIA_AGENT_ID": OPERATOR,
            "EMPORIA_DB_PATH": str(self.root / "relay.sqlite3"),
            "EMPORIA_KEYS_DIR": str(self.keys),
            "EMPORIA_LOG_DIR": str(self.root / "logs"),
            "EMPORIA_AUDIT_DIR": str(self.root / "audit"),
            "FEDERATED_RELAYS": ",".join(self.links),
            "EMPORIA_FEDERATED_DMS": "1",
            "EMPORIA_FEDERATION_SYNC_INTERVAL_SECONDS": "3600",   # pulls happen when a test says so
            "EMPORIA_X402_ENABLED": "0",
            "RATE_LIMIT_PER_MINUTE": "2000",
            **self.extra,
        }

    def start(self) -> Relay:
        out = self.log.open("ab")
        self.proc = subprocess.Popen([sys.executable, "-m", "emporia.relay_server"], env=self.env(),
                                     stdin=subprocess.DEVNULL, stdout=out, stderr=out)
        return self

    def wait_healthy(self, timeout: float = 40) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.proc and self.proc.poll() is not None:
                raise RuntimeError(f"relay {self.name} exited ({self.proc.returncode}):\n{tail(self.log)}")
            try:
                if httpx.get(self.url + "/health", timeout=1).status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            time.sleep(0.2)
        raise RuntimeError(f"relay {self.name} did not answer /health in {timeout}s:\n{tail(self.log)}")

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=5)

    def restart(self) -> None:
        self.stop()
        self.start()
        self.wait_healthy()

    def health(self) -> dict[str, Any]:
        return httpx.get(self.url + "/health", timeout=5).json()

    def rotate_key(self) -> dict[str, str]:
        """`emporia relay rotate-key`, run against this relay's key folder while it is stopped.
        The relay signs with the new key from its next start."""
        code = "import json\nfrom emporia import identity\nprint(json.dumps(identity.rotate_relay_key()))"
        out = subprocess.run([sys.executable, "-c", code], env={**self.env()}, capture_output=True, text=True,
                             timeout=60)
        assert out.returncode == 0, out.stderr[-1500:]
        return json.loads(out.stdout.strip().splitlines()[-1])

    def replace_key_without_handover(self) -> None:
        """What a different server answering at this relay's URL looks like: a new relay key, and no signed
        hand-over from the old one. Run while the relay is stopped."""
        for name in ("_relay_identity.priv", "_relay_key_history.json"):
            (self.keys / name).unlink(missing_ok=True)

    def sync(self) -> dict[str, Any]:
        """Pull from every linked relay now, the way the relay's own timer does: the operator's
        `POST /gaming/v1/federate/sync`. Returns what each peer gave and the sync record
        (`GET /federation/peers`) afterwards."""
        assert self.operator, "the fleet registers an operator on every relay"
        r = self.operator.call(
            "r = m._bearer_request('post', m.RELAY_URL + '/gaming/v1/federate/sync', agent_id=m.AGENT_ID,"
            " timeout=60)\nr = [r.status_code, r.json()]")
        assert r[0] == 200, r
        return {"synced": r[1]["synced"], "peers": self.peers()}

    def peers(self) -> dict[str, dict[str, Any]]:
        body = httpx.get(self.url + "/federation/peers", timeout=10).json()
        return {p["url"]: p for p in body["peers"]}


# --- agents -------------------------------------------------------------------

class Agent:
    """An agent in a process of its own: its key folder, its home relay. `call` runs Python in it
    (with `m`, the MCP tool module) and returns what it left in `r`."""

    def __init__(self, name: str, home: str, root: Path, **env: str):
        self.name = name
        self.home = home.rstrip("/")
        self.keys = root / f"keys_{name}"
        self.env_extra = env
        self.stderr = root / f"agent_{name}.log"
        self._address = ""
        self.proc: subprocess.Popen | None = None
        self._lock = threading.Lock()

    def _env(self) -> dict[str, str]:
        return {**clean_env(), "EMPORIA_NO_DOTENV": "1", "EMPORIA_RELAY_URL": self.home,
                "EMPORIA_AGENT_ID": self.name, "EMPORIA_KEYS_DIR": str(self.keys),
                "HERMES_HOME": str(self.keys.parent / f"hermes_{self.name}"),
                "EMPORIA_NO_ARCHIVE": "1", **self.env_extra}

    def start(self) -> Agent:
        self.stderr.parent.mkdir(parents=True, exist_ok=True)
        self.proc = subprocess.Popen([sys.executable, str(WORKER)], env=self._env(), stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, stderr=self.stderr.open("ab"), text=True, bufsize=1)
        return self

    def call(self, code: str, timeout: float = 90) -> Any:
        """Run `code` as this agent; the value is whatever it assigns to `r`."""
        if not self.proc or self.proc.poll() is not None:
            self.start()
        with self._lock:
            self.proc.stdin.write(json.dumps({"code": code}) + "\n")
            self.proc.stdin.flush()
            box: list[str] = []
            reader = threading.Thread(target=lambda: box.append(self.proc.stdout.readline()), daemon=True)
            reader.start()
            reader.join(timeout)
            if reader.is_alive() or not box or not box[0]:
                self.stop()
                raise TimeoutError(f"{self.name}: no answer in {timeout}s to:\n{code}\n{tail(self.stderr)}")
        answer = json.loads(box[0])
        if "error" in answer:
            raise AssertionError(f"{self.name}: {answer['error']}")
        return answer["r"]

    def tool(self, tool_name: str, /, **kwargs: Any) -> Any:
        """Call an MCP tool function by name with keyword arguments."""
        return self.call(f"r = m.{tool_name}(**json.loads({json.dumps(json.dumps(kwargs))}))")

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait(timeout=5)
        self.proc = None

    def restart(self, home: str | None = None, **env: str) -> Agent:
        """The process again, as after a settings change: same key folder, maybe another home relay."""
        self.stop()
        if home:
            self.home = home.rstrip("/")
        self.env_extra = {**self.env_extra, **env}
        return self.start()

    def register(self) -> dict[str, Any]:
        r = self.tool("register_agent", agent_id=self.name)
        assert r["success"], r
        self._address = r["address"]           # as its relay names it, whatever hop the agent came through
        return r

    def rotate_key(self) -> dict[str, Any]:
        """`emporia agent rotate-key`: a new identity key, the change signed by the old one, on this agent's
        home relay. The agent then starts again with the key now on disk and registers, which signs its
        encryption key and relay list with the new one (what the command does after the relay has the key)."""
        out = self.call(
            "from pathlib import Path\n"
            "from emporia import key_rotation\n"
            "c = httpx.Client(base_url=m.RELAY_URL, timeout=30)\n"
            "res = key_rotation.rotate_identity_key(m.AGENT_ID, Path(os.environ['EMPORIA_KEYS_DIR']), c)\n"
            "r = {k: (v if k == 'key_log' else str(v)) for k, v in res.items()}")
        self.restart()
        self.register()
        return out

    def make_recovery_key(self, out_file: Path) -> str:
        """`emporia agent recovery-key --out FILE`: publish the hash of a new recovery key (signed by the
        identity key) and write the key itself to `out_file`, 0600. Returns the commitment."""
        return self.call(
            "from pathlib import Path\n"
            "from emporia import key_recovery\n"
            "c = httpx.Client(base_url=m.RELAY_URL, timeout=30)\n"
            f"out = key_recovery.make_recovery_key(m.AGENT_ID, Path(os.environ['EMPORIA_KEYS_DIR']), c, Path({str(out_file)!r}))\n"
            "r = out['commitment']")

    def recover_key(self, recovery_file: Path, compromised_since: str = "") -> dict[str, Any]:
        """`emporia agent recover`: a new identity key authorised by the recovery key in `recovery_file`,
        which ends the old key's time at `compromised_since` (an ISO time). Then the agent starts again
        with the new key and registers."""
        out = self.call(
            "from pathlib import Path\n"
            "from emporia import key_recovery\n"
            "c = httpx.Client(base_url=m.RELAY_URL, timeout=30)\n"
            f"key = key_recovery.decode_recovery_key(Path({str(recovery_file)!r}).read_text())\n"
            "res = key_recovery.recover_identity_key(m.AGENT_ID, Path(os.environ['EMPORIA_KEYS_DIR']), c, key,\n"
            f"                                       compromised_since={compromised_since!r})\n"
            "r = {k: (v if k in ('key_log', 'superseded') else str(v)) for k, v in res.items()}")
        self.restart()
        self.register()
        return out

    @property
    def public_key(self) -> str:
        return self.call("from emporia import identity\nr = identity.get_public_key_hex(m.AGENT_ID)")

    @property
    def address(self) -> str:
        return self._address or f"{self.name}@{self.home.split('//')[1]}"


# --- a hostile operator ---------------------------------------------------------

#: feed path -> the key its items sit under
FEEDS = {"/gaming/v1/federate/listings": "listings", "/federation/v1/open-sessions": "open_sessions",
         "/federation/v1/rooms": "rooms", "/federation/v1/topics": "topics"}
_AGENT_PATH = re.compile(r"^/agents/[^/]+(/encryption-key|/card)?$")


class RogueRelay:
    """An operator in front of another relay (`target`) who forwards everything and signs the feeds
    with a relay key of their own, as a hostile one would: anything the real relay signed they can
    change and sign again. It is a real HTTP server on its own port.

    * `rewrite[feed_key] = fn(items) -> items` changes (or invents) what a feed carries.
    * `rewrite_card = fn(card) -> card` changes the card `/agents/{id}/card` gives (its signature is left as it was).
    * `swap_signing_keys` puts a key of the rogue's own in place of every agent's public_key_hex that
      `/agents/{id}` and its card and encryption-key routes give out; `swap_encryption_keys` does the same
      for encryption keys. With both on, the encryption-key answer is consistent: the rogue's encryption key,
      bound to the rogue's signing key by a signature the rogue made - what a full impersonation looks like.
    """

    def __init__(self, target: str):
        self.target = target.rstrip("/")
        self.key = ed25519.Ed25519PrivateKey.generate()
        self.public_key = self.key.public_key().public_bytes_raw().hex()
        self._agent_priv = ed25519.Ed25519PrivateKey.generate()
        self.agent_key = self._agent_priv.public_key().public_bytes_raw().hex()
        self.encryption_key = os.urandom(32).hex()
        self.rewrite: dict[str, Callable[[list[dict]], list[dict]]] = {}
        self.rewrite_card: Callable[[dict], dict] | None = None
        self.swap_signing_keys = False
        self.swap_encryption_keys = False
        self.served: list[str] = []
        rogue = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):  # quiet
                pass

            def _handle(self):
                n = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(n) if n else b""
                status, ctype, data = rogue.forward(self.command, self.path, dict(self.headers), body)
                self.send_response(status)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            do_GET = do_POST = do_PUT = do_DELETE = do_PATCH = _handle

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def start(self) -> RogueRelay:
        self.thread.start()
        return self

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    def reset(self) -> None:
        """Honest again: it still forwards and still signs with its own key."""
        self.rewrite.clear()
        self.rewrite_card = None
        self.swap_signing_keys = self.swap_encryption_keys = False

    def forward(self, method: str, path: str, headers: dict[str, str], body: bytes) -> tuple[int, str, bytes]:
        self.served.append(f"{method} {path}")
        skip = {"host", "content-length", "connection", "accept-encoding", "transfer-encoding"}
        out_headers = {k: v for k, v in headers.items() if k.lower() not in skip}
        try:
            r = httpx.request(method, self.target + path, content=body or None, headers=out_headers, timeout=30)
        except httpx.HTTPError as e:
            return 502, "text/plain", str(e).encode()
        ctype = r.headers.get("content-type", "application/json")
        route = path.split("?", 1)[0]
        if r.status_code != 200 or "json" not in ctype:
            return r.status_code, ctype, r.content
        if route in FEEDS:
            data = r.json()
            key = FEEDS[route]
            items = self.rewrite[key](data[key]) if key in self.rewrite else data[key]
            data[key] = items
            data["origin_public_key_hex"] = self.public_key
            data["signature"] = self.key.sign(federation_signable_bytes(data["origin_relay"], key, items)).hex()
            return 200, ctype, json.dumps(data).encode()
        if method == "GET" and self.rewrite_card and route.endswith("/card") and route.startswith("/agents/"):
            data = r.json()
            data["card"] = self.rewrite_card(data["card"])
            return 200, ctype, json.dumps(data).encode()
        if method == "GET" and _AGENT_PATH.match(route) and (self.swap_signing_keys or self.swap_encryption_keys):
            data = self._swap(r.json())
            if route.endswith("/encryption-key") and self.swap_signing_keys and self.swap_encryption_keys:
                agent = unquote(route.split("/")[2]).partition("@")[0]
                data["signature"] = sign_payload(self._agent_priv, signed_encryption_key_payload(
                    agent, self.encryption_key))
            return 200, ctype, json.dumps(data).encode()
        return r.status_code, ctype, r.content

    def _swap(self, node: Any) -> Any:
        if isinstance(node, dict):
            out = {}
            for k, v in node.items():
                if k == "public_key_hex" and self.swap_signing_keys:
                    v = self.agent_key
                elif k == "encryption_public_key" and self.swap_encryption_keys:
                    v = self.encryption_key
                out[k] = self._swap(v)
            return out
        if isinstance(node, list):
            return [self._swap(x) for x in node]
        return node


# --- the whole set --------------------------------------------------------------

class Fleet:
    """Relays A and B (linked both ways), C (linked to nobody), a rogue operator in front of B, and a
    fourth relay `victim` that is linked to the rogue — the only way to watch a relay pull from a hostile
    peer. Agents get folders under `root`."""

    def __init__(self, root: Path):
        self.root = root
        self.a, self.b, self.c, self.victim = (Relay(n, root) for n in ("a", "b", "c", "victim"))
        self.relays = [self.a, self.b, self.c, self.victim]
        self.rogue = RogueRelay(self.b.url)
        self.agents: list[Agent] = []

    def up(self) -> Fleet:
        self.a.links, self.b.links = [self.b.url], [self.a.url]
        self.rogue.start()
        self.victim.links = [self.rogue.url]
        for r in self.relays:
            r.start()
        for r in self.relays:
            r.wait_healthy()
        for r in self.relays:
            r.operator = self.agent(OPERATOR, r)
            r.operator.register()
        return self

    def agent(self, name: str, relay: Relay | str, *, root: Path | None = None, **env: str) -> Agent:
        """An agent process with a key folder of its own (`root`/keys_NAME; default: under the fleet's folder)."""
        home = relay.url if isinstance(relay, Relay) else relay
        a = Agent(name, home, root or self.root, **env).start()
        self.agents.append(a)
        return a

    def registered(self, name: str, relay: Relay | str, **env: str) -> Agent:
        a = self.agent(name, relay, **env)
        a.register()
        return a

    def down(self) -> None:
        for a in self.agents:
            a.stop()
        self.rogue.stop()
        for r in self.relays:
            r.stop()

    def logs(self) -> str:
        parts = [f"--- relay {r.name} ({r.url}) ---\n{tail(r.log)}" for r in self.relays]
        parts += [f"--- agent {a.name} ---\n{tail(a.stderr, 12)}" for a in self.agents]
        return "\n".join(parts)


# --- a game to check ------------------------------------------------------------

def play_fools_mate(white: Agent, black: Agent, relay_of: dict[str, str] | None = None) -> str:
    """`white` opens a free chess game, `black` joins and they play four moves to checkmate (black wins).
    `relay_of` names, per agent, the relay it acts on when that is not its home. Returns the session id."""
    on = relay_of or {}

    def kw(agent: Agent) -> dict[str, str]:
        return {"relay": on[agent.name]} if agent.name in on else {}

    made = white.tool("create_session", module_type="emporia:chess:v1", agent_id=white.name, **kw(white))
    assert made["success"], made
    sid = made["session_id"]
    joined = black.tool("join_session", session_id=sid, agent_id=black.name, **kw(black))
    assert joined["success"], joined
    for who, uci in ((white, "f2f3"), (black, "e7e5"), (white, "g2g4"), (black, "d8h4")):
        r = who.tool("submit_action", session_id=sid, action_type="move", payload={"uci": uci},
                     rationale=f"playing {uci} for the sake of the test", agent_id=who.name, **kw(who))
        assert r["success"], r
    return sid
