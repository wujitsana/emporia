"""Nobody can approve a mainnet payment in a turn another agent started (catalog rule 12).

With mainnet unlocked (`EMPORIA_X402_MAINNET` in this profile, or the signer's policy when the wallet is behind
a signer), every payment goes to Hermes's approval prompt as an MCP elicitation carrying the exact terms: the
wallet's own per-payment approval (threshold 0 on mainnet; the runtime refuses when it cannot ask). In a turn a
relay event or the rounds job started there is nobody to answer, and the elicitation would wait out its whole
timeout. So in such a turn this `pre_tool_call` hook refuses the payment tools at once. In a person's own turn it
does nothing. On a testnet nothing changes.

Self-contained: imports nothing from Emporia (see plugin.yaml).
"""

from __future__ import annotations

import http.client
import json
import os
import socket
import time
from pathlib import Path
from typing import Any

#: Hermes names an MCP server's tools mcp__<server>__<tool> (as relay_turn.EMPORIA_TOOL_PREFIXES).
PREFIX = "mcp__emporia__"
#: The tools that can move money, from the runtime's tool surface: join_room and subscribe_agora_topic
#: (pay_with_x402 / pay_with_tempo for an entry fee), fund_session (an escrow hold) and reclaim_escrow
#: (takes a hold back with an on-chain transaction from the agent's wallet). tests/test_money_guard.py
#: checks this list against the runtime's source: a tool that reaches the wallet must be on it.
MONEY_TOOLS = frozenset({"join_room", "subscribe_agora_topic", "fund_session", "reclaim_escrow"})
try:                                               # the turn's origin is relay_turn's to know: one state, not two
    from . import relay_turn
except ImportError:                                # loaded as a bare file (tests, tools)
    import importlib.util as _ilu
    from pathlib import Path as _Path
    _spec = _ilu.spec_from_file_location("emporia_relay_turn", _Path(__file__).with_name("relay_turn.py"))
    relay_turn = _ilu.module_from_spec(_spec)
    _spec.loader.exec_module(relay_turn)

STALE = ("Emporia: this profile's runtime is not the one this plugin release pins, and it may not ask before it "
         "pays. Not paid: run `hermes emporia update` first.")
UNATTENDED = ("Emporia: a payment on mainnet needs your approval, and a turn another agent started cannot ask you. "
              "Not paid.")


def _setting(name: str) -> str:
    try:  # the gateway may serve several profiles; read this profile's value
        from gateway.platforms._shared import get_scoped_secret
        value = get_scoped_secret(name, "")
    except Exception:
        value = None
    return str(value if value is not None else os.environ.get(name, "")).strip()


class _UnixConnection(http.client.HTTPConnection):
    def __init__(self, path: str):
        super().__init__("signer", timeout=5)
        self._path = path

    def connect(self) -> None:
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(5)
        self.sock.connect(self._path)


_signer_seen: dict[str, tuple[float, bool]] = {}


def _signer_says_mainnet() -> bool:
    """With the wallet behind a signer (`emporia signer add` takes EMPORIA_X402_MAINNET out of the profile; the
    unlock lives in the signer's policy), ask the signer: its /v1/wallet reports `mainnet`. The signer stays the
    authority and this only ever adds a prompt. Not reachable, or not understood: ask (prompting is the safe side).
    An older signer's answer has no `mainnet`: it counts as mainnet (ask). Remembered for 30 seconds."""
    url = _setting("EMPORIA_SIGNER_URL")
    if not url:
        return False
    agent = _setting("EMPORIA_AGENT_ID")
    hit = _signer_seen.get(f"{url}|{agent}")
    if hit and time.time() - hit[0] < 30:
        return hit[1]
    headers = {"authorization": f"Bearer {_setting('EMPORIA_SIGNER_TOKEN')}", "x-emporia-agent": agent}
    try:
        if url.startswith("unix://"):
            conn = _UnixConnection(url.removeprefix("unix://"))
        else:
            from urllib.parse import urlsplit
            u = urlsplit(url)
            cls = http.client.HTTPSConnection if u.scheme == "https" else http.client.HTTPConnection
            conn = cls(u.netloc, timeout=5)
        try:
            conn.request("GET", "/v1/wallet", headers=headers)
            resp = conn.getresponse()
            data = json.loads(resp.read() or b"{}")
        finally:
            conn.close()
        answer = bool(data.get("mainnet", True)) if resp.status == 200 and isinstance(data, dict) else True
    except Exception:
        answer = True
    _signer_seen[f"{url}|{agent}"] = (time.time(), answer)
    return answer


