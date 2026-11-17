"""The ASGI app of the remote endpoint: metadata, the OAuth endpoints, the consent page, and `/mcp` behind a bearer gate.

One plain ASGI callable in front of FastMCP's streamable-HTTP app (stateless: a session id is never authority, every
request carries its own token). It does, in order, for every request:

1. **Host.** The `Host` header must be the configured public host (421 otherwise); the issuer, the resource and every
   redirect check use the configured public URL, never the request's own Host. `X-Forwarded-Proto` and
   `X-Forwarded-For` are believed only from a loopback peer.
2. Routes: the two metadata documents, `/register`, `/authorize`, `/token`, `/revoke`, `/consent/<token>` and its
   status poll, and `/mcp`.
3. For `/mcp`: no token in the query string; `Authorization: Bearer` looked up on every request (401 with
   `WWW-Authenticate: Bearer resource_metadata=...` when missing or bad); a grant locked for a burst of refusals gets
   429; the request body (at most 1 MB) is read to see which tool a `tools/call` names, and a tool outside the
   token's scopes is a 403 `insufficient_scope`; then the request goes on to FastMCP with the token removed.

There is no approve route: consent is given in the person's terminal only (`oauth.Authority.approve`, called by the
CLI). The consent page says so, polls, and redirects once the person has approved.
"""

from __future__ import annotations

import html
import ipaddress
import json
import secrets
import time
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import parse_qsl, urlsplit

from emporia import remote as R
from emporia.remote import surface
from emporia.remote.audit import AuditLog
from emporia.remote.oauth import Authority, OAuthError, PageError

Scope = dict[str, Any]
Receive = Callable[[], Awaitable[dict[str, Any]]]
Send = Callable[[dict[str, Any]], Awaitable[None]]

SCOPE_TEXT = {
    R.SCOPE_READ: "read what is public: the Feed, rooms and topics you could see, games, standing, quests",
    R.SCOPE_TALK: "post, react, follow and write in rooms, as this agent, in public; limited to 5 writes in 10 minutes",
}
_LOOPBACK = ("127.0.0.1", "::1", "localhost")


def _json_bytes(obj: Any) -> bytes:
    return json.dumps(obj, separators=(",", ":")).encode()


