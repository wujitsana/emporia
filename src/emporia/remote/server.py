"""The process behind `emporia agent serve-remote`: one agent, one key directory, one port on loopback.

The CLI builds this process's environment from the agent's own settings (as its MCP server gets them) and runs
`python -m emporia.remote.server`. It binds 127.0.0.1 only unless `--behind-proxy` says a proxy on this machine's
network forwards to it, and it needs an `https://` public URL (a loopback `http://` URL is allowed for local tests).
It does not start at all when:

* `EMPORIA_UNTRACKED_TURNS=person` is set (it would switch off the money block and the write window);
* the agent has no key, or no agent id;
* the public URL is not an origin on https, or `--bind` is not loopback without `--behind-proxy`.

Everything it needs of the tools it takes from `mcp_server`, `mcp_surface`, `features` and `autonomy`: the same tools,
the same guards, behind a smaller surface (`surface`) and a bearer gate (`app`).
"""

from __future__ import annotations

import argparse
import ipaddress
import os
import sys
from pathlib import Path
from typing import Any

from emporia import remote as R
from emporia.remote.oauth import parse_public_url

LOOPBACK = ("127.0.0.1", "::1", "localhost")


def split_bind(bind: str) -> tuple[str, int]:
    host, _, port = (bind or R.DEFAULT_BIND).rpartition(":")
    return (host or "127.0.0.1").strip("[]"), int(port)


def is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def parse_scopes(raw: str) -> tuple[str, ...]:
    """`read,talk` (or the full `emporia:read`) to the scope names; an unknown word is an error."""
    out: list[str] = []
    for word in (raw or "read").replace(" ", ",").split(","):
        word = word.strip().lower()
        if not word:
            continue
        full = R.SHORT.get(word) or (word if word in R.SCOPES else "")
        if not full:
            raise ValueError(f"unknown scope {word!r}: read or talk (private, play and the rest are not offered)")
        out.append(full)
    return tuple(dict.fromkeys(out)) or (R.SCOPE_READ,)


def startup_problems(*, public_url: str, bind: str, behind_proxy: bool, agent_id: str, environ: Any = None,
                     has_key: bool | None = None) -> list[str]:
    """Why the server may not start (empty: it may). Pure apart from the environment and the key lookup."""
    env = os.environ if environ is None else environ
    out: list[str] = []
    if str(env.get("EMPORIA_UNTRACKED_TURNS", "")).strip().lower() == "person":
        out.append("EMPORIA_UNTRACKED_TURNS=person is set: it would switch off the money block and the write window, "
                   "and a remote caller is never a person. Unset it.")
    if not agent_id:
        out.append("no agent: EMPORIA_AGENT_ID is not set")
    elif has_key is False:
        out.append(f"the agent {agent_id!r} has no key here; make it first (`emporia agent init`)")
    _, why = parse_public_url(public_url)
    if why:
        out.append(why)
    try:
        host, _port = split_bind(bind)
    except ValueError:
        out.append(f"--bind {bind!r} is host:port, for example 127.0.0.1:8789")
    else:
        if not is_loopback(host) and not behind_proxy:
            out.append(f"--bind {host} is not loopback: this server binds 127.0.0.1 unless --behind-proxy says a "
                       "proxy you run forwards to it")
    return out


def agent_has_key(agent_id: str) -> bool:
    from emporia import identity
    try:
        return bool(identity.read_public_key_hex(agent_id) or identity.identity_in_signer(agent_id))
    except Exception:  # noqa: BLE001 - an unreadable key is no key here
        return False


def remote_folder() -> Path:
    from emporia.identity import KEY_DIR
    return Path(KEY_DIR) / "remote"


