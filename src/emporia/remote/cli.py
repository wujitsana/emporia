"""`emporia agent serve-remote NAME` and `emporia agent connector list|pending|approve|deny|revoke|log|allow-redirect NAME`.

A person's commands, run in their own terminal (the Hermes key guard refuses them to an agent's tools). NAME is an
agent made with `emporia agent init` (its key directory defaults to `~/.config/emporia/agents/NAME`), a key directory
path, or a Hermes profile with Emporia set up. The connector's state lives in `<key dir>/remote/`.

`serve-remote` runs `emporia.remote.server` as a child with the agent's own settings (the way its MCP server gets
them) and the remote settings below. `approve` is the only way a client becomes a grant: it shows what is being
approved in plain words and asks y/N in this terminal.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from emporia import features
from emporia import remote as R
from emporia.remote import server as S
from emporia.remote import surface
from emporia.remote.audit import AuditLog
from emporia.remote.oauth import Authority, OAuthError
from emporia.remote.store import Store

SERVER_FILE = "server.json"
SCOPE_EFFECT = {
    R.SCOPE_READ: "read what is public: the Feed, rooms and topics, games, standing, quests (no private messages, ever)",
    R.SCOPE_TALK: "post, react, follow and write in rooms AS THIS AGENT, in public (5 writes per 10 minutes at most)",
}


class AgentNotFound(Exception):
    pass


def find_agent(name: str) -> tuple[Path, dict[str, str], str]:
    """(key directory, the environment its server runs with, how it was found) for NAME."""
    from emporia import agent_init, host_configs
    path = Path(name).expanduser()
    if os.sep in name or name.startswith("."):
        if (path / agent_init.ENV_FILE).is_file():
            return path.resolve(), host_configs.settings_for("generic", path), "key directory"
        raise AgentNotFound(f"{name} has no {agent_init.ENV_FILE}: run `emporia agent init` first")
    default = agent_init.default_dir(name)
    if (default / agent_init.ENV_FILE).is_file():
        return default, host_configs.settings_for("generic", default), "agent"
    try:
        from emporia import cli
        for profile, _entry in cli._agent_profiles(cli.profiles_root()):
            if profile.name == name:
                env = cli._agent_child_env(profile, {})
                keys = env.get("EMPORIA_KEYS_DIR") or str(Path.home() / ".hermes" / "keys")
                return Path(keys).expanduser(), {k: v for k, v in env.items() if k.startswith(("EMPORIA_", "OP_", "BW_",
                       "VAULT_", "DOPPLER_", "INFISICAL_", "AWS_", "AZURE_"))}, "Hermes profile"
    except Exception:  # noqa: BLE001 - not a Hermes machine: fall through to the plain message
        pass
    raise AgentNotFound(f"no agent {name!r} here ({default} has no {agent_init.ENV_FILE}); make it with "
                        f"`emporia agent init --agent {name} --relay URL`, or pass its key directory")


def _folder(key_dir: Path) -> Path:
    return key_dir / "remote"


def _authority(key_dir: Path, agent: str = "") -> Authority:
    """An Authority for the person's commands. It needs no public URL for list/approve/revoke; the running
    server's own (kept in server.json) is used when there is one."""
    folder = _folder(key_dir)
    store, audit = Store(folder), AuditLog(folder)
    url, ceiling = "http://127.0.0.1", R.SCOPES
    try:
        info = json.loads((folder / SERVER_FILE).read_text())
        url, ceiling = info.get("public_url") or url, tuple(info.get("ceiling") or ceiling)
    except (OSError, ValueError):
        pass
    return Authority(store, audit, url, ceiling, agent)


def ago(t: float | None, now: float | None = None) -> str:
    if not t:
        return "never"
    secs = int((now or time.time()) - t)
    for unit, n in (("d", 86400), ("h", 3600), ("min", 60)):
        if secs >= n:
            return f"{secs // n}{unit} ago"
    return "just now"


# ── serve-remote ──────────────────────────────────────────────────────────────────────

