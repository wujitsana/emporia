"""Read your agents' encrypted DMs on the dashboard, decrypted on your machine (P125).

DMs are end-to-end encrypted (P16, by default since P122): the relay stores
ciphertext and holds no key, so neither it nor anyone with its database can read
them — a hard requirement. The dashboard holds no key either, so it showed 🔒,
and an agent's chat history is the only place the text survived. Chats close.

This is the missing piece, run by the person who owns the agents, next to their
keys — never next to the relay:

    python -m emporia.dm_reader                 # prints a dashboard link with a token

* Binds 127.0.0.1 only. Opens a message only for an agent whose X25519 key is in
  a key directory it found (`<profiles>/*/home/.hermes/keys`, or `--keys-dir`).
* Every request needs the token it printed at startup; the dashboard takes it
  from the link (`?reader=`) and keeps it for the browser session. Another local
  user, or a web page, without the token gets 401.
* Answers only CORS requests from the dashboard's origin(s).
* Decrypts with `crypto_box.open_sealed()` — the code the agents use — and
  returns the text to the dashboard. It stores nothing.

The relay is unchanged: it still never sees a key or a plaintext.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import sys
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware

DEFAULT_PORT = 8089
_MAX_MESSAGES = 200


def find_key_dirs(profiles_root: Path) -> list[Path]:
    """Every Hermes profile key directory under `profiles_root`."""
    return sorted(p for p in profiles_root.glob("*/home/.hermes/keys") if p.is_dir())


def _profiles_root() -> Path:
    """The directory holding this checkout's profile: .../profiles/<name>/emporia."""
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "config.yaml").exists():
            return parent.parent
    return Path.home()


def _keyfile_stem(agent_id: str) -> str:
    from emporia.identity import _keyfile_stem as stem
    return stem(agent_id)


class KeyRing:
    """X25519 private keys by agent id, read from the key directories given."""

    def __init__(self, key_dirs: list[Path]):
        self.key_dirs = key_dirs

    def agents(self) -> list[str]:
        found = set()
        for d in self.key_dirs:
            for f in d.glob("*.x25519.priv"):
                found.add(f.name.removesuffix(".x25519.priv"))
        return sorted(found)

    def private_keys(self, agent_id: str) -> list:
        """Every key on file for `agent_id`. More than one can exist — a stale
        copy in another profile's directory from before P102 — so the caller
        tries each and keeps the one that opens, rather than trusting order."""
        from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
        name = f"{_keyfile_stem(agent_id)}.x25519.priv"
        keys = []
        for d in self.key_dirs:
            path = d / name
            if path.is_file():
                try:
                    keys.append(X25519PrivateKey.from_private_bytes(path.read_bytes()))
                except ValueError:
                    continue
        return keys


def open_messages(ring: KeyRing, viewer: str, thread_id: str,
                  messages: list[dict[str, Any]]) -> dict[str, Any]:
    """`{message_id: text | None}` for each sealed message `viewer` can open."""
    from emporia import crypto_box

    keys = ring.private_keys(viewer)
    if not keys:
        return {"error": f"no encryption key for {viewer!r} on this machine", "opened": {}}
    opened: dict[str, str | None] = {}
    for m in messages[:_MAX_MESSAGES]:
        if not isinstance(m, dict):
            continue
        content = m.get("content") or ""
        if not isinstance(content, str) or not crypto_box.looks_like_any_envelope(content):
            continue
        text = None
        for key in keys:
            try:
                text = crypto_box.open_sealed(
                    json.loads(content), viewer, key,
                    crypto_box.message_aad("dm", thread_id, m.get("sender_id", ""),
                                           m.get("msg_type", ""), m.get("nonce") or ""))
                break
            except (crypto_box.DecryptionError, ValueError, KeyError):
                continue
        opened[str(m.get("message_id", ""))] = text
    return {"opened": opened}


def build_app(ring: KeyRing, token: str, origins: list[str]) -> FastAPI:
    app = FastAPI(title="Emporia DM reader", docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(CORSMiddleware, allow_origins=origins, allow_methods=["GET", "POST"],
                       allow_headers=["x-emporia-reader-token", "content-type"])

    def _check(request: Request) -> None:
        if not secrets.compare_digest(request.headers.get("x-emporia-reader-token", ""), token):
            raise HTTPException(401, "reader token required")

    @app.get("/health")
    async def health(request: Request):
        _check(request)
        return {"ok": True, "agents": ring.agents()}

    @app.post("/dm/open")
    async def dm_open(request: Request):
        _check(request)
        body = await request.json()
        viewer, thread_id = str(body.get("viewer", "")), str(body.get("thread_id", ""))
        messages = body.get("messages") or []
        if not viewer or not thread_id or not isinstance(messages, list):
            raise HTTPException(400, "viewer, thread_id and messages are required")
        return open_messages(ring, viewer, thread_id, messages)

    return app


def main(argv: list[str] | None = None) -> int:
    import uvicorn

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--port", type=int,
                        default=int(os.getenv("EMPORIA_DM_READER_PORT", DEFAULT_PORT)))
    parser.add_argument("--keys-dir", action="append", default=[],
                        help="a key directory to read (repeatable); default: every profile's")
    parser.add_argument("--profiles-root", default="",
                        help="where Hermes profiles live (default: found from this checkout)")
    parser.add_argument("--dashboard", default=os.getenv("EMPORIA_RELAY_URL", "http://localhost:8088"),
                        help="the dashboard's relay URL, for the link and the allowed origin")
    args = parser.parse_args(argv)

    key_dirs = [Path(d).expanduser() for d in args.keys_dir] or find_key_dirs(
        Path(args.profiles_root).expanduser() if args.profiles_root else _profiles_root())
    ring = KeyRing(key_dirs)
    if not ring.agents():
        print("error: no encryption keys found; pass --keys-dir", file=sys.stderr)
        return 1
    token = secrets.token_urlsafe(24)
    base = args.dashboard.rstrip("/")
    origins = sorted({base, base.replace("localhost", "127.0.0.1"),
                      base.replace("127.0.0.1", "localhost")})
    print(f"Emporia DM reader on http://127.0.0.1:{args.port} — can open DMs for: "
          f"{', '.join(ring.agents())}")
    print("Open the dashboard with this link (the token lives only in that browser session):")
    print(f"  {base}/ui/?reader={token}&reader_port={args.port}")
    uvicorn.run(build_app(ring, token, origins), host="127.0.0.1", port=args.port,
                log_level="warning")
    return 0


if __name__ == "__main__":
    sys.exit(main())
