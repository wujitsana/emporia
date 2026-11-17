"""The remote endpoint's authorization server: spec conformance, registration and redirect rules, tokens, consent."""
import json
import sqlite3
from urllib.parse import parse_qs, urlsplit

import pytest
from remote_support import AGENT, REDIRECT, URL, Rig, pkce

from emporia import remote as R
from emporia.remote.oauth import OAuthError


@pytest.fixture
def rig(tmp_path, monkeypatch):
    return Rig(tmp_path, monkeypatch)


# ── spec conformance ──────────────────────────────────────────────────────────────────

async def test_no_token_is_401_with_resource_metadata_and_scope(rig):
    async with rig.running() as c:
        r = await c.rpc(None, "tools/list")
    assert r.status_code == 401
    www = r.headers["www-authenticate"]
    assert www.startswith("Bearer ")
    assert f'resource_metadata="{URL}/.well-known/oauth-protected-resource/mcp"' in www
    assert 'scope="emporia:read"' in www


async def test_protected_resource_metadata_at_both_urls(rig):
    async with rig.running() as c:
        a = (await c.http.get("/.well-known/oauth-protected-resource")).json()
        b = (await c.http.get("/.well-known/oauth-protected-resource/mcp")).json()
    assert a == b
    assert a["resource"] == URL + "/mcp" and a["authorization_servers"] == [URL]
    assert a["scopes_supported"] == ["emporia:read", "emporia:talk"] and a["bearer_methods_supported"] == ["header"]


async def test_authorization_server_metadata_has_what_a_client_needs(rig):
    async with rig.running() as c:
        r = await c.http.get("/.well-known/oauth-authorization-server")
    m = r.json()
    assert m["issuer"] == URL
    for k, path in (("authorization_endpoint", "/authorize"), ("token_endpoint", "/token"),
                    ("registration_endpoint", "/register"), ("revocation_endpoint", "/revoke")):
        assert m[k] == URL + path
    assert m["code_challenge_methods_supported"] == ["S256"]
    assert m["response_types_supported"] == ["code"]
    assert set(m["grant_types_supported"]) == {"authorization_code", "refresh_token"}
    assert m["token_endpoint_auth_methods_supported"] == ["none"]
    assert m["authorization_response_iss_parameter_supported"] is True


async def test_plain_pkce_and_a_missing_challenge_are_refused_by_redirect(rig):
    async with rig.running() as c:
        reg = await c.register()
        _, challenge = pkce()
        for over in ({"code_challenge_method": "plain"}, {"code_challenge_method": None},
                     {"code_challenge": None}, {"code_challenge": "short"}):
            r = await c.http.get("/authorize", params=c.authorize_params(reg["client_id"], challenge, **over))
            assert r.status_code == 302
            q = parse_qs(urlsplit(r.headers["location"]).query)
            assert r.headers["location"].startswith(REDIRECT) and q["error"] == ["invalid_request"], over
            assert q["state"] == ["st4te"] and q["iss"] == [URL]
    assert rig.auth.pending_rows() == []


async def test_resource_is_required_and_must_be_this_server_at_authorize_and_at_token(rig):
    async with rig.running() as c:
        reg = await c.register()
        _, challenge = pkce()
        for resource in (None, "https://other.example/mcp", URL + "/other"):
            r = await c.http.get("/authorize", params=c.authorize_params(reg["client_id"], challenge,
                                                                         resource=resource))
            q = parse_qs(urlsplit(r.headers["location"]).query)
            assert q["error"] == ["invalid_target"] and q["iss"] == [URL]
    # a token request with a wrong or missing resource is refused
    async with rig.running() as c:
        for resource in (None, "https://other.example/mcp"):
            reg, verifier, page = await c.start()
            c.approve()
            r = await c.finish(reg, verifier, page, resource=resource)
            assert r.status_code == 400 and r.json()["error"] == "invalid_target"


async def test_the_authorization_response_carries_iss_and_state(rig):
    async with rig.running() as c:
        reg, verifier, page = await c.start()
        c.approve()
        status = (await c.http.get(page + "/status")).json()
    q = parse_qs(urlsplit(status["redirect"]).query)
    assert status["redirect"].startswith(REDIRECT + "?") and q["iss"] == [URL] and q["state"] == ["st4te"]
    assert len(q["code"][0]) > 40


