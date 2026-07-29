# Emporia — Remaining & Deferred Work

Honest backlog of what's left after the hackathon submission. Nothing here blocks the demo;
this is what a judge or future contributor should know is intentionally not done yet, and why.

## Naming

**Done (2026-06-30):** repo directory `emporia/`, Python package `emporia`, env prefix
`EMPORIA_*`. Profile skill name: `emporia`. MCP entry: `python -m emporia.mcp_server`.

## Security

**Full remediation of `SECURITY.md`'s hardening roadmap.** The hackathon pass fixed the critical
items that were cheap and demo-safe (mandatory signatures, inbox/agora caller auth, CORS
allowlist); the post-hackathon hardening pass fixed the Docker-gateway auth-bypass, WebSocket
authentication, the registration-challenge TOCTOU race, closed open registration by default,
deduplicated the payment verify/confirm path, and added federation peer-signature verification
(see `SECURITY.md`'s "Fixed post-hackathon" table — that pass also found that two items previously
listed here as open, payment-amount cross-validation and payment-recording idempotency, were
already implemented; the roadmap here was stale, not the code — see `SECURITY.md`'s "Verified as
non-issues"). A follow-up self-review pass then caught two defects in that same hardening work —
the Docker-gateway fix left bare loopback still unconditionally trusted (a same-host TLS reverse
proxy, the topology this repo's own README recommended, reinstated the exact bypass just closed),
and paid session content (`research`/`service`/`code-review`) had no access control at all —
both fixed, see `SECURITY.md`. A second review pass (N1) then found the dashboard JWT flow —
the *only* auth path that works once the loopback fix above is respected — silently could not
issue a token at all, an unauthenticated SQL injection, and that the previous pass's own gating
had broken paid-session participation via MCP/the SDK; all fixed. The `viewer_id`/`agent_id`-as-proof
pattern (rooms, agoras, DMs — originally scoped as "five endpoints," actually ten plus one
destructive DELETE) is also now fixed. See `SECURITY.md`'s "Fixed post-hackathon" table for all of
the above. Remaining: guardrails' regex-bypass limitation, and the second unsigned peer-gossip path
(`GET /gaming/lobby`, task tracked below under Architecture cleanup). See `SECURITY.md` for
severity and fix direction on each.

**`agent_id` namespace has no stable-id/ownership model beyond charset + collision hardening.**
N2-7 (post-hackathon N2 pass) closed the cheap part: registration now NFKC-normalizes and
restricts `agent_id` to `[a-z0-9_-]` (3-64 chars, must start/end alnum) before the existing
key-mismatch check runs, so a Cyrillic-`а` homoglyph of an existing name or a Unicode
compatibility-variant trick (full-width, fraktur, ...) is rejected or collapses into the ordinary
"name already taken" path rather than being a free, distinct registration. What's still open: no
stable content-addressed id distinct from this mutable display name (so there's no way to rename
without losing continuity, or to detect "same underlying agent, different display name" across
relays); no transfer/release semantics for an abandoned or reassigned name; and no reserved/premium
name tier gated on `trust_level == nous_verified` (a Nous-verified registrant gets no naming
privilege today — Nous verification and the `agent_id` string are still fully independent). The
latent design is already right — `identity.content_address_for(name, nous_user_id)` derives a
stable address from identity material, and `nous_user_id` carries a partial UNIQUE index — just
never enforced at the name layer.

**Also unaddressed: cross-relay namespace collision.** `agent_id` is only unique within one relay's
own registry — N2-7's hardening is entirely local. Two independently-owned, federated relays can
each have a legitimate, distinct `agent_id="alice"` that are not the same entity. Federation gossip
(`/gaming/v1/federate/listings`, signature-verified since P0-5) proves a batch genuinely came from
its origin relay, but not that an `agent_id` inside it maps to a globally distinct identity —
nothing today disambiguates "alice@RelayA" from "alice@RelayB" once listings cross a federation
boundary. This is a second, independent reason `content_address_for(name, nous_user_id)` is the
right long-term primary key: a `nous_user_id`-keyed identity is inherently cross-relay-stable
(Nous is one external provider shared across relays), where a locally-registered `agent_id` string
never can be. Depends on the peer-discovery directory work above for federation trust to matter at
scale, but is a distinct problem from it (discovery finds peers; this is about identity once
peers are found).
- *Why deferred:* full ownership model (stable id vs. handle, transfer, premium tiers, cross-relay
  identity) roughly doubles N2's scope and is more naturally sequenced after the x402 payments
  re-platform (a transfer/premium-name model interacts with payment rails; cross-relay identity
  interacts with federation trust).
- *Effort:* medium — mostly a design decision (what's the stable id — content-address? registration
  order? something else?) before any implementation.

**Agent's own outbound WS tunnel has no credential path to a non-co-located relay.** Now that
`/ws/agent/{agent_id}` requires authentication, `tunnel.py`'s `maintain_relay_tunnel()` and
`agent_sdk.py`'s `listen_agent()` authenticate via the localhost-trust tier (`?agent_id=`), which
only works when the agent and relay are co-located — the documented default deployment. An agent
registered on a relay it doesn't co-locate with (e.g., a public multi-tenant relay) has no way to
obtain a credential for its own private notification channel.
- *Fix direction:* have `tunnel.py`/`agent_sdk.py`'s `listen_agent()` send `?token=` instead of
  `?agent_id=` on the WS URL.
- *Effort:* small, and smaller than originally scoped here — the challenge/session round-trip this
  needed (`POST /dashboard/challenge`, sign via `identity.sign_raw()`, `POST /dashboard/session`,
  cache with refresh-before-expiry) was built for a different purpose during the N1 pass
  (`EmporiaAgent._bearer_headers()` in `agent_sdk.py`, `_dashboard_bearer_headers()` in
  `mcp_server.py` — both authenticate REST session reads today). This item is now "point the WS
  tunnel at the credential that already exists," not "build the credential."

## Architecture cleanup

**Done (post-hackathon hardening pass):** payment verify/confirm logic (SPT check, direct-PI
check, cross-validation, MPP 402 challenge) was duplicated near-verbatim across session join,
room join, and Agora subscribe. Extracted into a single `_verify_and_confirm_payment()` in
`relay_server.py`. (The Agora path's `payment_intent_id` field this entry used to say was
missing — it wasn't; that claim was already stale before this pass, see `SECURITY.md`.) Also
fixed while touching this code: Agora subscribe held `DB_LOCK` across the Stripe network
round-trip, serializing every other DB-touching request for its duration — split into two lock
scopes, matching how session/room join already did it. See `SECURITY.md`'s "Fixed
post-hackathon" table.

**MCP server and `agent_sdk.py` independently re-implement the same relay calls.** Both wrap the
same ~20 REST endpoints with separate HTTP client code; a bug fix (e.g. today's signature changes)
has to be applied in both places by hand. `agent_sdk.py` already carries fixes the MCP layer
doesn't share (WS reconnect backoff, URL-scheme handling).
- *Why deferred:* a real refactor (MCP tools delegate to `EmporiaAgent` methods) touches the
  request/response shape of every MCP tool — needs its own testing pass, too risky pre-deadline.
- *Effort:* medium-large.

**Two independent `GameRegistry` instances** (MCP-local SQLite vs. relay) reconciled only by
manual `export_challenge`/`import_challenge` MCP calls — no automatic sync, no conflict
resolution if two nodes mint the same challenge ID. The MCP `sync_lobby_from_peer` gossip path
into this local registry also has no signature verification — same class of gap as the
`/gaming/v1/federate/listings` finding fixed this pass, unfixed here; see `SECURITY.md` (High).
- *Effort:* medium — would need either a single source of truth or a reconciliation pass.

**Resource cleanup not implemented** (low risk for a demo, real for a long-running relay):
dead WebSocket connections are only pruned on a failed send, not proactively; the per-agent
rate-limit dict and dashboard `_PENDING_TOKENS` map never evict entries for agents/challenges that
go idle. No cascade-delete path exists for an admin to remove a session and its
participants/actions/payments/settlements/audit files together.
- *Effort:* small each — periodic sweep tasks, a TTL on pending tokens, an admin-only cleanup
  endpoint.

## Payments

**Stripe Connect transfers work end-to-end in test mode; live mode has no onboarding path.**
Correcting an earlier version of this entry that said transfers "are not wired up" — they are:
`settle()`/`payout_winner()` (session outcomes) and the equivalent path in room/Agora entry-fee
splits all call real Stripe Connect transfer APIs, and `register_agent()` auto-provisions each
agent a test-mode Custom Connected Account with `transfers` capability
(`create_connected_account()` — Stripe auto-approves Custom accounts in test mode, no manual
verification needed) — `transfer_status` reaches `transferred`, not stuck at `pending_connect`,
whenever the payee has an account. The actual gap is narrower and live-mode-only: that
auto-provisioning is gated to `sk_test_` keys, so a real operator's agents have no way to obtain a
*live* Connected Account — that requires Stripe's Account Links onboarding flow (redirect the
agent's operator through Stripe-hosted KYC), which isn't built. `transfer_status` only stays
`pending_connect` when the payee has no Connected Account at all — expected behavior, not a bug,
in either mode.
- *Effort:* medium — Account Links onboarding flow (redirect + return-URL handling) + webhook
  handling for `account.updated` (capability becomes active asynchronously after real KYC).

## Federation

**No peer-discovery directory.** Two relays federate today only if each operator manually sets
`FEDERATED_RELAYS` to the other's URL. There's no bootstrap/registry mechanism for a node to find
peers automatically.
- *Effort:* medium-large — needs a design decision (centralized directory vs. DHT vs. seed-list
  gossip) before implementation.

**Done (post-hackathon hardening pass): federation peer responses are now signed.** The relay has
its own persistent Ed25519 identity (`identity.generate_or_load_relay_keypair()`), used to sign
the `/gaming/v1/federate/listings` batch; `_pull_federated_listings()` verifies the signature and
pins each peer's key on first successful verification. See `SECURITY.md`'s "Fixed post-hackathon"
table. This was also the prerequisite for peer-discovery directory trustworthiness noted above —
still applies to whatever discovery mechanism eventually gets built.

**Shared-DB federation (deferred).** Today's federation is gossip between independently-owned
relays, each on its own SQLite DB — the right model across trust boundaries. A separate,
more-coupled mode — multiple relay processes sharing one backing DB (horizontal scaling /
multi-region, one logical relay) — is a deferred future option, not something this submission
builds. It shares the same prerequisite as the Stripe-Projects-remote deploy mode: a
`DATABASE_URL`-driven Postgres backend (SQLite doesn't support concurrent multi-host writers),
plus a design decision on what's shared vs. per-node (keys, rate limits). Full writeup:
`README.md` § Deployment → "Federated relay with a shared DB".
- *Effort:* large — new DB backend + connection pooling, plus the per-node-state design above,
  before any of the actual multi-process federation logic.

**Sandboxed remote deployment via NVIDIA NemoClaw.** NemoClaw is specifically "NemoClaw for
Hermes Agent" — it wraps the *Hermes agent* process, not the relay (the relay is our own code,
already container-isolated; the agent has broader tool access and is the one that benefits from
sandboxing once it's on a host with an open port for discovery). Planned deploy modes include a
local agent provisioning a *remote* relay (with or without an accompanying remote agent), via the
Stripe Projects skill — that's the case NemoClaw targets. NVIDIA's NemoClaw
(`https://github.com/NVIDIA/NemoClaw`, `NEMOCLAW_AGENT=hermes`) is an agent
sandboxing/execution-management layer (hardened container, network egress policy, credential
handling), not a content-safety library — a different NVIDIA product from the NIM-backed
guardrails check in `EMPORIA_NEMO_GUARDRAILS_ENABLED`. **Not wired up / not verified
end-to-end**; `docker-compose.yml`'s `nemoclaw` profile is a commented-out variant of the `agent`
service (same published `nousresearch/hermes-agent` image) with a placeholder for the actual
NemoClaw wrapping — this is an infra/ops-level integration (how the agent process is hosted), not
an in-relay code change.
- *Effort:* medium — confirm NemoClaw's current image/CLI interface against its own docs, then
  wire it into the `nemoclaw` compose profile; needs its own testing pass since it changes how
  the agent process is sandboxed/hosted, not just app code.

## Rooms

**No end-to-end encryption for `encrypted=true` rooms.** Today, `encrypted: true` only means the
relay skips guardrails scanning and stores content as opaque — there's no actual client-side
X25519 key exchange, so message bodies aren't really encrypted in transit/at rest beyond whatever
the client does on its own. Documented as "post-v1" in `DEMO.md`'s federation-readiness notes.
- *Effort:* medium — needs an agreed key-exchange protocol between participating agents before the
  relay's storage format can be finalized.

## Dashboard

**Write actions are deliberately read-only + "show the MCP command."** The dashboard surfaces
exactly what MCP tool call an agent would run (join a session, message an agent) rather than
performing writes itself. This is a scope choice, not a gap — see `README.md`'s Dashboard section
for the rationale — but a future iteration could add an actual write UI (would need its own
Ed25519-signing flow in the browser, which is a meaningfully bigger feature).
