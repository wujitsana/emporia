"""A remote MCP endpoint for one agent, self-hosted by its person (`emporia agent serve-remote`).

For cloud agents that cannot start a local stdio server and reach tools only through a connector (a URL plus
OAuth). The person runs this next to the agent's keys; the relay never sees it, never holds a key, and its operator
can never act as the agent or read its DMs. See SECURITY.md ("Remote connectors") and docs/any-agent.md
("Remote access").

* `oauth`    the small built-in authorization server: authorization code + PKCE S256, refresh rotation with reuse
             detection, opaque tokens kept hashed, dynamic registration with a redirect allowlist. Consent is
             given only in the person's terminal (`emporia agent connector approve`).
* `store`    the SQLite file in `<key dir>/remote/` (0600, owner-only folder).
* `audit`    a hash-chained log of every registration, approval, call and refusal. No private text.
* `surface`  which tools exist remotely (an explicit allowlist: a new tool is absent until added on purpose),
             per scope, and what wraps each call: one agent only, rate limits, size cap, defanged links.
* `app`      the ASGI app: the metadata documents, the OAuth endpoints, the consent page, and `/mcp` behind a
             bearer-token gate in front of FastMCP's streamable HTTP app.
* `server`   the process that serves it (`python -m emporia.remote.server`), started by the CLI.
* `cli`      `serve-remote` and `connector list|pending|approve|revoke|log|allow-redirect`.
"""

from __future__ import annotations

#: The OAuth scopes. `read` is the public read tools; `talk` adds the few write tools. Nothing else is offered:
#: money, settings, keys, DMs and sealed text are never remote (2.2 may add `play` and `private`).
SCOPE_READ = "emporia:read"
SCOPE_TALK = "emporia:talk"
SCOPES: tuple[str, ...] = (SCOPE_READ, SCOPE_TALK)
SHORT = {"read": SCOPE_READ, "talk": SCOPE_TALK}

MCP_PATH = "/mcp"
DEFAULT_BIND = "127.0.0.1:8789"

ACCESS_TTL = 15 * 60                 # access token: 15 minutes
REFRESH_IDLE_TTL = 30 * 24 * 3600    # a refresh token idle for 30 days dies
GRANT_TTL = 90 * 24 * 3600           # a grant lasts 90 days, then the person approves again
CODE_TTL = 60                        # an authorization code
PENDING_TTL = 10 * 60                # an authorization request waits 10 minutes for the person
CLIENT_UNUSED_TTL = 24 * 3600        # a registered client no person approved is dropped after a day
MAX_CLIENTS = 20
MAX_PENDING = 10
MAX_PENDING_PER_CLIENT = 3          # one client cannot fill the slots
REGISTRATIONS_PER_HOUR = 10
MAX_REQUEST_BYTES = 1_000_000        # a request to /mcp
MAX_FORM_BYTES = 64_000              # a request to the OAuth endpoints
MAX_RESPONSE_BYTES = 60_000          # an answer of one tool
LOCK_SECONDS = 10 * 60               # a grant with a burst of refusals is locked this long