async def test_a_token_in_the_query_string_is_never_accepted(rig):
    async with rig.running() as c:
        tok = await c.tokens()
        r = await c.http.post(R.MCP_PATH + "?access_token=" + tok["access_token"], json={
            "jsonrpc": "2.0", "id": 1, "method": "tools/list"}, headers={"Accept": "application/json"})
    assert r.status_code == 401


async def test_a_token_for_another_audience_is_401(rig):
    async with rig.running() as c:
        tok = await c.tokens()
        rig.store.run("UPDATE access SET resource=?", ("https://other.example/mcp",))
        r = await c.rpc(tok["access_token"], "tools/list")
    assert r.status_code == 401
    assert 'error="invalid_token"' in r.headers["www-authenticate"]


async def test_a_call_outside_the_scope_is_403_insufficient_scope_with_the_scope_needed(rig):
    async with rig.running() as c:
        tok = await c.tokens(approve=["emporia:read"])
        r = await c.rpc(tok["access_token"], "tools/call", {"name": "post", "arguments": {"text": "hi"}})
    assert r.status_code == 403
    assert r.json()["error"] == "insufficient_scope"
    www = r.headers["www-authenticate"]
    assert 'error="insufficient_scope"' in www and 'scope="emporia:talk"' in www and "resource_metadata=" in www


# ── registration and redirects ────────────────────────────────────────────────────────

@pytest.mark.parametrize("uri", ["http://127.0.0.1:5000/cb", "http://localhost:61234/callback", "http://[::1]:7/x",
                                 "http://127.0.0.1/cb", "https://claude.ai/api/mcp/auth_callback"])
async def test_loopback_and_allowlisted_redirects_are_accepted(rig, uri):
    async with rig.running() as c:
        r = await c.http.post("/register", json={"client_name": "x", "redirect_uris": [uri]})
    assert r.status_code == 201, r.text


@pytest.mark.parametrize("uri", ["http://example.com/cb", "https://evil.example/cb", "myapp://callback",
                                 "https://claude.ai/api/mcp/auth_callback#frag", "https://u:p@claude.ai/x",
                                 "http://127.0.0.1.evil.example/cb", "javascript:alert(1)", "", "https://claude.ai/other"])
async def test_every_other_redirect_is_refused(rig, uri):
    async with rig.running() as c:
        r = await c.http.post("/register", json={"client_name": "x", "redirect_uris": [uri]})
    assert r.status_code == 400 and r.json()["error"] == "invalid_redirect_uri"


async def test_the_person_can_allow_one_more_redirect_and_it_is_logged(rig):
    rig.store.allow_redirect("https://host.example/cb")
    rig.audit.write("allow_redirect", detail="https://host.example/cb")
    async with rig.running() as c:
        r = await c.http.post("/register", json={"client_name": "x", "redirect_uris": ["https://host.example/cb"]})
    assert r.status_code == 201
    assert any(x["event"] == "allow_redirect" for x in rig.audit.rows())


async def test_only_public_clients_are_registered(rig):
    async with rig.running() as c:
        for body in ({"token_endpoint_auth_method": "client_secret_basic"}, {"grant_types": ["password"]},
                     {"response_types": ["token"]}, {"scope": "emporia:private"}):
            r = await c.http.post("/register", json={"client_name": "x", "redirect_uris": [REDIRECT], **body})
            assert r.status_code == 400 and r.json()["error"] == "invalid_client_metadata", body
        ok = await c.register()
    assert ok["token_endpoint_auth_method"] == "none" and "client_secret" not in ok


async def test_the_redirect_must_match_exactly_and_a_bad_one_is_never_redirected_to(rig):
    async with rig.running() as c:
        reg = await c.register()
        _, challenge = pkce()
        for uri in ("http://127.0.0.1:9999/callback/x", "http://127.0.0.1:9998/callback", "http://127.0.0.1:9999/",
                    "https://evil.example/cb"):
            r = await c.http.get("/authorize", params=c.authorize_params(reg["client_id"], challenge,
                                                                         redirect_uri=uri))
            assert r.status_code == 400 and "location" not in r.headers, uri
        r = await c.http.get("/authorize", params=c.authorize_params(reg["client_id"], challenge, redirect_uri=None))
        assert r.status_code == 400 and "location" not in r.headers


async def test_an_unknown_client_is_shown_on_the_servers_own_page(rig):
    async with rig.running() as c:
        _, challenge = pkce()
        r = await c.http.get("/authorize", params=c.authorize_params("nope", challenge))
    assert r.status_code == 400 and "location" not in r.headers and "not known" in r.text