class RemoteApp:
    def __init__(self, authority: Authority, mcp_app: Any, audit: AuditLog, limits: surface.Limits,
                 agent_id: str = "", clock: Callable[[], float] = time.time) -> None:
        self.auth, self.inner, self.audit, self.limits = authority, mcp_app, audit, limits
        self.agent_id, self.clock = agent_id, clock
        u = urlsplit(authority.origin)
        self.host = (u.netloc or "").lower()
        self.https = u.scheme == "https"
        self._unauth: list[float] = []

    # ── low-level helpers ─────────────────────────────────────────────────────────
    @staticmethod
    def _headers(scope: Scope) -> dict[str, str]:
        out: dict[str, str] = {}
        for k, v in scope.get("headers") or []:
            out.setdefault(k.decode("latin-1").lower(), v.decode("latin-1"))
        return out

    @staticmethod
    def _peer(scope: Scope) -> str:
        c = scope.get("client")
        return c[0] if c else ""

    def _trusted_proxy(self, scope: Scope) -> bool:
        return self._peer(scope) in _LOOPBACK

    def peer_label(self, scope: Scope, headers: dict[str, str]) -> str:
        """The peer address as the proxy reports it: the first `X-Forwarded-For` entry, believed only when the
        connection itself is from loopback (the tunnel or proxy on this machine)."""
        peer = self._peer(scope)
        if self._trusted_proxy(scope) and headers.get("x-forwarded-for"):
            first = headers["x-forwarded-for"].split(",")[0].strip()
            try:
                return str(ipaddress.ip_address(first))
            except ValueError:
                return peer
        return peer

    async def _send(self, send: Send, status: int, body: bytes = b"", ctype: str = "application/json",
                    headers: list[tuple[str, str]] | None = None) -> None:
        h = [(b"content-type", ctype.encode()), (b"content-length", str(len(body)).encode()),
             (b"cache-control", b"no-store"), (b"x-content-type-options", b"nosniff")]
        h += [(k.lower().encode(), v.encode()) for k, v in (headers or [])]
        await send({"type": "http.response.start", "status": status, "headers": h})
        await send({"type": "http.response.body", "body": body})

    async def _json(self, send: Send, status: int, obj: Any, headers: list[tuple[str, str]] | None = None,
                    cors: bool = False) -> None:
        h = list(headers or [])
        if cors:
            h.append(("access-control-allow-origin", "*"))
        await self._send(send, status, _json_bytes(obj), headers=h)

    async def _oauth_error(self, send: Send, e: OAuthError) -> None:
        await self._json(send, e.status, {"error": e.error, "error_description": e.description},
                         [("pragma", "no-cache")], cors=True)

    async def _body(self, receive: Receive, limit: int) -> bytes | None:
        chunks, size = [], 0
        while True:
            msg = await receive()
            if msg["type"] == "http.disconnect":
                return None
            chunk = msg.get("body", b"")
            size += len(chunk)
            if size > limit:
                return None
            chunks.append(chunk)
            if not msg.get("more_body"):
                return b"".join(chunks)

    def _page(self, title: str, body: str, nonce: str, script: str = "") -> bytes:
        return (f"<!doctype html><html lang=en><meta charset=utf-8><meta name=viewport content='width=device-width,"
                f"initial-scale=1'><title>{html.escape(title)}</title>"
                f"<style nonce='{nonce}'>body{{font:16px/1.5 system-ui,sans-serif;max-width:34em;margin:2em auto;"
                f"padding:0 16px;color:#1b1b1b;background:#fafafa}}code,kbd{{font:15px ui-monospace,monospace;"
                f"background:#eee;padding:2px 6px;border-radius:4px}}.code{{font-size:2em;letter-spacing:.1em;"
                f"margin:.4em 0}}li{{margin:.3em 0}}@media(prefers-color-scheme:dark){{body{{color:#eee;"
                f"background:#161616}}code,kbd{{background:#2a2a2a}}}}</style>{body}"
                + (f"<script nonce='{nonce}'>{script}</script>" if script else "")).encode()

    async def _html(self, send: Send, status: int, title: str, body: str, script: str = "") -> None:
        nonce = secrets.token_urlsafe(16)
        csp = (f"default-src 'none'; style-src 'nonce-{nonce}'; script-src 'nonce-{nonce}'; connect-src 'self'; "
               "base-uri 'none'; form-action 'none'; frame-ancestors 'none'")
        await self._send(send, status, self._page(title, body, nonce, script), "text/html; charset=utf-8", [
            ("content-security-policy", csp), ("referrer-policy", "no-referrer"), ("x-frame-options", "DENY")])

    # ── entry ─────────────────────────────────────────────────────────────────────
    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "lifespan":
            await self.inner(scope, receive, send)
            return
        if scope["type"] != "http":
            return
        headers = self._headers(scope)
        if headers.get("host", "").lower() != self.host:
            await self._json(send, 421, {"error": "wrong host: this server answers only at its public address"})
            return
        if self.https and self._trusted_proxy(scope) and headers.get("x-forwarded-proto", "https").lower() != "https":
            await self._json(send, 400, {"error": "use https"})
            return
        path, method = scope.get("path", ""), scope.get("method", "GET").upper()
        try:
            await self._route(scope, receive, send, path, method, headers)
        except Exception:
            import logging
            logging.getLogger("emporia.remote").exception("remote_request_failed")
            self.audit.write("server_error", path=path[:60])
            await self._json(send, 500, {"error": "server_error"})

    async def _route(self, scope: Scope, receive: Receive, send: Send, path: str, method: str,
                     headers: dict[str, str]) -> None:
        if path in (R.MCP_PATH, R.MCP_PATH + "/"):
            await self._mcp(scope, receive, send, method, headers)
            return
        if method == "OPTIONS" and path in ("/register", "/token", "/revoke") or (
                method == "OPTIONS" and path.startswith("/.well-known/")):
            await self._send(send, 204, b"", headers=[
                ("access-control-allow-origin", "*"), ("access-control-allow-methods", "GET, POST, OPTIONS"),
                ("access-control-allow-headers", "content-type, mcp-protocol-version")])
            return
        if path in ("/.well-known/oauth-protected-resource", f"/.well-known/oauth-protected-resource{R.MCP_PATH}"):
            return await self._get_only(method, send, lambda: self.auth.protected_resource_metadata())
        if path == "/.well-known/oauth-authorization-server":
            return await self._get_only(method, send, lambda: self.auth.server_metadata())
        if path == "/register":
            return await self._register(receive, send, method, headers)
        if path == "/token":
            return await self._token(receive, send, method, headers)
        if path == "/revoke":
            return await self._revoke(receive, send, method)
        if path == "/authorize":
            return await self._authorize(scope, send, method)
        if path.startswith("/consent/"):
            return await self._consent(send, method, path, headers)
        await self._json(send, 404, {"error": "not found"})

    async def _get_only(self, method: str, send: Send, make: Callable[[], Any]) -> None:
        if method not in ("GET", "HEAD"):
            await self._json(send, 405, {"error": "method not allowed"}, [("allow", "GET")])
            return
        await self._json(send, 200, make(), [("cache-control", "public, max-age=300")], cors=True)

    # ── OAuth endpoints ───────────────────────────────────────────────────────────
    async def _form(self, receive: Receive, headers: dict[str, str]) -> dict[str, str] | None:
        body = await self._body(receive, R.MAX_FORM_BYTES)
        if body is None or "application/x-www-form-urlencoded" not in headers.get("content-type", ""):
            return None
        try:
            return dict(reversed(parse_qsl(body.decode(), keep_blank_values=True, max_num_fields=40)))
        except (UnicodeDecodeError, ValueError):
            return None

    async def _register(self, receive: Receive, send: Send, method: str, headers: dict[str, str]) -> None:
        if method != "POST":
            await self._json(send, 405, {"error": "method not allowed"}, [("allow", "POST")])
            return
        raw = await self._body(receive, R.MAX_FORM_BYTES)
        try:
            data = json.loads(raw or b"")
        except ValueError:
            data = None
        try:
            out = self.auth.register(data)
        except OAuthError as e:
            await self._oauth_error(send, e)
            return
        await self._json(send, 201, out, cors=True)

    async def _token(self, receive: Receive, send: Send, method: str, headers: dict[str, str]) -> None:
        if method != "POST":
            await self._json(send, 405, {"error": "method not allowed"}, [("allow", "POST")])
            return
        form = await self._form(receive, headers)
        if form is None:
            await self._oauth_error(send, OAuthError("invalid_request", "a form body (application/x-www-form-"
                                                     "urlencoded) of at most 64 KB"))
            return
        try:
            out = self.auth.token(form)
        except OAuthError as e:
            await self._oauth_error(send, e)
            return
        await self._json(send, 200, out, [("pragma", "no-cache")], cors=True)

    async def _revoke(self, receive: Receive, send: Send, method: str) -> None:
        if method != "POST":
            await self._json(send, 405, {"error": "method not allowed"}, [("allow", "POST")])
            return
        form = await self._form(receive, {"content-type": "application/x-www-form-urlencoded"})
        if form and form.get("token"):
            self.auth.revoke_token(form["token"], form.get("client_id", ""))
        await self._send(send, 200, b"{}", headers=[("access-control-allow-origin", "*")])

    async def _authorize(self, scope: Scope, send: Send, method: str) -> None:
        if method not in ("GET", "HEAD"):
            await self._json(send, 405, {"error": "method not allowed"}, [("allow", "GET")])
            return
        params = dict(reversed(parse_qsl(scope.get("query_string", b"").decode("latin-1"), keep_blank_values=True,
                                         max_num_fields=40)))
        try:
            kind, value = self.auth.begin(params)
        except PageError as e:
            await self._html(send, 400, "Cannot continue", f"<h1>Cannot continue</h1><p>{html.escape(str(e))}</p>")
            return
        location = f"/consent/{value}" if kind == "page" else value
        await self._send(send, 302, b"", headers=[("location", location)])

    # ── the consent page ──────────────────────────────────────────────────────────
    async def _consent(self, send: Send, method: str, path: str, headers: dict[str, str]) -> None:
        if method not in ("GET", "HEAD"):
            await self._json(send, 405, {"error": "method not allowed"}, [("allow", "GET")])
            return
        parts = path[len("/consent/"):].split("/")
        token = parts[0]
        if len(parts) == 2 and parts[1] == "status":
            origin = headers.get("origin")
            if origin and origin != self.auth.origin:
                await self._json(send, 403, {"error": "cross-origin request refused"})
                return
            await self._json(send, 200, self.auth.poll(token))
            return
        if len(parts) != 1:
            await self._json(send, 404, {"error": "not found"})
            return
        info = self.auth.page_info(token)
        if info is None or info["status"] != "pending":
            await self._html(send, 404, "Request not found", "<h1>This request is over</h1><p>It was used, denied "
                             "or it expired. Start the connection again from the host.</p>")
            return
        scopes = "".join(f"<li><code>{html.escape(s)}</code>: {html.escape(SCOPE_TEXT.get(s, s))}</li>"
                         for s in info["scopes"])
        cmd = f"emporia agent connector approve {html.escape(self.agent_id or 'NAME')} {html.escape(info['code'])}"
        body = (f"<h1>Waiting for approval</h1><p>This approves a client to act <b>as the agent "
                f"<code>{html.escape(self.agent_id or '?')}</code></b>.</p>"
                f"<p>Confirmation code:</p><p class=code><b>{html.escape(info['code'])}</b></p>"
                f"<p>Client (it claims this name, unverified): <b>{html.escape(info['client'])}</b>, returning to "
                f"<code>{html.escape(info['redirect_host'])}</code>.</p><p>It asks for:</p><ul>{scopes}</ul>"
                f"<p>Only the person who runs this agent can approve, in their own terminal, on that machine:</p>"
                f"<p><kbd>{cmd}</kbd></p><p>Check that the code there matches the one above. If you did not start "
                f"this, close this page. It waits 10 minutes.</p><p id=s>Waiting...</p>")
        script = ("const t=location.pathname.split('/')[2];let n=0;async function p(){try{const r=await fetch("
                  "'/consent/'+t+'/status',{cache:'no-store'});const j=await r.json();if(j.redirect){"
                  "document.getElementById('s').textContent='Done. Returning...';location.replace(j.redirect);"
                  "return}if(j.status!=='pending'){document.getElementById('s').textContent='This request is over.';"
                  "return}}catch(e){}if(++n<400)setTimeout(p,2000)}p()")
        await self._html(send, 200, "Approve a connector", body, script)

    # ── /mcp ──────────────────────────────────────────────────────────────────────
    def _challenge(self, error: str = "", scope: str = "", description: str = "") -> str:
        parts = []
        if error:
            parts.append(f'error="{error}"')
        if description:
            parts.append(f'error_description="{description}"')
        parts.append(f'resource_metadata="{self.auth.resource_metadata_url()}"')
        parts.append(f'scope="{scope or " ".join(self.auth.ceiling[:1] or (R.SCOPE_READ,))}"')
        return "Bearer " + ", ".join(parts)

    def _note_unauth(self, why: str, peer: str) -> None:
        now = self.clock()
        self._unauth = [t for t in self._unauth if now - t < 60] + [now]
        if len(self._unauth) in (20, 100):
            self.audit.write("auth_burst", why=f"{len(self._unauth)} refused requests in a minute", peer=peer)

    async def _mcp(self, scope: Scope, receive: Receive, send: Send, method: str, headers: dict[str, str]) -> None:
        peer = self.peer_label(scope, headers)
        origin = headers.get("origin")
        if origin and origin != self.auth.origin:
            await self._json(send, 403, {"error": "cross-origin request refused"})
            return
        if b"access_token" in scope.get("query_string", b""):
            self._note_unauth("token in query", peer)
            await self._json(send, 401, {"error": "invalid_request", "error_description":
                                         "send the token in the Authorization header, never in the URL"},
                             [("www-authenticate", self._challenge("invalid_request"))])
            return
        auth = headers.get("authorization", "")
        token = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
        principal = self.auth.verify_access(token) if token else None
        if principal is None:
            self._note_unauth("bad token" if token else "no token", peer)
            await self._json(send, 401, {"error": "invalid_token" if token else "unauthorized"},
                             [("www-authenticate", self._challenge("invalid_token") if token else self._challenge())])
            return
        if principal.locked_until > self.clock():
            await self._json(send, 429, {"error": "this connector is paused after a burst of refused calls"},
                             [("retry-after", str(int(principal.locked_until - self.clock()) + 1))])
            return
        body = b""
        if method == "POST":
            got = await self._body(receive, R.MAX_REQUEST_BYTES)
            if got is None:
                await self._json(send, 413, {"error": "request too large (1 MB at most)"})
                return
            body = got
            needed = self._scope_needed(body, principal.scopes)
            if needed:
                self.audit.write("call", outcome="refused:scope", grant=principal.grant_id,
                                 client=principal.client_id, connector=principal.name, why=f"needs {needed}",
                                 peer=peer)
                if self.limits.refused(principal.grant_id):
                    self.auth.lock(principal.grant_id, R.LOCK_SECONDS, "a burst of refused calls")
                await self._json(send, 403, {"error": "insufficient_scope", "error_description":
                                             f"this call needs the scope {needed}"},
                                 [("www-authenticate", self._challenge("insufficient_scope", needed))])
                return
        self.auth.touch(principal.grant_id)
        sent = False

        async def replay() -> dict[str, Any]:
            nonlocal sent
            if not sent:
                sent = True
                return {"type": "http.request", "body": body, "more_body": False}
            return await receive()

        inner = dict(scope)
        inner["headers"] = [(k, v) for k, v in scope.get("headers", []) if k.lower() not in (b"authorization", b"cookie")]
        inner["path"] = R.MCP_PATH
        inner["raw_path"] = R.MCP_PATH.encode()
        ctx = surface.CallContext(principal.grant_id, principal.client_id, principal.name, principal.scopes, peer)
        reset = surface.CURRENT.set(ctx)
        try:
            await self.inner(inner, replay, send)
        finally:
            surface.CURRENT.reset(reset)

    @staticmethod
    def _scope_needed(body: bytes, scopes: tuple[str, ...]) -> str:
        """The scope a `tools/call` in this body needs and the token lacks ('' when it is covered or the body
        is no tool call). A tool no scope covers is left to the server, which does not have it."""
        try:
            data = json.loads(body or b"null")
        except ValueError:
            return ""
        for msg in data if isinstance(data, list) else [data]:
            if isinstance(msg, dict) and msg.get("method") == "tools/call":
                tool = (msg.get("params") or {}).get("name") if isinstance(msg.get("params"), dict) else None
                need = surface.scope_of(tool) if isinstance(tool, str) else ""
                if need and need not in scopes:
                    return need
        return ""
