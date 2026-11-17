"""The small built-in authorization server, and the resource-server checks that use it.

A deliberately tiny subset of OAuth 2.1 for one owner, one agent and a few clients:

* authorization code with PKCE **S256 only** (`plain` is refused, a missing challenge is refused);
* refresh tokens rotated on every use, with reuse detection (an old one presented again revokes the whole grant);
* opaque 256-bit tokens, stored as hashes only (no JWT, so there is no signing key to guard, and revocation is
  instant: every request looks the hash up);
* dynamic client registration of public clients only (`token_endpoint_auth_method: none`), redirect URIs on a
  allowlist (or a loopback address), rate limited and capped;
* `resource` (RFC 8707) required and equal to this server's one canonical MCP URL, in the authorization and the
  token request, and checked again as the audience of every access token;
* `iss` (RFC 9207) on every authorization response;
* **consent only in the person's terminal.** `begin()` makes a pending request; only `approve()` turns it into a
  grant, and `approve()` is called by the CLI alone. No HTTP route can reach it.

No clock but `self.clock`, so tests can move time. Nothing here prints or logs a secret.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode, urlsplit

from emporia import remote as R
from emporia.remote.audit import AuditLog
from emporia.remote.store import Store, digest, dumps, new_secret, norm_code, short_code

#: HTTPS callbacks of hosts that connect through a browser redirect, as their documentation says (reported, not yet
#: tested here: each is marked so until a real connection is made). A person adds one with
#: `emporia agent connector allow-redirect URL`; anything else is refused at registration.
KNOWN_REDIRECTS: dict[str, str] = {
    "https://claude.ai/api/mcp/auth_callback": "reported, unverified",
    "https://claude.com/api/mcp/auth_callback": "reported, unverified",
}
LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "[::1]", "::1")
_CHALLENGE = re.compile(r"[A-Za-z0-9_-]{43}")
_VERIFIER = re.compile(r"[A-Za-z0-9._~-]{43,128}")
_NAME_STRIP = re.compile(r"[^\x20-\x7e]")


class OAuthError(Exception):
    """An error the HTTP layer sends as RFC 6749 JSON (`error`, `error_description`)."""

    def __init__(self, error: str, description: str = "", status: int = 400) -> None:
        super().__init__(f"{error}: {description}")
        self.error, self.description, self.status = error, description, status


class PageError(Exception):
    """A problem with the client or its redirect: shown on this server's own page, never redirected to."""


@dataclass(frozen=True)
class Principal:
    grant_id: str
    client_id: str
    name: str
    scopes: tuple[str, ...]
    locked_until: float = 0.0


def clean_name(text: Any, limit: int = 80) -> str:
    return _NAME_STRIP.sub("", str(text or "")).strip()[:limit]


def parse_public_url(url: str) -> tuple[str, str]:
    """(origin, "") for a valid public URL, else ("", why). HTTPS with a host and no path, query or fragment; the
    one exception is `http://127.0.0.1` or `localhost` for local testing."""
    try:
        u = urlsplit((url or "").strip())
        port = u.port
    except ValueError:
        return "", "not a URL"
    host = u.hostname or ""
    if u.scheme == "http":
        if host not in ("127.0.0.1", "localhost", "::1"):
            return "", "the public URL must be https:// (http:// only for 127.0.0.1 or localhost, for testing)"
    elif u.scheme != "https":
        return "", "the public URL must start with https://"
    if not host:
        return "", "the public URL needs a host"
    if u.username or u.password or u.query or u.fragment or u.path.strip("/"):
        return "", "the public URL is an origin only: scheme, host and an optional port, no path or query"
    shown = f"[{host}]" if ":" in host else host
    return f"{u.scheme}://{shown}" + (f":{port}" if port else ""), ""


def pkce_s256(verifier: str) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")