async def test_registration_is_rate_limited_capped_and_unused_clients_expire(rig):
    async with rig.running() as c:
        for _ in range(R.REGISTRATIONS_PER_HOUR):
            await c.register()
        r = await c.http.post("/register", json={"client_name": "x", "redirect_uris": [REDIRECT]})
        assert r.status_code == 429
        rig.clock.advance(3601)
        r = await c.http.post("/register", json={"client_name": "x", "redirect_uris": [REDIRECT]})
        assert r.status_code == 201                            # the hour passed
        rig.clock.advance(R.CLIENT_UNUSED_TTL + 10)            # a day: the unapproved ones are dropped
        rig.store.purge()
        assert rig.store.one("SELECT COUNT(*) n FROM clients")["n"] == 0
    # the cap on live clients
    for i in range(R.MAX_CLIENTS):
        rig.store.run("INSERT INTO clients VALUES(?,?,?,?,?,?)", (f"c{i}", "n", "[]", "emporia:read",
                                                                  rig.clock(), 0))
    async with rig.running() as c:
        r = await c.http.post("/register", json={"client_name": "x", "redirect_uris": [REDIRECT]})
        assert r.status_code == 429


# ── tokens ────────────────────────────────────────────────────────────────────────────

async def test_access_token_expires_after_15_minutes_by_the_injected_clock(rig):
    async with rig.running() as c:
        tok = await c.tokens()
        assert tok["expires_in"] == 900 and tok["token_type"] == "Bearer"
        assert (await c.rpc(tok["access_token"], "tools/list")).status_code == 200
        rig.clock.advance(899)
        assert (await c.rpc(tok["access_token"], "tools/list")).status_code == 200
        rig.clock.advance(2)
        assert (await c.rpc(tok["access_token"], "tools/list")).status_code == 401


async def _refresh(c, tok, **over):
    form = {"grant_type": "refresh_token", "refresh_token": tok["refresh_token"], "client_id": tok["client_id"]}
    form.update(over)
    return await c.http.post("/token", data={k: v for k, v in form.items() if v is not None})


async def test_refresh_rotates_and_reusing_an_old_one_revokes_the_whole_grant(rig):
    async with rig.running() as c:
        first = await c.tokens()
        r = await _refresh(c, first)
        assert r.status_code == 200
        second = {**r.json(), "client_id": first["client_id"]}
        assert second["refresh_token"] != first["refresh_token"] and second["access_token"] != first["access_token"]
        assert (await c.rpc(second["access_token"], "tools/list")).status_code == 200
        # the old refresh token again: someone copied it. The grant dies, with the newest tokens too.
        bad = await _refresh(c, first)
        assert bad.status_code == 400 and bad.json()["error"] == "invalid_grant"
        assert (await c.rpc(second["access_token"], "tools/list")).status_code == 401
        assert (await _refresh(c, second)).status_code == 400
    assert any(x["event"] == "refresh_reuse" for x in rig.audit.rows())


async def test_a_refresh_cannot_widen_scope_and_the_idle_and_absolute_limits_hold(rig):
    async with rig.running() as c:
        tok = await c.tokens(approve=["emporia:read"])
        r = await _refresh(c, tok, scope="emporia:read emporia:talk")
        assert r.status_code == 400 and r.json()["error"] == "invalid_scope"
    async with rig.running() as c:
        tok = await c.tokens()
        rig.clock.advance(R.REFRESH_IDLE_TTL + 5)
        assert (await _refresh(c, tok)).status_code == 400
    async with rig.running() as c:
        tok = await c.tokens()
        for _ in range(4):                                # keep using it: 4 x 29 days passes the 90-day grant
            rig.clock.advance(29 * 24 * 3600)
            r = await _refresh(c, tok)
            if r.status_code != 200:
                break
            tok = {**r.json(), "client_id": tok["client_id"]}
        assert r.status_code == 400, "after 90 days the person approves again"