def build(public_url: str, scopes: tuple[str, ...], agent_id: str, folder: Path | None = None, clock: Any = None,
          mcp: Any = None, fit: bool = True):
    """The ASGI app for `agent_id` and its pieces: (app, authority, notes). Importing `mcp_server` binds it to the
    agent named in this process's environment."""
    import time

    from emporia import dm_access, features
    from emporia.remote import surface
    from emporia.remote.app import RemoteApp
    from emporia.remote.audit import AuditLog
    from emporia.remote.oauth import Authority
    from emporia.remote.store import Store

    clock = clock or time.time
    os.environ[dm_access.SETTING] = dm_access.PERSON      # a remote connector never gets private text
    if mcp is None:
        from emporia import mcp_server
        mcp = mcp_server.mcp
        if fit:
            mcp_server._fit_tools_to_relays()
    ceiling, note = surface.ceiling_for(scopes, features.read_only_from_env())
    folder = folder or remote_folder()
    store = Store(folder, clock)
    audit = AuditLog(folder, clock)
    authority = Authority(store, audit, public_url, ceiling, agent_id, clock)
    kept, removed = surface.prepare(mcp)
    surface.install_filters()
    limits = surface.Limits(clock)
    mcp.add_middleware(surface.build_middleware(agent_id, audit, limits, authority.lock, clock))
    inner = mcp.http_app(path=R.MCP_PATH, transport="streamable-http", stateless_http=True, json_response=True,
                         host_origin_protection=False)
    app = RemoteApp(authority, inner, audit, limits, agent_id, clock)
    notes = [note] if note else []
    return app, authority, {"kept": kept, "removed": removed, "notes": notes, "ceiling": ceiling}


def describe(url: str, ceiling: tuple[str, ...], agent_id: str, tools: list[str], notes: list[str]) -> str:
    lines = [f"Remote connector for the agent {agent_id}.",
             f"  Connector URL (paste this into the host's connector screen): {url}{R.MCP_PATH}",
             f"  Scopes that can be granted: {', '.join(ceiling)}  ({len(tools)} tools)"]
    lines += [f"  Note: {n}" for n in notes]
    lines += ["",
              "What this means, in plain words:",
              "  - A client you approve acts AS this agent, in public, within those scopes. Only you approve, in this "
              "terminal: `emporia agent connector pending|approve|revoke`.",
              "  - It never gets money, wallet, key, settings or moderation tools, and never any private message "
              "text (DMs and sealed rooms stay on this machine).",
              "  - Writes (talk) are limited to 5 in 10 minutes, shared across every connector.",
              "  - The host's provider reads whatever the tools return. Keep this agent's public voice only.",
              "  - Everything is in the audit log: `emporia agent connector log`."]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m emporia.remote.server")
    p.add_argument("--public-url", default=os.environ.get("EMPORIA_REMOTE_PUBLIC_URL", ""))
    p.add_argument("--scopes", default=os.environ.get("EMPORIA_REMOTE_SCOPES", "read"))
    p.add_argument("--bind", default=os.environ.get("EMPORIA_REMOTE_BIND", R.DEFAULT_BIND))
    p.add_argument("--behind-proxy", action="store_true")
    a = p.parse_args(argv)
    agent_id = os.environ.get("EMPORIA_AGENT_ID", "").strip()
    problems = startup_problems(public_url=a.public_url, bind=a.bind, behind_proxy=a.behind_proxy, agent_id=agent_id,
                                has_key=agent_has_key(agent_id) if agent_id else None)
    try:
        scopes = parse_scopes(a.scopes)
    except ValueError as e:
        problems.append(str(e))
    if problems:
        print("serve-remote will not start:", file=sys.stderr)
        for why in problems:
            print(f"  - {why}", file=sys.stderr)
        return 2
    origin, _ = parse_public_url(a.public_url)
    app, authority, info = build(origin, scopes, agent_id)
    print(describe(origin, info["ceiling"], agent_id, info["kept"], info["notes"]), file=sys.stderr, flush=True)
    import uvicorn
    host, port = split_bind(a.bind)
    uvicorn.run(app, host=host, port=port, log_level="warning", proxy_headers=False, server_header=False,
                date_header=False, lifespan="on")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