class Authority:
    def __init__(self, store: Store, audit: AuditLog, public_url: str, ceiling: tuple[str, ...],
                 agent_id: str = "", clock: Callable[[], float] | None = None) -> None:
        origin, why = parse_public_url(public_url)
        if why:
            raise ValueError(why)
        self.store, self.audit = store, audit
        self.origin = origin
        self.issuer = origin
        self.resource = origin + R.MCP_PATH
        self.ceiling = tuple(s for s in R.SCOPES if s in ceiling)
        self.agent_id = agent_id
        self.clock = clock or store.clock

    # ── metadata ──────────────────────────────────────────────────────────────────
    def resource_metadata_url(self) -> str:
        return f"{self.origin}/.well-known/oauth-protected-resource{R.MCP_PATH}"

    def protected_resource_metadata(self) -> dict[str, Any]:
        return {"resource": self.resource, "authorization_servers": [self.issuer],
                "scopes_supported": list(self.ceiling), "bearer_methods_supported": ["header"],
                "resource_name": "Emporia agent connector"}

    def server_metadata(self) -> dict[str, Any]:
        return {"issuer": self.issuer, "authorization_endpoint": f"{self.origin}/authorize",
                "token_endpoint": f"{self.origin}/token", "registration_endpoint": f"{self.origin}/register",
                "revocation_endpoint": f"{self.origin}/revoke", "scopes_supported": list(self.ceiling),
                "response_types_supported": ["code"], "response_modes_supported": ["query"],
                "grant_types_supported": ["authorization_code", "refresh_token"],
                "token_endpoint_auth_methods_supported": ["none"],
                "revocation_endpoint_auth_methods_supported": ["none"],
                "code_challenge_methods_supported": ["S256"],
                "authorization_response_iss_parameter_supported": True}

    # ── redirects ─────────────────────────────────────────────────────────────────
    def redirect_problem(self, uri: str) -> str:
        """Why `uri` may not be a redirect URI ("" when it may): https and on the allowlist, or loopback http."""
        if not isinstance(uri, str) or not uri or len(uri) > 400 or re.search(r"[\s\x00-\x1f]", uri):
            return "a redirect URI is a URL of up to 400 characters, with no spaces"
        try:
            u = urlsplit(uri)
            u.port  # noqa: B018 - raises on a bad port
        except ValueError:
            return "not a URL"
        if u.fragment or u.username or u.password:
            return "a redirect URI has no fragment and no credentials"
        if u.scheme == "http" and (u.hostname or "") in ("127.0.0.1", "localhost", "::1"):
            return ""
        if u.scheme == "https" and (uri in KNOWN_REDIRECTS or uri in self.store.allowed_redirects()):
            return ""
        return ("this redirect URI is not allowed: only a loopback address (http://127.0.0.1 or localhost) or a "
                "callback the person allowed (`emporia agent connector allow-redirect URL`)")

    # ── registration ──────────────────────────────────────────────────────────────
    def register(self, body: Any) -> dict[str, Any]:
        self.store.purge()
        if not isinstance(body, dict):
            raise OAuthError("invalid_client_metadata", "the body is a JSON object")
        if self.store.events_since("register", 3600) >= R.REGISTRATIONS_PER_HOUR:
            self.audit.write("register_refused", why="rate limit")
            raise OAuthError("temporarily_unavailable", "too many registrations; try again later", 429)
        live = self.store.one("SELECT COUNT(*) n FROM clients")["n"]
        if live >= R.MAX_CLIENTS:
            self.audit.write("register_refused", why="client cap")
            raise OAuthError("temporarily_unavailable", "too many clients are registered; the person can revoke "
                             "unused ones", 429)
        method = body.get("token_endpoint_auth_method") or "none"
        if method != "none":
            raise OAuthError("invalid_client_metadata", "only public clients (token_endpoint_auth_method: none) "
                             "are served; the client proves itself with PKCE")
        grants = body.get("grant_types") or ["authorization_code"]
        if (not isinstance(grants, list) or "authorization_code" not in grants
                or not set(grants) <= {"authorization_code", "refresh_token"}):
            raise OAuthError("invalid_client_metadata", "grant_types: authorization_code, and optionally refresh_token")
        rtypes = body.get("response_types") or ["code"]
        if rtypes != ["code"]:
            raise OAuthError("invalid_client_metadata", "response_types: code")
        uris = body.get("redirect_uris")
        if not isinstance(uris, list) or not uris or len(uris) > 5 or not all(isinstance(x, str) for x in uris):
            raise OAuthError("invalid_redirect_uri", "redirect_uris: one to five URLs")
        for uri in uris:
            if (why := self.redirect_problem(uri)):
                self.audit.write("register_refused", why="redirect", detail=uri[:120])
                raise OAuthError("invalid_redirect_uri", why)
        scope = " ".join(str(body.get("scope") or "").split())
        asked = scope.split() if scope else []
        if any(s not in self.ceiling for s in asked):
            raise OAuthError("invalid_client_metadata", "scope: only " + ", ".join(self.ceiling))
        name = clean_name(body.get("client_name")) or "unnamed client"
        client_id = uuid.uuid4().hex
        now = self.clock()
        self.store.run("INSERT INTO clients VALUES(?,?,?,?,?,?)",
                       (client_id, name, dumps(uris), " ".join(asked) or " ".join(self.ceiling), now, 0))
        self.store.event("register")
        self.audit.write("register", client=client_id, connector=name, detail=", ".join(
            urlsplit(u).netloc for u in uris))
        return {"client_id": client_id, "client_id_issued_at": int(now), "client_name": name, "redirect_uris": uris,
                "token_endpoint_auth_method": "none", "grant_types": grants, "response_types": ["code"],
                "scope": " ".join(asked) or " ".join(self.ceiling)}

    def client(self, client_id: str) -> Any:
        return self.store.one("SELECT * FROM clients WHERE client_id=?", (client_id or "",))

    # ── the authorization request ─────────────────────────────────────────────────
    def _redirect(self, uri: str, state: str, **params: str) -> str:
        q = {**params, "iss": self.issuer}
        if state:
            q["state"] = state
        sep = "&" if urlsplit(uri).query else "?"
        return f"{uri}{sep}{urlencode(q)}"

    def begin(self, params: dict[str, str]) -> tuple[str, str]:
        """Validate an authorization request. Returns ("page", token): send the browser to /consent/<token>; or
        ("redirect", url): an error the client may be told. Raises PageError for an unknown client or a redirect
        that does not match: those are shown here and never redirected to."""
        self.store.purge()
        client = self.client(params.get("client_id", ""))
        if client is None:
            raise PageError("This client is not known here (it may have expired). Register it again.")
        uri = params.get("redirect_uri", "")
        if uri not in json.loads(client["redirect_uris"]) or self.redirect_problem(uri):
            raise PageError("The redirect address does not match the one this client registered.")
        state = (params.get("state") or "")[:512]

        def err(code: str, text: str) -> tuple[str, str]:
            self.audit.write("authorize_refused", client=client["client_id"], connector=client["name"], why=code)
            return "redirect", self._redirect(uri, state, error=code, error_description=text)

        if params.get("response_type") != "code":
            return err("unsupported_response_type", "response_type must be code")
        challenge = params.get("code_challenge", "")
        if params.get("code_challenge_method") != "S256" or not _CHALLENGE.fullmatch(challenge):
            return err("invalid_request", "PKCE is required: code_challenge with code_challenge_method=S256")
        if not self.same_resource(params.get("resource", "")):
            return err("invalid_target", f"resource must be {self.resource}")
        asked = params.get("scope", "").split() or [R.SCOPE_READ]
        allowed = set(client["scope"].split()) & set(self.ceiling)
        if not set(asked) <= allowed:
            return err("invalid_scope", "scope: " + ", ".join(sorted(allowed)))
        if self.store.one("SELECT COUNT(*) n FROM pending WHERE status='pending' AND expires_at>?",
                          (self.clock(),))["n"] >= R.MAX_PENDING:
            return err("temporarily_unavailable", "too many requests are waiting for the person")
        page, code, now = new_secret("pg"), short_code(), self.clock()
        self.store.run("INSERT INTO pending VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                       (digest(page), code, client["client_id"], uri, 1, dumps(asked), state, challenge,
                        self.resource, now, now + R.PENDING_TTL, "pending", "", "", ""))
        self.audit.write("authorize_pending", client=client["client_id"], connector=client["name"], code=code,
                         detail=" ".join(asked))
        return "page", page

    def same_resource(self, resource: str) -> bool:
        """False for a missing or different resource. Compared as a string with one trailing slash forgiven."""
        return bool(resource) and str(resource).rstrip("/") == self.resource.rstrip("/")

    # ── the person's terminal ─────────────────────────────────────────────────────
    def pending_rows(self) -> list[Any]:
        self.store.purge()
        return self.store.all("SELECT * FROM pending WHERE status='pending' AND expires_at>? ORDER BY created_at",
                              (self.clock(),))

    def pending_by_code(self, code: str) -> Any:
        row = self.store.one("SELECT * FROM pending WHERE code=?", (norm_code(code),))
        if row is None or row["status"] != "pending" or row["expires_at"] <= self.clock():
            return None
        return row

    def approve(self, code: str, scopes: list[str] | None = None, name: str = "") -> dict[str, Any]:
        """The person's yes (the CLI calls this after showing what is approved). The scopes may be narrower than
        asked, never wider. Makes the grant; the browser's next poll turns it into an authorization code."""
        row = self.pending_by_code(code)
        if row is None:
            raise OAuthError("invalid_request", "no such request is waiting (it may have expired)")
        asked = json.loads(row["scopes"])
        chosen = [s for s in asked if scopes is None or s in scopes]
        if not chosen or (scopes is not None and not set(scopes) <= set(asked)):
            raise OAuthError("invalid_scope", "approve some of: " + ", ".join(asked))
        client = self.client(row["client_id"])
        label = clean_name(name) or (client["name"] if client else "connector")
        grant_id, now = uuid.uuid4().hex[:12], self.clock()
        self.store.run("INSERT INTO grants(grant_id,client_id,name,scopes,created_at,expires_at) VALUES(?,?,?,?,?,?)",
                       (grant_id, row["client_id"], label, dumps(chosen), now, now + R.GRANT_TTL))
        self.store.run("UPDATE pending SET status='approved', grant_id=?, approved_scopes=?, name=? WHERE page_hash=?",
                       (grant_id, dumps(chosen), label, row["page_hash"]))
        self.audit.write("approve", grant=grant_id, client=row["client_id"], connector=label,
                         detail=("narrower: " if set(chosen) != set(asked) else "") + " ".join(chosen))
        return {"grant_id": grant_id, "scopes": chosen, "name": label}

    def deny(self, code: str) -> bool:
        row = self.pending_by_code(code)
        if row is None:
            return False
        self.store.run("UPDATE pending SET status='denied' WHERE page_hash=?", (row["page_hash"],))
        self.audit.write("deny", client=row["client_id"], code=row["code"])
        return True

    # ── the browser's poll ────────────────────────────────────────────────────────
    def poll(self, page_token: str) -> dict[str, Any]:
        """The state of the request behind a consent page. Once approved, the first poll mints the authorization
        code (so its plaintext is only ever in this answer) and returns the redirect; later polls say `done`."""
        row = self.store.one("SELECT * FROM pending WHERE page_hash=?", (digest(page_token or ""),))
        if row is None:
            return {"status": "unknown"}
        now = self.clock()
        status = row["status"]
        if status == "pending" and row["expires_at"] <= now:
            self.store.run("UPDATE pending SET status='expired' WHERE page_hash=?", (row["page_hash"],))
            status = "expired"
        if status == "pending":
            return {"status": "pending", "code": row["code"]}
        if status in ("denied", "expired"):
            self.store.run("UPDATE pending SET status='done' WHERE page_hash=?", (row["page_hash"],))
            return {"status": status, "redirect": self._redirect(
                row["redirect_uri"], row["state"], error="access_denied",
                error_description="the person did not approve in time" if status == "expired" else "denied")}
        if status == "approved":
            if self.store.run("UPDATE pending SET status='done' WHERE page_hash=? AND status='approved'",
                              (row["page_hash"],)) != 1:
                return {"status": "done"}
            code = new_secret("emp_ac")
            self.store.run("INSERT INTO codes VALUES(?,?,?,?,?,?,?,?,0)",
                           (digest(code), row["grant_id"], row["client_id"], row["redirect_uri"], row["challenge"],
                            row["resource"], row["approved_scopes"], now + R.CODE_TTL))
            return {"status": "approved", "redirect": self._redirect(row["redirect_uri"], row["state"], code=code)}
        return {"status": "done"}

    def page_info(self, page_token: str) -> dict[str, Any] | None:
        row = self.store.one("SELECT * FROM pending WHERE page_hash=?", (digest(page_token or ""),))
        if row is None:
            return None
        client = self.client(row["client_id"])
        return {"code": row["code"], "client": client["name"] if client else "?",
                "redirect_host": urlsplit(row["redirect_uri"]).netloc, "scopes": json.loads(row["scopes"]),
                "status": row["status"]}

    # ── the token endpoint ────────────────────────────────────────────────────────
    def _tokens(self, grant: Any, scopes: list[str], resource: str) -> dict[str, Any]:
        now = self.clock()
        access, refresh = new_secret("emp_at"), new_secret("emp_rt")
        self.store.run("INSERT INTO access VALUES(?,?,?,?,?,?)", (digest(access), grant["grant_id"],
                       grant["client_id"], resource, dumps(scopes), now + R.ACCESS_TTL))
        self.store.run("INSERT INTO refresh VALUES(?,?,?,?,?,?,0,?)", (
            digest(refresh), grant["grant_id"], grant["client_id"], resource, dumps(scopes),
            min(now + R.REFRESH_IDLE_TTL, grant["expires_at"]), now))
        return {"access_token": access, "token_type": "Bearer", "expires_in": R.ACCESS_TTL,
                "refresh_token": refresh, "scope": " ".join(scopes)}

    def _live_grant(self, grant_id: str) -> Any:
        g = self.store.one("SELECT * FROM grants WHERE grant_id=?", (grant_id,))
        if g is None or g["revoked_at"] or g["expires_at"] <= self.clock():
            return None
        return g

    def token(self, form: dict[str, str]) -> dict[str, Any]:
        grant_type = form.get("grant_type", "")
        client = self.client(form.get("client_id", ""))
        if client is None:
            raise OAuthError("invalid_client", "unknown client", 401)
        if form.get("client_secret"):
            raise OAuthError("invalid_client", "this server has public clients only; send no client_secret", 401)
        if grant_type == "authorization_code":
            return self._from_code(client, form)
        if grant_type == "refresh_token":
            return self._from_refresh(client, form)
        raise OAuthError("unsupported_grant_type", "authorization_code or refresh_token")

    def _from_code(self, client: Any, form: dict[str, str]) -> dict[str, Any]:
        code, verifier = form.get("code", ""), form.get("code_verifier", "")
        row = self.store.one("SELECT * FROM codes WHERE hash=?", (digest(code),))
        if row is None or row["client_id"] != client["client_id"]:
            raise OAuthError("invalid_grant", "the code is not valid")
        if self.store.run("UPDATE codes SET used=1 WHERE hash=? AND used=0", (row["hash"],)) != 1:
            # a second use of a code: assume it was stolen; end everything issued from it
            self.revoke_grant(row["grant_id"], "code reuse")
            raise OAuthError("invalid_grant", "the code was already used; the grant is revoked")
        if row["expires_at"] <= self.clock():
            raise OAuthError("invalid_grant", "the code has expired")
        if form.get("redirect_uri") != row["redirect_uri"]:
            raise OAuthError("invalid_grant", "redirect_uri does not match the authorization request")
        if not _VERIFIER.fullmatch(verifier) or not hmac.compare_digest(pkce_s256(verifier), row["challenge"]):
            raise OAuthError("invalid_grant", "the code_verifier does not match")
        if not self.same_resource(form.get("resource", "")) or not self.same_resource(row["resource"]):
            raise OAuthError("invalid_target", f"resource must be {self.resource}")
        grant = self._live_grant(row["grant_id"])
        if grant is None:
            raise OAuthError("invalid_grant", "the grant is revoked or expired")
        out = self._tokens(grant, json.loads(row["scopes"]), self.resource)
        self.store.run("UPDATE clients SET last_used=? WHERE client_id=?", (self.clock(), client["client_id"]))
        self.audit.write("token_issued", grant=grant["grant_id"], client=client["client_id"], connector=grant["name"])
        return out

    def _from_refresh(self, client: Any, form: dict[str, str]) -> dict[str, Any]:
        row = self.store.one("SELECT * FROM refresh WHERE hash=?", (digest(form.get("refresh_token", "")),))
        if row is None or row["client_id"] != client["client_id"]:
            raise OAuthError("invalid_grant", "the refresh token is not valid")
        if self.store.run("UPDATE refresh SET used=1 WHERE hash=? AND used=0", (row["hash"],)) != 1:
            self.revoke_grant(row["grant_id"], "refresh token reuse")
            self.audit.write("refresh_reuse", grant=row["grant_id"], client=client["client_id"])
            raise OAuthError("invalid_grant", "the refresh token was already used; the grant is revoked")
        grant = self._live_grant(row["grant_id"])
        if grant is None or row["expires_at"] <= self.clock():
            raise OAuthError("invalid_grant", "the refresh token has expired or the grant is revoked")
        if form.get("resource") and not self.same_resource(form["resource"]):
            raise OAuthError("invalid_target", f"resource must be {self.resource}")
        have = json.loads(row["scopes"])
        asked = form.get("scope", "").split() or have
        if not set(asked) <= set(have):
            raise OAuthError("invalid_scope", "a refresh cannot widen the scope")
        out = self._tokens(grant, asked, self.resource)
        self.audit.write("token_refreshed", grant=grant["grant_id"], client=client["client_id"],
                         connector=grant["name"])
        return out

    def revoke_token(self, token: str, client_id: str) -> None:
        """RFC 7009: a client revokes its own token (and with it the grant). Always quiet about the result."""
        h = digest(token or "")
        for table in ("access", "refresh"):
            row = self.store.one(f"SELECT grant_id, client_id FROM {table} WHERE hash=?", (h,))
            if row is not None and row["client_id"] == client_id:
                self.revoke_grant(row["grant_id"], "revoked by the client")
                return

    # ── the resource server's check ───────────────────────────────────────────────
    def verify_access(self, token: str) -> Principal | None:
        """The principal behind a bearer token, or None: unknown, expired, for another resource, or its grant is
        revoked or expired. Looked up on every request, so a revoke takes effect on the next one."""
        row = self.store.one("SELECT * FROM access WHERE hash=?", (digest(token or ""),))
        if row is None or row["expires_at"] <= self.clock() or not self.same_resource(row["resource"]):
            return None
        grant = self._live_grant(row["grant_id"])
        if grant is None:
            return None
        scopes = tuple(s for s in json.loads(row["scopes"]) if s in self.ceiling
                       and s in json.loads(grant["scopes"]))
        return Principal(grant["grant_id"], grant["client_id"], grant["name"], scopes, float(grant["locked_until"] or 0))

    def touch(self, grant_id: str) -> None:
        self.store.run("UPDATE grants SET last_used=?, calls=calls+1 WHERE grant_id=?", (self.clock(), grant_id))

    def lock(self, grant_id: str, seconds: float, why: str) -> None:
        self.store.run("UPDATE grants SET locked_until=? WHERE grant_id=?", (self.clock() + seconds, grant_id))
        self.audit.write("grant_locked", grant=grant_id, why=why)

    # ── revoking and listing ──────────────────────────────────────────────────────
    def revoke_grant(self, grant_id: str, reason: str = "revoked by the person") -> bool:
        now = self.clock()
        n = self.store.run("UPDATE grants SET revoked_at=?, revoke_reason=? WHERE grant_id=? AND revoked_at IS NULL",
                           (now, reason, grant_id))
        for table in ("access", "refresh", "codes"):
            self.store.run(f"DELETE FROM {table} WHERE grant_id=?", (grant_id,))
        if n:
            self.audit.write("revoke", grant=grant_id, why=reason)
        return bool(n)

    def revoke_all(self) -> int:
        rows = self.store.all("SELECT grant_id FROM grants WHERE revoked_at IS NULL")
        return sum(self.revoke_grant(r["grant_id"]) for r in rows)

    def listing(self) -> dict[str, list[Any]]:
        self.store.purge()
        return {"clients": self.store.all("SELECT * FROM clients ORDER BY created_at"),
                "grants": self.store.all("SELECT * FROM grants ORDER BY created_at")}