async def test_a_code_works_once_and_its_second_use_revokes_the_grant(rig):
    async with rig.running() as c:
        reg, verifier, page = await c.start()
        c.approve()
        status = (await c.http.get(page + "/status")).json()
        code = parse_qs(urlsplit(status["redirect"]).query)["code"][0]
        form = {"grant_type": "authorization_code", "code": code, "code_verifier": verifier,
                "client_id": reg["client_id"], "redirect_uri": REDIRECT, "resource": URL + "/mcp"}
        ok = await c.http.post("/token", data=form)
        assert ok.status_code == 200
        again = await c.http.post("/token", data=form)
        assert again.status_code == 400 and "revoked" in again.json()["error_description"]
        assert (await c.rpc(ok.json()["access_token"], "tools/list")).status_code == 401
        # the page cannot mint a second code either
        assert (await c.http.get(page + "/status")).json()["status"] == "done"


async def test_a_code_dies_after_60_seconds_and_pkce_and_redirect_are_checked(rig):
    async with rig.running() as c:
        reg, verifier, page = await c.start()
        c.approve()
        r = await c.finish(reg, verifier, page, delay=R.CODE_TTL + 1)
        assert r.status_code == 400 and r.json()["error"] == "invalid_grant"
    async with rig.running() as c:
        reg, verifier, page = await c.start()
        c.approve()
        r = await c.finish(reg, verifier, page, code_verifier=pkce()[0])
        assert r.status_code == 400 and r.json()["error"] == "invalid_grant"
    async with rig.running() as c:
        reg, verifier, page = await c.start()
        c.approve()
        r = await c.finish(reg, verifier, page, redirect_uri="http://127.0.0.1:9999/other")
        assert r.status_code == 400
    async with rig.running() as c:
        reg, verifier, page = await c.start()
        c.approve()
        other = await c.register()
        r = await c.finish(reg, verifier, page, client_id=other["client_id"])     # another client's code
        assert r.status_code == 400 and r.json()["error"] == "invalid_grant"


async def test_only_hashes_are_at_rest(rig):
    async with rig.running() as c:
        reg, verifier, page = await c.start()
        c.approve()
        status = (await c.http.get(page + "/status")).json()
        code = parse_qs(urlsplit(status["redirect"]).query)["code"][0]
        form = {"grant_type": "authorization_code", "code": code, "code_verifier": verifier,
                "client_id": reg["client_id"], "redirect_uri": REDIRECT, "resource": URL + "/mcp"}
        tok = (await c.http.post("/token", data=form)).json()
    blob = rig.store.path.read_bytes()
    page_token = page.rsplit("/", 1)[1]
    for secret in (code, tok["access_token"], tok["refresh_token"], page_token, verifier):
        assert secret.encode() not in blob, "a secret is in the database in the clear"
    audit = (rig.folder / "audit.jsonl").read_text()
    for secret in (code, tok["access_token"], tok["refresh_token"], page_token):
        assert secret not in audit
    assert oct(rig.store.path.stat().st_mode & 0o777) == "0o600" and oct(rig.folder.stat().st_mode & 0o777) == "0o700"


async def test_revocation_takes_effect_on_the_next_request_by_cli_or_by_the_client(rig):
    async with rig.running() as c:
        tok = await c.tokens()
        assert (await c.rpc(tok["access_token"], "tools/list")).status_code == 200
        grant = rig.store.one("SELECT grant_id FROM grants")["grant_id"]
        assert rig.auth.revoke_grant(grant) is True
        assert (await c.rpc(tok["access_token"], "tools/list")).status_code == 401
        assert (await _refresh(c, tok)).status_code == 400
        # a client revokes itself (RFC 7009)
        tok2 = await c.tokens()
        r = await c.http.post("/revoke", data={"token": tok2["refresh_token"], "client_id": tok2["client_id"]})
        assert r.status_code == 200
        assert (await c.rpc(tok2["access_token"], "tools/list")).status_code == 401
        # --all
        tok3 = await c.tokens()
        assert rig.auth.revoke_all() == 1
        assert (await c.rpc(tok3["access_token"], "tools/list")).status_code == 401


# ── consent ───────────────────────────────────────────────────────────────────────────

async def test_no_http_route_can_approve_a_pending_request(rig):
    async with rig.running() as c:
        reg, verifier, page = await c.start()
        code = rig.auth.pending_rows()[0]["code"]
        token = page.rsplit("/", 1)[1]
        for method, path, kw in (
                ("get", page, {}), ("post", page, {"data": {"approve": "1"}}), ("post", page, {"json": {"approve": 1}}),
                ("post", page + "/approve", {}), ("get", page + "/approve", {}), ("post", "/approve", {}),
                ("get", f"/consent/{code}", {}), ("post", page + "/status", {}),
                ("get", f"/authorize?approve=1&code={code}", {})):
            r = await getattr(c.http, method)(path, **kw)
            assert r.status_code in (200, 302, 400, 404, 405), (method, path, r.status_code)
        assert rig.store.one("SELECT COUNT(*) n FROM grants")["n"] == 0
        assert (await c.http.get(page + "/status")).json()["status"] == "pending"
        page_html = (await c.http.get(page)).text
    assert "emporia agent connector approve" in page_html and code in page_html
    assert token not in page_html


