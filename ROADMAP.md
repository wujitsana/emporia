# Emporia — Remaining & Deferred Work

Honest backlog of what's left after the hackathon submission. Nothing here blocks the demo;
this is what a judge or future contributor should know is intentionally not done yet, and why.

## Naming

**Done (2026-06-30):** repo directory `emporia/`, Python package `emporia`, env prefix
`EMPORIA_*`. Profile skill name: `emporia`. MCP entry: `python -m emporia.mcp_server`.

## Security

**Full remediation of `SECURITY.md`'s hardening roadmap.** Today's pass fixed the critical items
that were cheap and demo-safe (mandatory signatures, inbox/agora caller auth, CORS allowlist).
Still open: WebSocket authentication, federation peer signature verification, payment-amount
cross-validation, payment idempotency, registration-challenge race condition, guardrails'
regex-bypass limitation. See `SECURITY.md` for severity and fix direction on each.
- *Effort:* WS auth (small), federation signing (medium — needs a signed-batch format), payment
  validation + idempotency (small each), challenge race (small).

## Architecture cleanup

**MCP server and `agent_sdk.py` independently re-implement the same relay calls.** Both wrap the
same ~20 REST endpoints with separate HTTP client code; a bug fix (e.g. today's signature changes)
has to be applied in both places by hand. `agent_sdk.py` already carries fixes the MCP layer
doesn't share (WS reconnect backoff, URL-scheme handling).
- *Why deferred:* a real refactor (MCP tools delegate to `EmporiaAgent` methods) touches the
  request/response shape of every MCP tool — needs its own testing pass, too risky pre-deadline.
- *Effort:* medium-large.

**Payment verify/confirm logic is duplicated three times** (session join, room join, Agora
subscribe) instead of one shared helper. The Agora path is also incomplete —
`AgoraSubscribeRequest` has no `payment_intent_id` field, so the `stripe_pi` fallback there is a
no-op; only the SPT/MPP rails work for paid Agora subscriptions today.
- *Effort:* small (extract `_verify_and_confirm_payment()`, add the missing field).

**Two independent `GameRegistry` instances** (MCP-local SQLite vs. relay) reconciled only by
manual `export_challenge`/`import_challenge` MCP calls — no automatic sync, no conflict
resolution if two nodes mint the same challenge ID.
- *Effort:* medium — would need either a single source of truth or a reconciliation pass.

**Resource cleanup not implemented** (low risk for a demo, real for a long-running relay):
dead WebSocket connections are only pruned on a failed send, not proactively; the per-agent
rate-limit dict and dashboard `_PENDING_TOKENS` map never evict entries for agents/challenges that
go idle. No cascade-delete path exists for an admin to remove a session and its
participants/actions/payments/settlements/audit files together.
- *Effort:* small each — periodic sweep tasks, a TTL on pending tokens, an admin-only cleanup
  endpoint.

## Payments

**Real Stripe Connect transfers are not wired up.** Settlement math (97.5%/2.5% split) is
accurate and recorded, but the actual wire transfer to a winner's Connected Account needs Stripe
Connect onboarding that wasn't built for hackathon scope — `transfer_status` stays
`pending_connect` in test mode.
- *Effort:* medium — Connect account onboarding flow + webhook handling for transfer completion.

## Federation

**No peer-discovery directory.** Two relays federate today only if each operator manually sets
`FEDERATED_RELAYS` to the other's URL. There's no bootstrap/registry mechanism for a node to find
peers automatically.
- *Effort:* medium-large — needs a design decision (centralized directory vs. DHT vs. seed-list
  gossip) before implementation.

**Federation peer responses aren't signed** — see `SECURITY.md` (High severity). Listed here too
because fixing it is also the prerequisite for any peer-discovery directory being trustworthy.

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