def serve_remote(args: argparse.Namespace) -> int:
    try:
        key_dir, env, how = find_agent(args.url_or_name)
    except AgentNotFound as e:
        print(f"Error: {e}", file=sys.stderr)
        return 2
    from emporia import agent_init
    held = agent_init.settings_of(key_dir)           # the remote settings sit in emporia.env, beside the keys
    name = env.get("EMPORIA_AGENT_ID", "")
    public_url = args.public_url or held.get("EMPORIA_REMOTE_PUBLIC_URL", "")
    bind = args.bind or held.get("EMPORIA_REMOTE_BIND") or R.DEFAULT_BIND
    scopes = args.scopes or held.get("EMPORIA_REMOTE_SCOPES") or "read"
    problems = S.startup_problems(public_url=public_url, bind=bind, behind_proxy=args.behind_proxy, agent_id=name,
                                  environ=os.environ)
    try:
        S.parse_scopes(scopes)
    except ValueError as e:
        problems.append(str(e))
    if problems:
        print("serve-remote will not start:", file=sys.stderr)
        for why in problems:
            print(f"  - {why}", file=sys.stderr)
        print("\nExample: emporia agent serve-remote NAME --public-url https://emporia-me.example.net", file=sys.stderr)
        return 2
    child = {"PATH": os.defpath, "HOME": str(Path.home()), **env, "EMPORIA_NO_DOTENV": "1",
             "EMPORIA_KEYS_DIR": str(key_dir)}
    import emporia
    root = str(Path(emporia.__file__).resolve().parent.parent)     # the code that runs this command runs the server
    cmd = [sys.executable, "-c", "import sys; sys.path.insert(0, sys.argv[1]); "
           "from emporia.remote.server import main; raise SystemExit(main(sys.argv[2:]))", root,
           "--public-url", public_url, "--scopes", scopes, "--bind", bind] + (
               ["--behind-proxy"] if args.behind_proxy else [])
    folder = _folder(key_dir)
    folder.mkdir(parents=True, exist_ok=True)
    os.chmod(folder, 0o700)
    from emporia.remote.oauth import parse_public_url
    origin, _ = parse_public_url(public_url)
    ceiling, _note = surface.ceiling_for(S.parse_scopes(scopes), features.read_only(env.get(features.SETTING, "")))
    info = folder / SERVER_FILE
    fd = os.open(info, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump({"public_url": origin, "ceiling": list(ceiling), "agent_id": name, "started": time.time()}, f)
    print(f"Starting the remote connector for {name} ({how}); state in {folder}", file=sys.stderr)
    try:
        return subprocess.run(cmd, env=child).returncode
    except KeyboardInterrupt:
        return 0


# ── connector ─────────────────────────────────────────────────────────────────────────

def connector(args: argparse.Namespace) -> int:
    action, name, extra = args.url_or_name, args.name or "", getattr(args, "extra", "") or ""
    if action not in ("list", "pending", "approve", "deny", "revoke", "log", "allow-redirect") or not name:
        print("usage: emporia agent connector list|pending NAME | deny NAME CODE | approve NAME CODE [--scopes read,talk] "
              "[--connector-name TEXT] | revoke NAME GRANT_ID|--all | log NAME [--tail] | allow-redirect NAME URL",
              file=sys.stderr)
        return 2
    try:
        key_dir, env, _how = find_agent(name)
    except AgentNotFound as e:
        print(f"Error: {e}", file=sys.stderr)
        return 2
    auth = _authority(key_dir, env.get("EMPORIA_AGENT_ID", name))
    if action == "list":
        return _list(auth)
    if action == "pending":
        return _pending(auth)
    if action == "approve":
        return _approve(auth, extra, args, env.get("EMPORIA_AGENT_ID", name))
    if action == "deny":
        if not extra:
            print("deny needs the confirmation code (see `connector pending`)", file=sys.stderr)
            return 2
        if auth.deny(extra):
            print("Denied. The host's page ends there.")
            return 0
        print("No such request is waiting (it may have expired).", file=sys.stderr)
        return 1
    if action == "revoke":
        return _revoke(auth, extra, args.all)
    if action == "log":
        return _log(auth.audit, args.tail)
    return _allow(auth, extra)


def _list(auth: Authority) -> int:
    rows = auth.listing()
    now = time.time()
    print("Clients (registered, not yet approved ones expire after a day):")
    for c in rows["clients"]:
        print(f"  {c['client_id'][:8]}  {c['name']!r}  registered {ago(c['created_at'], now)}")
    if not rows["clients"]:
        print("  none")
    print("\nGrants (what is approved to act as this agent):")
    for g in rows["grants"]:
        state = (f"revoked ({g['revoke_reason']})" if g["revoked_at"] else
                 "expired" if g["expires_at"] <= now else "live")
        scopes = ", ".join(s.split(":")[-1] for s in json.loads(g["scopes"]))
        print(f"  {g['grant_id']}  {g['name']!r}  scopes: {scopes}  created {ago(g['created_at'], now)}  "
              f"last used {ago(g['last_used'], now)}  calls {g['calls']}  {state}")
    if not rows["grants"]:
        print("  none")
    return 0


def _pending(auth: Authority) -> int:
    rows = auth.pending_rows()
    if not rows:
        print("Nothing is waiting for you.")
        return 0
    now = time.time()
    for r in rows:
        client = auth.client(r["client_id"])
        scopes = ", ".join(s.split(":")[-1] for s in json.loads(r["scopes"]))
        print(f"  {r['code']}  {client['name'] if client else '?'!r} (claims to be)  asks for: {scopes}  "
              f"{ago(r['created_at'], now)}, waits {max(0, int(r['expires_at'] - now))}s more")
    print("\nApprove one with: emporia agent connector approve NAME CODE")
    return 0


def _approve(auth: Authority, code: str, args: argparse.Namespace, agent: str) -> int:
    if not code:
        print("approve needs the confirmation code shown on the consent page (see `connector pending`)", file=sys.stderr)
        return 2
    row = auth.pending_by_code(code)
    if row is None:
        print("No such request is waiting (it may have expired). `emporia agent connector pending` lists them.",
              file=sys.stderr)
        return 1
    client = auth.client(row["client_id"])
    asked = json.loads(row["scopes"])
    try:
        chosen = list(S.parse_scopes(args.scopes)) if args.scopes else asked
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 2
    chosen = [s for s in chosen if s in asked]
    if not chosen:
        print(f"None of those scopes was asked for. It asked for: {', '.join(asked)}", file=sys.stderr)
        return 2
    label = args.connector_name or (client["name"] if client else "connector")
    print(f"\nApprove this connector to act AS the agent {agent}?\n")
    print(f"  Client: {client['name'] if client else '?'!r}  (it claims this name; unverified)")
    print(f"  Returns to: {client['redirect_uris'] if client else '?'}")
    print(f"  Confirmation code: {row['code']}  (it must match the one on the consent page)")
    print(f"  Connector name in the log: {label}")
    print("  Scopes:")
    for s in chosen:
        print(f"    {s}: {SCOPE_EFFECT.get(s, s)}")
    if len(chosen) < len(asked):
        print(f"  (narrower than asked: it asked for {', '.join(asked)})")
    print("  It gets no money, wallet, key, settings or moderation tools, and no private message text.")
    print("  Lasts until you revoke it; you approve again every 90 days. Revoke: emporia agent connector revoke "
          f"{args.name} GRANT_ID\n")
    if not args.yes:
        if not (sys.stdin.isatty() and sys.stdout.isatty()):
            print("approve asks y/N in a terminal; there is none here (or pass --yes to say yes yourself).",
                  file=sys.stderr)
            return 2
        if input("Approve? [y/N] ").strip().lower() not in ("y", "yes"):
            auth.deny(code)
            print("Denied.")
            return 1
    try:
        out = auth.approve(code, chosen, args.connector_name or "")
    except OAuthError as e:
        print(f"Could not approve: {e.description}", file=sys.stderr)
        return 1
    print(f"Approved: grant {out['grant_id']} ({', '.join(out['scopes'])}). The host's page continues by itself.")
    return 0


def _revoke(auth: Authority, grant: str, everything: bool) -> int:
    if everything:
        n = auth.revoke_all()
        print(f"Revoked {n} grant{'s' if n != 1 else ''}. Their tokens stop working on the next request.")
        return 0
    if not grant:
        print("revoke needs a grant id (see `connector list`) or --all", file=sys.stderr)
        return 2
    if auth.revoke_grant(grant):
        print(f"Revoked {grant}. Its tokens stop working on the next request.")
        return 0
    print(f"No live grant {grant!r}.", file=sys.stderr)
    return 1


def _fmt(row: dict[str, Any]) -> str:
    t = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(row.get("at", 0)))
    rest = " ".join(f"{k}={json.dumps(v) if not isinstance(v, str) else v}" for k, v in row.items()
                    if k not in ("at", "event"))
    return f"{t}Z  {row.get('event')}  {rest}"


def _log(audit: AuditLog, tail: bool) -> int:
    ok, note = audit.verify()
    rows = audit.rows(0 if tail else 60)
    for r in rows:
        print(_fmt(r))
    print(f"\nchain: {'OK' if ok else 'BROKEN'}: {note}", file=sys.stderr)
    if tail:
        seen = len(audit.rows())
        try:
            while True:
                time.sleep(1)
                all_rows = audit.rows()
                for r in all_rows[seen:]:
                    print(_fmt(r), flush=True)
                seen = len(all_rows)
        except KeyboardInterrupt:
            return 0
    return 0 if ok else 1


def _allow(auth: Authority, url: str) -> int:
    from urllib.parse import urlsplit
    try:
        u = urlsplit(url)
        bad = u.scheme != "https" or not u.hostname or u.fragment or u.username or u.password or len(url) > 400
    except ValueError:
        bad = True
    if bad:
        print("allow-redirect takes one https:// callback URL, with no fragment and no credentials.", file=sys.stderr)
        return 2
    auth.store.allow_redirect(url)
    auth.audit.write("allow_redirect", detail=url[:200])
    print(f"Allowed: clients may now register {url} as a redirect URI.")
    return 0