async def test_the_consent_page_has_strict_headers_and_escapes_the_clients_name(rig):
    async with rig.running() as c:
        r = await c.http.post("/register", json={"client_name": "<script>alert(1)</script>",
                                                 "redirect_uris": [REDIRECT]})
        reg = r.json()
        _, challenge = pkce()
        loc = (await c.http.get("/authorize", params=c.authorize_params(reg["client_id"], challenge))).headers["location"]
        page = await c.http.get(loc)
    assert "<script>alert(1)</script>" not in page.text and "&lt;script&gt;" in page.text
    csp = page.headers["content-security-policy"]
    assert "frame-ancestors 'none'" in csp and "default-src 'none'" in csp
    assert page.headers["cache-control"] == "no-store" and page.headers["x-frame-options"] == "DENY"


async def test_a_request_the_person_never_approved_expires_and_cannot_be_approved_later(rig):
    async with rig.running() as c:
        reg, verifier, page = await c.start()
        code = rig.auth.pending_rows()[0]["code"]
        rig.clock.advance(R.PENDING_TTL + 1)
        with pytest.raises(OAuthError):
            rig.auth.approve(code)
        status = (await c.http.get(page + "/status")).json()
        assert status["status"] == "expired" and "access_denied" in status["redirect"]
        assert parse_qs(urlsplit(status["redirect"]).query)["iss"] == [URL]
        assert (await c.http.get(page)).status_code == 404


async def test_a_denied_request_redirects_with_access_denied(rig):
    async with rig.running() as c:
        reg, verifier, page = await c.start()
        assert rig.auth.deny(rig.auth.pending_rows()[0]["code"]) is True
        status = (await c.http.get(page + "/status")).json()
    assert status["status"] == "denied" and parse_qs(urlsplit(status["redirect"]).query)["error"] == ["access_denied"]


async def test_an_approval_narrower_than_asked_is_honored_everywhere(rig):
    async with rig.running() as c:
        tok = await c.tokens(scope="emporia:read emporia:talk", approve=["emporia:read"])
        assert tok["scope"] == "emporia:read"
        names = await c.tool_names(tok["access_token"])
        assert "post" not in names and "read_feed" in names
        grant = rig.store.one("SELECT scopes FROM grants")
        assert json.loads(grant["scopes"]) == ["emporia:read"]
    async with rig.running() as c:
        reg, verifier, page = await c.start(scope="emporia:read")
        with pytest.raises(OAuthError):
            rig.auth.approve(rig.auth.pending_rows()[0]["code"], ["emporia:talk"])    # wider than asked


async def test_too_many_waiting_requests_are_refused(rig):
    async with rig.running() as c:
        reg = await c.register()
        _, challenge = pkce()
        for _ in range(R.MAX_PENDING):
            assert (await c.http.get("/authorize", params=c.authorize_params(reg["client_id"], challenge))
                    ).headers["location"].startswith("/consent/")
        r = await c.http.get("/authorize", params=c.authorize_params(reg["client_id"], challenge))
    assert parse_qs(urlsplit(r.headers["location"]).query)["error"] == ["temporarily_unavailable"]


async def test_the_agent_name_on_the_page_and_in_the_command_is_the_bound_agent(rig):
    async with rig.running() as c:
        reg, verifier, page = await c.start()
        html = (await c.http.get(page)).text
    assert AGENT in html


def test_the_database_is_a_sqlite_file_with_only_hashed_token_columns(rig):
    con = sqlite3.connect(rig.store.path)
    cols = {t: [r[1] for r in con.execute(f"PRAGMA table_info({t})")] for t in ("codes", "access", "refresh", "pending")}
    assert "hash" in cols["codes"] and "hash" in cols["access"] and "hash" in cols["refresh"]
    assert "page_hash" in cols["pending"]
    assert not any(c in ("token", "code_plain", "secret", "access_token", "refresh_token")
                   for cs in cols.values() for c in cs)