def mainnet_on() -> bool:
    """Real money is unlocked: EMPORIA_X402_MAINNET in this profile, or the signer's policy."""
    return _setting("EMPORIA_X402_MAINNET").lower() in ("1", "true", "yes", "on") or _signer_says_mainnet()


def _home():
    try:
        from hermes_constants import get_hermes_home
        return Path(get_hermes_home())
    except Exception:
        return Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")


def _is_emporia(path: Path) -> bool:
    return (path / "src" / "emporia" / "cli.py").is_file() and (path / "pyproject.toml").is_file()


def catalog_runtime() -> Path | None:
    """The runtime this profile runs, resolved as `setup_cli.runtime()` does, when it is a catalog runtime (one
    `hermes emporia setup` installed under some profile's plugin-data, which follows this plugin's pin); None for
    a development checkout (never subject to the pin) or no runtime. A checkout wins; else the one the profile's
    EMPORIA_PYTHON belongs to (a profile made from another shares that profile's runtime); else its own."""
    here = Path(__file__).resolve().parent
    if _is_emporia(here.parents[1]):
        return None
    found = None
    py = _setting("EMPORIA_PYTHON")
    if py:
        root = Path(py).parent.parent.parent                    # <runtime>/.venv/bin/python
        if _is_emporia(root):
            found = root
    if found is None:
        own = _home() / "plugin-data" / "emporia" / "runtime"
        found = own if _is_emporia(own) else None
    if found is None or not (found.parent.name == "emporia" and found.parent.parent.name == "plugin-data"):
        return None
    return found


def _git_head(repo: Path) -> str:
    """The commit a clone is at, from its .git/HEAD (and the ref file or packed-refs): no subprocess. A `.git`
    that is a file (a worktree) is not read: "" ."""
    git = repo / ".git"
    if not git.is_dir():
        return ""
    try:
        head = (git / "HEAD").read_text().strip()
        if not head.startswith("ref:"):
            return head
        ref = head.split(None, 1)[1]
        try:
            return (git / ref).read_text().strip()
        except OSError:
            for line in (git / "packed-refs").read_text().splitlines():
                if line.endswith(" " + ref):
                    return line.split()[0]
    except OSError:
        pass
    return ""


def runtime_is_stale() -> bool:
    """The catalog runtime this profile runs (its own, or the one it shares) is at a commit other than the one the
    plugin pins: after a plugin update, until `hermes emporia update`. It may be from before this release, which does
    not ask before a mainnet payment. No pin, a development checkout, or a runtime whose commit cannot be read: not
    stale."""
    try:
        pinned = json.loads((Path(__file__).with_name("emporia_source.json")).read_text()).get("sha", "")
    except (OSError, ValueError):
        return False
    runtime = catalog_runtime()
    if not pinned or runtime is None:
        return False
    head = _git_head(runtime)
    return bool(head) and head != pinned


def pre_tool_call(tool_name: str = "", session_id: str = "", task_id: str = "", **_: Any) -> dict | None:
    """On mainnet, refuse an Emporia payment tool in an unattended turn, and in any turn when the runtime is
    not the pinned one (it may not ask)."""
    if not isinstance(tool_name, str) or not tool_name.startswith(PREFIX) or tool_name[len(PREFIX):] not in MONEY_TOOLS:
        return None
    unattended = bool(relay_turn.autonomous_kind(session_id, task_id))
    stale = runtime_is_stale()
    if not (unattended or stale) or not mainnet_on():
        return None
    return {"action": "block", "message": STALE if stale else UNATTENDED}
