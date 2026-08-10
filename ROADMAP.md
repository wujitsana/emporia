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
the above. An N11 pass then closed the second unsigned peer-gossip path (task #8 — lobby gossip
inverted from push to signed pull, same model `GET /gaming/v1/federate/listings` already used; see
`SECURITY.md`'s N11 section). Remaining: guardrails' regex-bypass limitation. See `SECURITY.md` for
severity and fix direction.

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
  interacts with federation trust). A2A's arrival (N10, see `## Interop` below) added a third
  concrete reason without changing the sequencing: a spec-conformant AgentCard built on `agent_id`
  would publish a known-collision-prone, relay-local string to the outside world as if it were
  stable and portable — precisely the promise A2A interoperability makes and this string can't keep.
  Confirmed from Hermes's real AgentCard builder that A2A supplies no answer here either — no DID, no
  pubkey, no `nous_user_id`, nothing identity-shaped in the schema at all.
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

**MCP server and `agent_sdk.py` independently re-implement the same relay calls — and the gap
kept growing across every hardening pass since.** Both wrap the same REST endpoints with separate
HTTP client code; a bug fix has to be applied in both places by hand, and each pass so far has
found at least one place where it wasn't (most recently N3-8: `/rooms/{id}/message` and
`/messages`' signed envelope changed shape, needing the identical construction updated
independently in `relay_server.py`, `agent_sdk.py`, and `mcp_server.py`). The N2 pass added
`_bearer_request`/`_invalidate_bearer` to *both* files with the same retry semantics, plus DM,
room-creation, Agora, and payment-intent method families to `agent_sdk.py` that `mcp_server.py`
already had in `httpx` form. The N3 pass (N3-7) made it worse in the other direction: fixing
`mcp_server.py`'s `create_room`/`list_rooms`/`send_room_message` to stop bypassing the relay
meant rewriting them to forward via `_bearer_request` — duplicating logic `agent_sdk.py`'s
equivalents already had, rather than sharing it (see `SECURITY.md`'s N3 pass, R6). Every future
pass that touches a write endpoint's contract will keep paying this tax until the refactor below
happens.
- *Why deferred:* a real refactor (MCP tools delegate to `EmporiaAgent` methods) touches the
  request/response shape of every MCP tool — needs its own testing pass, too risky pre-deadline.
- *Effort:* medium-large.

**Two independent `GameRegistry` instances** (MCP-local SQLite vs. relay) reconciled only by
manual `export_challenge`/`import_challenge` MCP calls — no automatic sync, no conflict
resolution if two nodes mint the same challenge ID. **Done (N11-5, task #8): the MCP
`sync_lobby_from_peer` gossip path into this local registry now verifies a signed batch** from
`GET /gaming/v1/federate/lobby` before importing — same signature/TOFU-pin model
`/gaming/v1/federate/listings` already used, reusing one shared canonicalizer
(`identity.federation_signable_bytes`) rather than growing a second copy. `publish_challenge_to_peer`
(the push-model half, which could never authenticate) is retired in place. See `SECURITY.md`'s N11
section. **Narrower again after N12 (N12-2/N12-3): a signed batch can no longer rewrite or cancel
a challenge it doesn't own** — `challenge_id` is now content-addressed and verified on import, and
a batch reusing an existing id under a different creator or a different `origin_relay` than the
peer it came from is rejected, closing the gap N11-5's signing alone had reopened (see
`SECURITY.md`'s N12 pass). What's still open here is genuine automatic-sync/conflict-resolution —
no merge logic if two relays *both* have legitimate standing to update the same challenge and
diverge — not signing or ownership, which are now enforced. **Narrower again after N14 (N14-1/
N14-2): a stored row now always re-verifies.** Through N13, `import_challenge()` verified the raw
incoming payload but stored a separately-normalized record, so a card with a legacy `payment_mode`
or a missing `currency`/`created_at` could pass verification once, at import, and then never verify
again — the export→import chain the module exists for was conditionally broken for exactly the
cards most likely to hit it. Content-address verification now runs against the record that will
actually be stored, and the method returns what `_upsert` actually persisted rather than what it
constructed. See `SECURITY.md`'s N14 pass.
- *Effort:* medium — would need either a single source of truth or a reconciliation pass.

**Resource cleanup — done (N11-3).** ~~dead WebSocket connections are only pruned on a failed
send, not proactively~~ — that claim was stale: every WS handler already discards its own socket
via `finally` on disconnect. The real gap was that `ws_connections`, `_RATE_WINDOWS`, the
per-agent rate-limit dict, and dashboard `_PENDING_TOKENS` all left their *key* behind once its
value emptied out or expired — one permanent entry per IP/session/agent/challenge ever seen, for
the life of a process meant to run for months. `_sweep_expired_state()` (the relay's first
background task, run on `EMPORIA_SWEEP_INTERVAL_SECONDS`, default 300s) now evicts all four. No
cascade-delete path exists for an admin to remove a session and its
participants/actions/payments/settlements/audit files together — that piece is still open.
- *Effort:* small — an admin-only cleanup endpoint.

## Payments

**P1b entry point: the x402 rail opens after P3, a rail-seam pass, now that P1a, P1r, P2, and P2r
have hardened the rail-agnostic payment core (SECURITY.md's P1a/P1r/P2/P2r passes).** P1r found and
fixed two regressions P1a itself shipped; P2 then found the premise underneath both was itself
wrong — a room/Agora entry fee was charged *per join* when it should buy *access* until an optional
window expires or the agent is kicked; **P2r then found five defects in P2's own eight commits**,
two of them live product breakage (see SECURITY.md's P2r pass). The entry model is now correct.
The payment plumbing is not yet ready for a second rail: "a payment" is spelled `payment_intent_id`
in roughly 150 places across the codebase, including `access_entitlements.payment_intent_id`, a
column P2-3 created *this arc* for the table meant to be rail-agnostic. **P3 (not yet planned in
detail) is a seam pass with no x402 code** — make the payment reference rail-tagged and the
settlement/payout step pluggable, proving it by leaving Stripe's behavior unchanged — so x402 lands
as an additive rail instead of a fork of every call site. Sequencing: **P2r (done) → P3 (seam,
unplanned) → P1b (x402, this entry).**

**Payout has no x402 analogue under the custody model chosen.** Decided this arc: x402 settles
**direct to the creator, with the relay taking a cut — non-custodial**, not mirroring Stripe's
"relay holds funds, then transfers out" flow. That means `payout_winner()` (a Stripe Connect
transfer out of a relay-held balance) isn't a second-implementation problem, it has **no
counterpart**: on x402 the payment *is* the settlement, the platform's fee must be enforced by the
payment request itself rather than deducted afterward, and `settlements.transfer_id`/
`transfer_status` describe a Stripe-only hop. The open mechanism question this creates: a single
x402 payment pays one `payTo`, so taking a cut without custody needs either two payment
requirements, a splitter contract, or facilitator-side support — decide this alongside P3's design,
not as an afterthought.

**Composable payment options, on both sides.** Also decided this arc, as a design principle for P3
and P1b: agents should not have a rail/chain dictated to them, and a relay operator should be able
to declare what it accepts rather than the platform hardcoding one option. This maps directly onto
x402 v2's actual `accepts` array in the 402 response — that array **is** the operator's declared
acceptance list (chains/assets/rails), and the paying agent's wallet picks from it. If adopted, the
non-custodial fee-split question above has to be solved per accepted option, not once.

**A public paid Agora topic's entry fee currently gates nothing — resolve before scoping x402 to
Agora subscriptions.** Pinned, not fixed, in P2r-5 (SECURITY.md's P2r pass, R5):
`_topic_can_read()` returns `True` unconditionally for `visibility="public"` topics before any
entitlement check, and every paid topic in the seed script and the test suite uses that visibility
— so a never-paid stranger can currently read and post on one. Whether `"public"` should mean
"readable by anyone" or "readable by anyone who's paid" is a product decision nobody's been asked;
x402 would be selling access on this exact route, so answer it before, not during, that pass.

x402 is confirmed (live docs, checked in the P1a pass) to be the
Coinbase-originated, Linux-Foundation-formalized HTTP-402 protocol settled in stablecoins (USDC),
distinct from Stripe's MPP, which `payments.py` already implements — the two share the "HTTP 402"
mechanism, not a protocol. Scope per the user, ahead of P1b's own review-and-plan round (do not
start from this entry — confirm the x402 spec against live docs again at implementation time, spec
was still moving as of the P1a review):
- **x402 is additive, not a Stripe replacement.** Session escrow (`capture_method="manual"`,
  hold-then-conditionally-release-minus-fee) has no analogue in x402's request-scoped settlement
  model — it stays on Stripe. Scope x402 to the two `capture_method="automatic"` pay-to-access
  routes first: `/rooms/{id}/join`, `/agoras/topics/{slug}/subscribe`. After P2/P2r, both buy an
  **entitlement** — a better fit for request-scoped settlement than per-join payment ever was.
- **No Hermes x402 skill or implementation exists to reuse** (checked in the P1a pass). One relevant
  first-party data point: Nous Portal's own inference API already accepts an x402 payment header
  (Solana USDC, beta, anonymous, per-request) as an alternative to a static API key — Portal is the
  x402 *consumer* side, Emporia would be the *resource server* side, no shared code, but it answers
  the network question absent a chosen-by-the-payer model (see "composable payment options" above):
  **Solana USDC, not Base**, is the ecosystem-native default if only one is offered.
- `payment_mode="x402"` — done in P1a-4: the old `"stripe_link"` alias is gone, `"x402"` now rejects
  outright with a message naming the reason, and is deliberately not yet added to `PaymentMode`.
  x402 v2 moved payment data into headers (`PAYMENT-REQUIRED`/`PAYMENT-SIGNATURE`/`PAYMENT-RESPONSE`,
  deprecating `X-*` forms) — keep it a fully separate code path from `build_mpp_challenge()`'s Stripe
  MPP headers, not one function with a flag.
- **Stripe Connect stays available but should not be the default/main payment mode** going forward —
  revisit `_normalize_payment_mode()`'s no-mode-given default (still `"stripe_link"` for any staked
  challenge, left unchanged in P1a since there's no second rail to demote it to yet).
- x402 v2's wallet-controlled sessions / `SIGN-IN-WITH-X` are the primitive that could finally close
  the one payer-binding gap P1a left open — see the row below.

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

**`_assert_payment_intent_matches()` validated amount/currency/capture-method/resource binding but
never the payer — done in P1a-1 for the authenticated path.** The N4 security review's R1 finding
was a working payment bypass for paid Agora topics built directly on this gap. Both halves of that
specific exploit were already closed; this underlying gap was not, until now: a payer check against
`metadata[buyer_agent_id]` (already stamped by both PI-minting paths) closes it on the direct-
`payment_intent_id` branch, where the caller is genuinely authenticated. **What's still open, and
genuinely needs x402/P1b:** the MPP/SPT branch. `confirm_spt()` mints its PaymentIntent from the
caller's own claimed, unverified `agent_id` in the same call — comparing them there is circular, not
a fix, so the three `_KNOWN_VIOLATIONS` routes stay flagged for that branch specifically. x402 v2's
wallet-controlled sessions could supply the verifiable-payer-identity credential this branch
actually needs. See `SECURITY.md`'s P1a pass (R1) and the correction to N2's own row.
- *Effort remaining:* large — payer-verified MPP/SPT credentials are core x402/P1b work, not a
  standalone patch.

**~~`record_settlement()`'s `INSERT OR IGNORE` is idempotency theatre~~ — done in P1a-2/P1a-3, then
corrected in P1r-1/P1r-2.** It minted a fresh UUID primary key on every call before the insert, so
`OR IGNORE` had no key to collide on, and `payout_winner()` had no idempotency key of its own. The
N5 security review's R2 finding (a retry after a mid-flight crash could reach `payout_winner` twice)
was fixed with ordering + an early short-circuit — deliberately not with real idempotency at the
time. P1a-2/P1a-3 made it real, but keyed only on `session_id||':'||COALESCE(payer_id,'')` —
*(resource, payer)*, not *(resource, payer, payment)* — so a payer's second genuine payment for the
same resource (rejoin after a kick, resubscribe after an unsubscribe) collided with their first and
was silently dropped, taking the second payout with it. P1r-1 fixed the key itself
(`payments.settlement_dedupe_key()`, folding a hashed digest of `payment_intent_ids` into the key);
P1r-2 rewired every `Idempotency-Key` derivation (payout ×2, `settle()`'s refund payout) through the
same helper so storage and Stripe agree by construction. See `SECURITY.md`'s P1a pass (R2/R3) and
P1r pass (R1/R3). **P2 correction:** "rejoin after a kick, resubscribe after an unsubscribe" as
described here was itself the wrong premise — by product decision a rejoin/resubscribe within an
active entitlement window is now *free*, not a second genuine payment; P1r-1's dedupe key still
matters for the case that remains genuine (a rejoin *after* the window expires). See `SECURITY.md`'s
P2 pass (R1) and `entitlements.py`.

**~~`settlements` has no payer column~~ — done in N7-2.** This entry previously claimed a
`payer_id` column was "blocked on the migration runner P4 already tracks." That was wrong — this
codebase already had an additive-column migration pattern (`agora_topics.gate_type`/
`entry_fee_cents`, three columns on `authorized_agents`), and `_migrate_settlements()` follows it
exactly: `PRAGMA table_info` check + `ALTER TABLE ... ADD COLUMN`, with pre-existing rows backfilled
from `payments` in the same migration. `join_room`/`subscribe_agora_topic` now populate `payer_id`
at write time; the session `settle()`/refund path leaves it `NULL` (its aggregate-row-per-session
model has no single payer). `_caller_owns_settlement_row()` is now a plain column comparison for
room/Agora rows instead of the `payments`-table join it used in N6-2. See `SECURITY.md`'s N7
section (R1/R2) — the incorrect "blocked on a migration runner" claim itself was N7's R6 finding.

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

## Interop

**The relay does not speak A2A (Agent2Agent), and deliberately won't for now.** Hermes added A2A
support (v1.0, client + server) after this project started; `/.well-known/agent.json` had already
borrowed A2A's URL convention and the term "Agent Card" for a custom, non-conformant document — never
real A2A. Investigated (N10) rather than assumed: a relay is a service, not an agent, and its entire
value is being the one enforced door every economically meaningful action passes through
(`ARCHITECTURE.md`'s "Inbound processing order (hard contract)" — guardrails → signature → payment →
proof-of-reasoning → audit, no shortcuts). A2A's default shape is the opposite: an individual agent
becomes directly reachable, with no mandated intermediary — general-purpose agent-to-agent
interoperability, a different problem than Emporia's shared-trust marketplace. A relay-side JSON-RPC
adapter would be a second door into a building designed around one.

**A2A reachability at the agent layer already surfaces Emporia's tools today, for free — no relay
work exists or is needed for this.** Hermes ships a complete A2A v1.0 implementation
(`/opt/hermes/plugins/platforms/a2a/`) separate from this repo. MCP servers register as toolset
`mcp-{name}`, and Hermes's default (no `A2A_ADVERTISED_TOOLSETS` set) advertises every registered
toolset as a Skill — so any Hermes agent with `mcp_servers.emporia` configured and A2A enabled
already exposes Emporia's tools to A2A peers, with zero Emporia code involved. Whether to turn this
on for a given agent is a security-posture decision (see `SECURITY.md`'s new threat-model note), now
surfaced by the installer (N10-5) but not decided by it.

**N10 fixed the one concrete defect this created: Emporia was squatting A2A's reserved discovery
path with a document that wasn't a real Agent Card.** Confirmed via Hermes's own A2A client source,
not assumed from the naming: a real A2A client's discovery fallback (`/.well-known/agent-card.json`
→ `/.well-known/agent.json`) accepted Emporia's non-conformant 200 at face value instead of correctly
concluding "not an A2A peer," then failed trying to POST JSON-RPC at the relay root. The descriptor
moved to `GET /relay-card`; both well-known paths now hard-404. A compatibility alias at the old path
was considered and rejected — an alias that still returns 200 leaves the exact defect open, just
delayed.

**A full relay-side A2A JSON-RPC server (real `skills`/`supportedInterfaces` AgentCard, Task
lifecycle, SSE streaming, push webhooks) stays deferred**, and only becomes justified if the goal
changes from "Hermes-operated agents for known relays" (current scope) to "non-Hermes, non-SDK agents
must transact against Emporia directly" — effort comparable to the x402/MPP re-platform below.
- *Effort:* large, same order as x402/MPP — not worth scoping until the goal actually changes.

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
