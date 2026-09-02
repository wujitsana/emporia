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
- **Closed by P28-3, and the entry above understated it.** `agent_sdk.py`'s `listen_agent()` was
  already fixed (N6 review R4); only `tunnel.py` still sent `?agent_id=` alone — and that param is
  honoured solely for hosts in `_HEADER_AUTH_TRUSTED_HOSTS`, which is **empty** unless the operator
  sets `EMPORIA_TRUST_LOCAL_HEADER_AUTH=1`. That is not the shipped default, so the tunnel
  authenticated nowhere at all, co-located or not, rather than merely lacking a path to a remote
  relay. It now mints `?token=` through `EmporiaAgent`'s existing challenge/session flow — reusing
  that machinery rather than restating it — rebuilds the URL per reconnect attempt (a token that
  expired mid-outage could otherwise never recover), and re-mints once per cycle on a rejected
  handshake. See SECURITY.md's P28 section. **P29 correction:** that re-mint was throttled by a
  once-per-cycle flag copied from `listen_agent()`, which is safe there only because it has a
  `max_retries` budget that bounds the flag's life. The tunnel retries forever, so a first failed
  re-mint left the flag set permanently and the tunnel replaying a refused credential until the
  process restarted. Now re-mints on every rejected handshake, throttled by the backoff already
  present.

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
happens. **Closed (P7-2, finished P8-2).** `agent_sdk.py`'s three paid-entry methods —
`join_session`, `join_room`, `subscribe_agora_topic` — used to call `raise_for_status()`, so
P1brr-2's `declined` reason key (added to `mcp_server.py`'s equivalents in P4-3) was only reachable
by catching `HTTPStatusError` and re-parsing — same underlying information, two different shapes,
across the two clients. P7-2 fixed `join_session()`; P8-2 finished the other two, which had traded
that cross-client asymmetry for an intra-client one (P7-2 shipped, P7's own review missed that its
two siblings were structurally identical and still had the gap). All three now raise
`PaymentRequiredError` (a small `httpx.HTTPStatusError` subclass, `.detail`/`.www_authenticate`)
via one shared `_raise_for_status_or_payment()` helper, instead of a bare `HTTPStatusError` on 402
— without touching the SDK's raise-on-error contract for its other 37 methods. See `SECURITY.md`'s
P7 and P8 passes.

**De-risked in P5 — the estimate below understated a real blocker, and a better decomposition
exists.** The stated direction ("MCP tools delegate to `EmporiaAgent` methods") assumes a mechanical
wrap; it isn't one. `mcp_server.py` is **52 sync** tool functions (module-level `httpx.get`/`.post`);
`agent_sdk.py` is **40 async** methods (`await self._client()...`). Delegating one to the other needs
a sync→async bridge in ~52 places — architectural work, not the small-medium lift "medium-large" was
scoped against.

**Shared wire-contract module — done (P6).** Every duplication bug this entry actually cited
(N3-8's envelope shape, N2's bearer/retry semantics) was in *request construction* — what bytes to
send — not transport, and all of it was already pure and synchronous in both files:
- the dashboard challenge/sign/session handshake (`mcp_server.py`'s `_dashboard_bearer_headers()` vs
  `agent_sdk.py`'s `_bearer_headers()` — both `sign_raw(nonce, …)` → the same request shape);
- the registration handshake (`mcp_server.py`'s and `agent_sdk.py`'s `register()` — both
  `{agent_id, public_key_hex, display_name, challenge_id, challenge_signature}`);
- the signed action/message envelopes (`agent_sdk.py`'s `_sign()` call sites — the N3-8 shape lives
  here, independently reconstructed in `mcp_server.py` and `relay_server.py` too).

`src/emporia/wire.py` now owns all four as six pure, synchronous functions (no `httpx`, no
`identity` import — signing and the actual HTTP call both stay in each caller); both `mcp_server.py`
and `agent_sdk.py` call the same functions rather than building the dicts inline. Verified by the
existing 404-test suite passing unchanged (no existing assertion needed editing) plus new
single-sourcing and real-signature-round-trip tests per function. See `SECURITY.md`'s P6 pass.

**Verifier-side adoption — done (P10).** P6 unified the two *clients* on `wire.py` and left the
*verifier* — `relay_server.py`, the copy that actually decides whether a signature is valid —
hand-building its own copies of the same three envelope shapes (action, room message, and a fourth,
previously uncovered DM-routing envelope). N3-8's original framing was explicitly a three-file
duplication (`relay_server.py`, `agent_sdk.py`, `mcp_server.py`); as of P10 all three call the same
`wire.py` functions, so the shared wire-contract module is complete across all three files, not just
the two clients. Also folded in: `mcp_server.py`'s `confirm_delivery`/`dispute_delivery` (missed by
P6-4 despite being in the file P6-4 was editing) now call `wire.signed_action_payload()` too, and the
default rationale strings the signer and verifier each hand-copied (load-bearing for signature
verification) are now single-sourced constants in `wire.py`. **Correction (P11-1): the confirm side
of that last claim was wrong at the time it was written** — `relay_server.py`'s
`ConfirmDeliveryRequest` still carried a third, different hardcoded rationale default as its pydantic
field default, which shadowed `wire.py`'s constant on the common path (a client omitting the optional
field) and made it dead code there. P10-2's own test didn't catch this because it drove
`confirm_delivery` through `mcp_server.py`, which always resolves and forwards `rationale` explicitly
— the one path that can't exercise the field default. Fixed in P11; see `SECURITY.md`'s P11 pass.
**Second correction (P12): P11's own fix for this had the identical shape one level up** — it shipped
the source fix but skipped its own plan's test fix, so the verifier-side guarantee it was fixing went
unpinned by any test for a full pass (P11's diff touched zero existing test lines). Also extended the
same single-sourcing to room-message `msg_type`, the third hand-copied-default site of this shape
(`mcp_server.py`, `agent_sdk.py`, `relay_server.py`'s `SendRoomMessageRequest` field default) — benign
today, all three literals agreed, but the same reachable-by-a-raw-client shape as the confirm bug. See
`SECURITY.md`'s P12 pass.

**Two gaps surfaced at P11, recorded then, fixed at P13 — both were design questions, not
oversights.** `agent_sdk.py` had no `confirm_delivery`/`dispute_delivery` methods at all, unlike
`mcp_server.py`'s pair — a real MCP/SDK parity gap, found while checking how far P10-2's
confirm-rationale bug could reach (answer: not into the SDK, because there was no SDK path to reach
through). Now added, mechanically mirroring `mcp_server.py`'s pair. Separately,
`POST /dm/{thread_id}/send` (`relay_server.py`) had no `signature` field on its request model and
never called `verify_signature`, while both sibling message routes (`POST /messages`, room messages)
require one — the decision made at P13 was to require one here too, breaking any unsigned client,
the same call this codebase already made for the other two routes. See `SECURITY.md`'s P13 pass for
both.

**Transport dedup — still open, optional.** Leaving the two transports (sync `httpx` calls vs.
`AsyncClient`) alone was the right call: it kills the drift class this entry cared about with zero
async bridging, and P6 confirms it — every duplication bug found in this overlap, before and during
that pass, was in request construction, none in the transport difference itself.
- *Why deferred:* a real refactor (MCP tools delegate to `EmporiaAgent` methods) touches the
  request/response shape of every MCP tool — needs its own testing pass, too risky pre-deadline.
- *Effort:* large, and optional now that the shared-contract half has shipped. (Revised from a
  single "medium-large" figure, which didn't account for the sync/async split — see `SECURITY.md`'s
  P5 pass.)

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
background task, run on `EMPORIA_SWEEP_INTERVAL_SECONDS`, default 300s) now evicts all four.

**Cascade-delete — done (P4-5, hardened in P5-2/P5-3).** `DELETE /admin/sessions/{session_id}`,
operator-only, hard-deletes a terminal session's `participants`/`actions`/`messages`/`sessions` rows
in one transaction. This entry used to say "participants/actions/payments/settlements/audit files
together" — that overstated the shipped scope on purpose, not by oversight: `payments`/`settlements`
rows (the record that real money moved) are never deleted, only counted and reported, and the audit
trail is never rewritten (`_write_audit_line()` appends to a date-keyed log, not a per-session file —
stripping lines would break its append-only integrity property). One audit line is appended recording
the deletion itself, written **before** the row deletes run (P5-3 — a failed audit write now aborts
the request with nothing touched, rather than deleting with no record). P5-2 also closed a real gap
P4-5 left: a room linked to the deleted session (`rooms.linked_session_id`) used to dangle, pointing
at nothing — now cleared (not the room itself deleted) and reported as `"unlinked": {"rooms": N}`.
See `SECURITY.md`'s P4 and P5 passes.
- *Effort:* small — an admin-only cleanup endpoint. (Confirmed accurate in hindsight.)

**`sessions.status` vocabulary bug — done (P5-1).** Found while auditing P4-5's terminal-status
constant: `/health` and the dashboard WS both reported `session_count` using a query
(`WHERE status NOT IN ('complete','cancelled')`) that matched no real status value ever written
(`waiting`/`active`/`completed`/`abandoned`), so the count silently included every completed and
abandoned session as if still active — a user-visible wrong number, pre-existing, not introduced by
P4-5. `abandon_session`'s own re-abandon guard had the same bug and was dead code as a result. All
three (plus `mcp_server.py`'s status docstring) now read from one `_SESSION_STATUSES`/
`_SESSION_TERMINAL_STATUSES` pair. See `SECURITY.md`'s P5 pass.

## Payments

**P46 found a Stripe endpoint that does not exist, called since before the hackathon.**
`confirm_stripe_intent()`'s test-mode branch POSTed to
`/v1/test_helpers/payment_intents/{id}/confirm`; Stripe serves no such route (probed directly, with
a real `test_helpers` route as the positive control). So the test-mode auto-confirm advertised in
the function's docstring and in the dev skill's payment-testing table had never worked, on a path
with zero test coverage. Fixed to the one endpoint both modes actually use. The general lesson,
after P45: **a mocked HTTP layer structurally cannot have an opinion about the URL** — an
unauthenticated probe of a vendor's routing table costs nothing and is worth doing wherever this
repo hand-rolls a wire format, which is both payment rails. Lanes 2 (PaymentIntent) and 3 (MPP/SPT)
now exist on the `testnet` tier; the SPT is minted from Stripe's own test helper rather than
recorded, so the lane runs unattended. **Neither has been run** — no test key was available where
they were built; `docs/testnet.md` records that rather than implying green.

**P45 gave the x402 rail a payer, and found that it had never been payable.** P1b built the seller,
P1br and P1brr audited it, and 320 test references covered it — all seller-side, all with a mocked
facilitator, because nothing in this repo could produce an x402 credential (`mcp_server.py:51`
records the fork that stripped the signing half). Inside that gap: the challenge omitted
`PaymentRequirements.extra`, which on EVM carries the token contract's EIP-712 domain. P1brr had
noted `extra` was never sent and classed it a known unknown, reasoning from the spec's "Optional"
marking — accurate about the protocol, wrong about the mechanism. An `exact`-EVM payer has nowhere
else to read the signing domain from, so every challenge this relay ever issued was well-formed and
unsignable. `EMPORIA_X402_EXTRA_<NETWORK>` fixes it; a network configured without it is excluded
from `accepts` rather than advertised unpayable. `src/emporia/x402_payer.py` is the payer (optional
`x402` extra — the relay verifies and never signs), and `EmporiaAgent(..., x402_private_key=...)`
plus a per-call `pay_with_x402=True` is the SDK path. Verified end to end against the live public
facilitator on Base Sepolia, with tampered-credential controls; see SECURITY.md's P45 pass and
`docs/testnet.md`.

**The lesson, one level down: a mocked counterparty tests the half you wrote.** P44's shape was a
cost nothing measured. P45's is a *protocol half nothing implemented* — the audit passes were not
sloppy, they were complete over a surface that was only ever half the loop, and the missing half was
where the defect lived. The opt-in `testnet` tier exists so that at least one test per rail has a
counterparty this repo did not write.

**Still open after P45:** x402 has no `Idempotency-Key` equivalent on `/settle`, so a relay crash
between settlement and `record_payment()` can charge twice for one purchase. Recorded since P1b in
`_verify_x402_payment()`'s docstring; P45 makes it reachable rather than theoretical and does not
close it. Doing so needs either a facilitator-side idempotency contract that does not exist, or a
relay-side pre-settlement intent record — the latter is a real design, not a patch.

**P1b, the x402 rail, is done** — built on P3, the rail-seam pass (SECURITY.md's
P1a/P1r/P2/P2r/P2r-6/P3 passes hardened and then rail-tagged the payment core). P1r found and fixed
two regressions P1a itself shipped; P2 then found the premise underneath both was itself wrong — a
room/Agora entry fee was charged *per join* when it should buy *access* until an optional window
expires or the agent is kicked; P2r found five defects in P2's own eight commits, two of them live
product breakage; P2r-6 found one more (see SECURITY.md's respective passes). The entry model is
now correct. **P3 then closed the seam**, five commits: `gate_type: "stripe_payment"` renamed to
`"paid"` (breaking, no alias — a rail name baked into an agent-facing enum was actively wrong under
a second rail); a `rail` column added to `payments`/`settlements`/`access_entitlements` — most
glaringly `access_entitlements.payment_intent_id`, the column P2-3 created *that arc* for a table
meant to be rail-agnostic — with one canonical vocabulary (`payments.RAIL_STRIPE`/`KNOWN_RAILS`);
the single funnel `_verify_and_confirm_payment()` now returns a `PaymentRef(rail, reference)` instead
of a bare string, threaded to every persistence point; the two copy-pasted room/Agora payout blocks
collapsed into one `_settle_access_payment()` seam, drawn where custody actually splits; and
`/health`/`GET /agents/{id}`'s independently hand-built `payment_rails` lists factored into one
`_payment_rails()` helper (see SECURITY.md's P3 pass for the full findings, including a
previously-undocumented divergence the last refactor surfaced and pinned rather than fixed).

The "roughly 150 places" figure this entry used to carry was directionally right as an *identifier*
count (156 in `src/`, confirmed by the audit that opened P3) but overstated the actual seam: only
~30-35 of those are genuinely Stripe-coupled (essentially all of `payments.py`'s HTTP layer, plus a
handful of capture-method/PI-status/Connect sites in `relay_server.py`). The other ~120 were always
opaque tokens — stored, compared, redacted, never parsed — which is exactly why P3 could tag the
reference at one funnel instead of touching 150 call sites. Sequencing: **P3 (done) → P1b (done) →
P1br (done, the P1b self-review) → P1brr (done, the P1br self-review, and it discharges the wider
P1b sweep P1br's own coverage caveat left owed) → P4 (done — closes the x402 review chain and ships
the cascade-delete endpoint) → P5 (done — the P4 self-review; found the pre-existing
`sessions.status` bug P4-5 walked past, hardened the cascade-delete endpoint, and de-risked the
MCP/SDK refactor below) → P6 (done — the P5 self-review, plus the shared wire-contract module the
MCP/SDK refactor above needed) → P7 (done — the P6 self-review; closed a count TOCTOU P6-1's own
lock-scope fix reopened, and closed the join_session 402-shape gap P5 had left owed) → P8 (done —
the P7 self-review; compensated a false audit line P7-1's own fix could leave behind, and finished
the 402 parity P7-2 started across all three paid-entry SDK methods) → P9 (done — the P8
self-review, the smallest finding yet in the `delete_session()` arc, plus a research-only
Federation/Interop/Rooms/Dashboard survey that found every open item there still correctly scoped;
the P4–P9 self-review chain was deliberately closed here, seven straight passes each finding a
defect in the previous one's `delete_session()` fix until P9 found none, confirmed clean on an
eighth look) → P10 (done — **not** a continuation of that chain, a different pass reviewing
everything P6–P9 shipped as one body of work for duplication debt those passes created and didn't
fully discharge: finished the MCP/SDK wire-contract unification onto the verifier side, single-sourced
the confirm/dispute default rationale strings, made `subscribe_agora_topic()`'s response shape match
its two siblings, and collapsed `delete_session()`'s count queries — see `SECURITY.md`) → P11 (done —
resumed the audit-the-previous-pass pattern, applied to P10's own work: found and fixed a genuine gap
in P10-2's own single-sourcing claim, the same N4-7 unguarded-`sign()` defect recurring in three
siblings P10-1 edited without noticing, and the non-atomic/non-private secret writes that made that
defect reachable — see `SECURITY.md`) → P12 (done — audited P11's own work and found the same shape
one level up: P11 shipped item 1's source fix but skipped its own plan's test fix, so the
confirm-rationale verifier guarantee went unpinned for a full pass; also a wrong recovery message on
one of two corrupt-key callers, the same hand-copied-default shape recurring a third time in
`msg_type`, and one best-effort contract that could still raise — see `SECURITY.md`) → P13 (done —
not another review pass, the design-question follow-through P12 called for: signed
`POST /dm/{thread_id}/send` (the last unsigned content-bearing message route), added
`agent_sdk.py`'s `confirm_delivery()`/`dispute_delivery()` (closing the MCP/SDK parity gap), and gave
federation peer-pin corruption its own policy — log loudly, preserve the evidence, don't raise (fail
loud wasn't safe without also adding exception boundaries in `relay_server.py`'s federation-sync
loop; self-heal would have been the invisible-TOFU-reset bug wearing a fix's clothes) — see
`SECURITY.md`) → P14 (done — the review of P13, which P13 itself said shouldn't happen: a `msg_type`
that disagreed with itself across the DB row and the inbox broadcast, a guardrails scan P13 narrowed
on a premise that turned out to be factually wrong, and the cross-client body duplication P12-3 had
just finished clearing — see `SECURITY.md`) → P15 (done — not a review: the recorded-but-unfixed
pile P14 left. Enforced the `MsgType` contract `rooms.py` had *declared* since before the hackathon
but never checked at runtime (`wire.CLIENT_MSG_TYPES`, pinned against `rooms.MsgType` by a drift
test, enforced on the resolved value in both message routes — which also stops clients minting the
server-owned `"system"` label), hoisted `dm_send()`'s participant check above the guardrails scan,
and closed the broadcast sweep as a **finding of absence**: 19 call sites, one divergence, already
fixed at P14-1 — see `SECURITY.md`) → P16 (done — not a review: end-to-end encrypted DMs, built to
the requirement that the relay operator and anyone with relay access cannot read them. `crypto_box.py`
(X25519/HKDF/AES-GCM, content key wrapped per participant, AAD-bound), X25519 keys + TOFU peer pinning
in `identity.py`, key publication with an Ed25519 binding, one-way encrypted threads, and both clients
sealing/opening transparently — plus the pre-existing `INSERT OR REPLACE` column wipe it uncovered —
see `SECURITY.md`) → P17 (done — the review of P16, all five findings in code P16 wrote that pass: a
resolved-`msg_type` mismatch that made a message readable by nobody while returning 200, DM signatures
verified once and then discarded, a `decrypted` flag meaning two different things, the dashboard
rendering envelope JSON at humans, and — found by the live tamper drill *after* the suite was green —
a low-order `epk` escaping `crypto_box` as an uncaught `ValueError` that took the whole thread read
with it — see `SECURITY.md`) → P18 (done — room end-to-end encryption plus every recorded-but-fixable
item cleared: request/message size bounds (open since P15, deferred twice), DM replay closed with a
sender-chosen nonce enforced in the signature, the AAD and a UNIQUE index, and rooms moved from the
relay-side `encrypted` posture to real E2E with a one-shot member key bundle and a recoverable
membership race — see `SECURITY.md`) → next unnamed.** The `r`-suffix review-chain naming ends
at P1brr; review passes since are named sequentially (P5, P6, P7, P8, P9, P10, P11, P12, P13, P14, …)
rather than growing another suffix — review yield had shrunk to P1brr's own one Medium/three Low, the
signal to stop; P11 finding a live defect in P10's own work showed that signal doesn't mean stop
reviewing altogether, only that the *chain-of-fixes-in-one-function* shape (the P4–P9
`delete_session()` arc) had run its course.

**P13 declared the audit chain finished — "whatever comes next is a fresh body of work, not P14
auditing P13" — and P14 falsified that by three findings, two of them live.** The useful correction
isn't "keep auditing forever," though; it's that **every P14 finding sits in code P13 wrote that same
pass**, none in older code. Auditing keeps paying because each pass writes new code, not because the
existing code keeps rotting. The sustainable version of this is reviewing new work as it lands rather
than scheduling a pass to audit the pass — and the recorded-but-unfixed pile (broadcast-event
coverage generally, `msg_type` validation, guardrails-vs-participant ordering) is a better use of the
next pass than P15 auditing P14.

**P15 took that route, and the pile is now empty** — but the interesting part is what researching it
did to the items. Two of the three changed shape before a line was written: the broadcast sweep was
already clean (a finding of absence, and the reusable output is the risk signature — *a hand-built
event dict plus a value the handler resolved* — not the count of call sites), and `msg_type`
validation turned out not to be a new API-contract decision at all: `rooms.MsgType` had declared the
closed set the whole time, so the work was enforcing an existing promise rather than making a new
one. **What's left is the explicitly-large set** — transport dedup, peer-discovery directory, room
E2E encryption, dashboard write UI — each gated on a design decision rather than effort. The next
session is a product question ("which of the four?"), not a code-quality one; the first time that's
been true since P4.

**P16 answered that question with a fifth option the list didn't contain: end-to-end encrypted DMs.**
The requirement was stated as a threat model, not a feature — *the relay owner, and anyone with
access to the relay, must not be able to read encrypted DMs* — which ruled out encryption-at-rest and
every design where a relay-held key recovers plaintext. Six commits: a pure `crypto_box.py` (content
key per message, wrapped once per participant, AAD-bound to thread/sender/msg_type), X25519 keys and
TOFU peer pinning in `identity.py`, key publication with an Ed25519 binding at the relay, one-way
encrypted DM threads, and both clients sealing/opening transparently. Verified live against the
SQLite file, not just the API. Full write-up and the six honest limits — metadata, guardrails, no
forward secrecy, first contact, not retroactive, rooms unchanged — in `SECURITY.md`.

**Two things worth carrying forward from it.** First, the pass turned up a live pre-existing bug
nobody was looking for: `register_agent`'s `INSERT OR REPLACE` had been silently wiping
`x402_pay_to_json` on every re-register since P1b, quietly removing the x402 rail from that creator's
402 challenges. It surfaced only because the new encryption-key column would have been wiped the same
way — *designing a new column found a bug in an old one*. Second, `crypto_box.py` was written for N
recipients rather than 2, so **room E2E encryption is now a materially smaller item than it was on
the list above**: rooms today still have only the relay-side `encrypted` posture (store an opaque
blob, skip the scan), and the remaining work is wrapping the content key to the member set rather
than inventing the crypto.

**P17 audited P16 before anything was built on it, and that ordering paid.** Five findings, all in
P16's own new code, one of them (`crypto_box`'s uncaught `ValueError` on a low-order `epk`) in the
exact module room E2E would have been built on top of — it would have inherited the defect and made it
twice as expensive to find. **The audit-before-extend ordering is the transferable part**, not the
particular bugs.

**P18 emptied the recorded pile and finished the encryption arc.** Room E2E cost far less than it
would have before P16 — `crypto_box` already wrapped a content key to N recipients, so the work was
the member set, the trust plumbing and one genuinely new problem: **membership churns while a DM
thread's participants don't**, so a sender's key bundle can go stale mid-send. That is answered with a
409 naming the missing members and a single client retry, rather than either ignoring the race or
storing a message somebody can never read. One smaller thing worth remembering: the size bound had
been deferred twice on reasoning that was *correct in general* ("belongs at the proxy layer") and
wrong *here* (nothing runs in front of this relay) — a reminder that a true general principle can
still be the wrong local answer. **One claim in that pass didn't hold up: P18 asserted rooms didn't
need P18-2's replay nonce because `room_messages` already carried a hash chain — P19 (below) found
that chain is relay-computed and verified by nothing client-side, so room replay was fully open
through P18 despite the claim it was closed. "Checking what already exists" is only a substitute for
a fix if the existing thing actually does the job**, which nobody had verified it did.

**P19 audited P18 (five findings in five prior passes now — the pattern holds) and closed room
replay for real.** Three parallel reviews of P18's diff, ~25 findings total, the serious ones found
independently more than once. The headline items: the version bump C1/C2 forced anyway
(pre-P18 DMs were undecryptable and misreported as tampered) carried two more changes that would
otherwise have been breaking on their own — an explicit `kind` AAD tag (`"dm"`/`"room"`, replacing
an accidental id-prefix non-collision) and the room nonce that actually closes replay this time,
enforced the same three ways the DM nonce is (signature, AAD, `UNIQUE` index) plus a client-side
duplicate check on read. A member publishing an all-zeros X25519 key could brick `seal()` for an
entire encrypted room — P17-5 had guarded the read side of this exact class and the room probe that
"confirmed" it hadn't reappeared had only tested that same read side. And the negotiation broker ran
on encrypted-room content unconditionally: an envelope is a JSON dict, so it "looked like" an offer,
and on ACCEPT/COUNTER the broker wrote a **plaintext** system message into a room whose entire point
is that the relay holds no plaintext — verified live pre-fix. Full findings list and every live
verification in `SECURITY.md`'s P19 section. **Same lesson as P17, restated because it recurred:
audit the pass that shipped the most security-critical new code before building on top of it, and
verify a defense-in-depth claim actually works before writing it down as closed.**

**Two lessons worth carrying, both about method rather than code.** First, the `X or DEFAULT`
resolution shape has now produced a finding in four separate passes (P12-3, P13-1, P15-1, P17-1); the
generalized question — *whenever a handler resolves a default, who else computes that value
independently?* — is more valuable than any of the four fixes. Second, four of P17's findings came
from reading code and the fifth from a live adversarial drill run **after** the suite was green — and
the fifth was the most severe. For crypto, a live tamper drill is a distinct detector, not a
formality.

**P1b shipped: x402 is a real, working second payment rail**, non-custodial, eight commits
(SECURITY.md's P1b pass has the full findings). `_verify_and_confirm_payment()` gained an x402
branch (`PAYMENT-SIGNATURE` header), reconstructing the relay's own expected `paymentRequirements`
and asserting it against the client's claim *before* calling the facilitator — a tampered `payTo` or
amount is rejected locally. The 402 challenge now offers both rails at once (existing MPP fields
untouched; x402's payload nested under a nonclashing `"x402"` body key plus the spec-correct
`PAYMENT-REQUIRED` header). Creators register a wallet per network family (`POST
/agents/{id}/x402-wallet` — an EVM address and a Solana address are different key spaces, so this is
keyed, not a single field). Advertised on `/health`, `GET /agents/{id}`, and (newly) `/relay-card`.

**Payout has no x402 analogue under the custody model chosen — resolved for now, not permanently.**
x402 settles direct to the creator's registered wallet; `payout_winner()` (a Stripe transfer out of a
relay-held balance) has no counterpart and is never called on this branch. **The platform fee on
x402 is 0%** — `payTo` is the creator's wallet, they receive 100%, `transfer_status="direct"` records
that no transfer hop ever existed. This was the pragmatic call to ship a working rail rather than
build a cross-chain splitter for a rail that didn't exist yet to declare requirements against.
**Revisit when:** a splitter mechanism (or facilitator-side multi-recipient support) exists on both
configured networks. **Named risk:** if x402 stays cheaper for agents than Stripe's 2.5%, volume
could migrate off the fee-bearing rail — worth watching once real usage exists.

**Composable payment options — shipped.** `_payment_rails()` now includes `"x402"` in both variants
whenever the rail is configured, built from `EMPORIA_X402_NETWORKS`/`EMPORIA_X402_ASSET_<NETWORK>` —
the operator declares what's accepted, the 402's `accepts` array reflects exactly that, and the
paying agent's wallet picks from it. Both Base and Solana are supported simultaneously (register
both networks; see the corrected network-default note below) rather than one being hardcoded as *the*
option.

**Corollary, made explicit in the P1br self-review: once a payer has picked, the relay doesn't get to
override that pick.** P1b-5 shipped with a gap where a submitted `PAYMENT-SIGNATURE` credential that
couldn't be honored (x402 disabled, or a route with no x402 analogue) fell through to try Stripe
instead — silently substituting a rail the payer never chose. Fixed in P1br-1: the relay now declines
explicitly (the same merged-402 shape, so the agent sees what's actually accepted and can retry) and
never silently switches rails on the payer's behalf. **This rule applies to any future rail this
codebase adds, not just x402** — worth remembering as a design constraint before it's tempting to
"just fall through" again.

**Two things the P1brr self-review noted but deliberately left as-is** (SECURITY.md's P1brr section
has the full reasoning): a facilitator-side error surfaces as a 402 telling the payer to retry rather
than a 500 saying the relay's own backend call failed — matches the Stripe branches' existing
convention, not an x402-specific gap, and changing it is a cross-cutting API-contract decision, not a
review fix; and `entitlements.has_access(conn=…)`'s reused-connection parameter reads whatever
transaction snapshot the caller's connection already has, which no call site combines with a `grant()`
today but a future one should be aware of before doing so.

**A public paid Agora topic's entry fee gated nothing — resolved, provisionally.** Was pinned open by
P2r-5 specifically to block x402 scoping onto Agora subscriptions until answered. P1b-1: paid topics
now gate on the entitlement regardless of `visibility` — `"public"` comes to mean discoverable/listed,
not readable, matching the rule `_settlement_resource_viewer_allowed()` already applied to the same
topics (N5-R1) instead of contradicting it. **Revisit when:** there's real usage data on whether paid
communities actually want a free-to-read shopfront (a "public" that's discoverable-but-gated is a
narrower product than a fully open one, and nobody's validated agents want the narrower version).

x402's governance and spec **moved since the P1a check — re-confirmed against live docs during P1b
planning, corrections below.** It remains the HTTP-402 protocol settled in stablecoins (USDC),
distinct from Stripe's MPP (the two share the "HTTP 402" mechanism, not a protocol):
- **Governance actually moved, not just "formalized."** The canonical repo is now
  `x402-foundation/x402` (`coinbase/x402` is an explicit development fork); the Linux Foundation
  **operationally launched** the x402 Foundation 2026-07-14 under TSC-based open governance, 40
  member orgs including Coinbase, Cloudflare, Circle, Stripe, Visa, Mastercard, Solana Foundation.
  Coinbase contributed the protocol but no longer controls it.
- **v2 shipped 2025-12-09** — CAIP-2 network ids, payment data moved into headers
  (`PAYMENT-REQUIRED`/`PAYMENT-SIGNATURE`/`PAYMENT-RESPONSE`, deprecating `X-*` forms), restructured
  `PaymentPayload`, per-request dynamic `payTo`, a plugin (`@x402`) SDK architecture. P1b implements
  v2's header-based shape, not v1's.
- **Corrected: "Solana USDC, not Base, is the ecosystem-native default" was wrong.** Recent volume
  snapshots put Base and Solana roughly co-primary (Base larger in some snapshots, Solana in others);
  neither is uniquely "the" default. P1b offers both — the `accepts` array exists precisely so a
  payer's wallet picks whichever it holds, which is the actually-correct use of the protocol here
  rather than picking one network as canonical. Nous Portal's own consumer-side precedent (Solana
  USDC) is worth keeping as a tiebreaker where one must be picked, not as an exclusivity claim.
- **No native multi-recipient settlement in the protocol or any facilitator** (confirmed against
  live docs) — see the payout-mechanism note above; this is why the fee is 0% rather than split.
- **The official Python package (`x402` on PyPI, x402 Foundation, MIT) was evaluated and deliberately
  not taken as a dependency.** Its `PaymentMiddlewareASGI` runs blanket-early, before Emporia's own
  auth/membership/entitlement checks — it would charge agents who already hold a valid entitlement,
  the exact bug class P2 spent eight commits fixing. The wire format is small and fully specified;
  P1b hand-rolls it with raw `httpx`, the same posture `payments.py` already takes toward Stripe
  (`stripe>=9.0.0` is a declared dependency that's never imported).
- `payment_mode="x402"` — still rejected at `_normalize_payment_mode()` (P1a-4's message), and
  correctly so: that's the *session-escrow/stake-intent* vocabulary, a genuinely separate axis from
  the pay-to-access rail P1b built. Session escrow (`capture_method="manual"`,
  hold-then-conditionally-release) still has no x402 analogue and stays Stripe-only.
- **Stripe Connect stays available but should not be the default/main payment mode** going forward —
  `_normalize_payment_mode()`'s no-mode-given default is still `"stripe_link"`; revisiting it is not
  yet urgent with only one pay-to-access rail's worth of real usage.
- **x402 v2's `payer` field (returned by the facilitator's `/verify`) is a new opportunity to close
  the payer-binding gap P1a left open** — P1b's `_verify_x402_payment()` doesn't use it for binding;
  the three routes stay pinned in `_KNOWN_VIOLATIONS` pending that work. Binding a verified wallet
  address to an Emporia `agent_id` is its own design question (does one agent get one wallet? can a
  wallet be reused across agents? is the binding one-time or renewable?) — not taken up this pass.

**Stripe Connect transfers work end-to-end in both modes now — closed by P25-2.** Test mode still
auto-provisions and auto-accepts ToS at `register_agent()`, immediately usable, unchanged from
before. Live mode gets its own path: `POST /agents/{agent_id}/stripe-onboarding` creates a
Connected Account (no `tos_acceptance` auto-fill — that's Stripe's hosted form's job for a real
account) and a fresh Account Link; a verified `POST /webhooks/stripe` (hand-rolled
`Stripe-Signature` HMAC check) is the only thing that ever marks an account `'active'` once Stripe
confirms `capabilities.transfers`/`card_payments`. A new `authorized_agents.stripe_account_status`
column (`'none' | 'pending' | 'active'`) tracks this, surfaced on `GET /agents/{agent_id}` and
checked before every transfer attempt — a not-yet-active account gets
`transfer_status = "pending_connect_not_active"` instead of a doomed Stripe call. See SECURITY.md's
P25 pass for the full writeup, including the webhook signature verification and the 28 existing
tests whose Stripe-account mocks needed updating for the new status dimension.

**P26 then audited that work and found it shipped a money-stranding bug**, which is why "closed by
P25-2" above is accurate about the *feature* and was wrong about the *state migration*: P25's new
`stripe_account_status` defaulted to `'none'` with no backfill, so every agent registered before it
kept a working Connected Account while reading as unpayable — captured charges, no transfer, no
retry. P26 adds the backfill (run only when the column is newly added, so it can't promote a
legitimately-pending live account), a `stripe_account_mode` column closing the test-account-under-a-
live-key case, a `COALESCE` fix stopping a re-register from orphaning a real Connected Account, a
required real `email` plus an ISO `country` on live account creation, `account.application.
deauthorized` handling, and `livemode` filtering. It also made the webhook verifier total — two
malformed headers were reaching an unauthenticated 500 rather than a 403. Full findings in
SECURITY.md's P26 section.

**P27 audited P26 and found the backfill never reached the relays it was written for.** Gating it
on `stripe_account_status` being newly added meant it only fired on a P24 → P26 jump — but a relay
that ran P25 already *has* that column, holding the wrong value, because that mis-defaulting is the
bug. So the deployments actually carrying stranded money were the ones the fix skipped. P27 widens
the gate to the P25 → P26 upgrade and adds a `status = 'none'` predicate so the statement can't
touch a legitimately-pending account regardless of when it runs. P27 also found a *second*,
independent route to the same stranding, introduced by P26's own hardening: requiring
`card_payments` alongside `transfers` before an account counts as payable, when the payout is a
`/v1/transfers` into the account and needs `transfers` alone — pinning any transfers-approved,
card-restricted account (normal for one that only receives) at `'pending'` forever. Plus a 502 for
Stripe rejecting account creation, case/Unicode holes in the `email` and `country` guards, and
mode-stamping from the webhook's own `livemode` filter. Full findings in SECURITY.md's P27 section.

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

**P9 survey (research only, no code — see also Interop/Rooms/Dashboard below):** re-verified every
claim in this section directly against current source rather than trusting the prior text. All
hold. One thing worth recording that wasn't spelled out here before: the lack of an automatic
periodic federation pull is a **deliberate, already-documented design choice**
(`ARCHITECTURE.md`'s "Background tasks" section: "federation sync is pull-on-demand... not
scheduled by the relay itself" — the only background task that exists is `_sweep_loop()`), not an
oversight or a small gap. It would be easy to bolt on (`_sweep_loop()`'s exact pattern —
`asyncio.create_task` in `lifespan()`, an `EMPORIA_*_INTERVAL_SECONDS` env var, per-run exception
isolation) but doing so reverses a stated decision, not fills an unnoticed hole — not something to
pick up without first understanding *why* pull-on-demand was chosen (likely: keeping every relay's
outbound network behavior fully operator-controlled, no surprise traffic to peers). Not proposing it
as a candidate; flagging it so a future pass doesn't rediscover it as if it were new.

**No peer-discovery directory.** Two relays federate today only if each operator manually sets
`FEDERATED_RELAYS` to the other's URL. There's no bootstrap/registry mechanism for a node to find
peers automatically.
- *Effort:* medium-large — needs a design decision (centralized directory vs. DHT vs. seed-list
  gossip) before implementation. **Confirmed still accurate (P9):** no directory/bootstrap
  mechanism exists in `relay_server.py`; `FEDERATED_RELAYS` is still a manually-configured list.
  Still correctly the largest-effort item in this section — a design decision, not a coding task.

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
  before any of the actual multi-process federation logic. **Confirmed still accurate (P9):**
  `DATABASE_URL` doesn't appear anywhere in `src/emporia/` — the Postgres-backend prerequisite is
  still fully unbuilt.

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
  the agent process is sandboxed/hosted, not just app code. **Confirmed still accurate (P9):**
  `docker-compose.yml`'s `nemoclaw` profile is still commented out. Deliberately not picked up by
  this review chain regardless — it's ops/infra work needing external verification against
  NVIDIA's own docs, a different risk profile than this chain's usual read-the-code-write-the-fix
  passes.

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
  **Confirmed still accurate (P9):** the goal hasn't changed since N10; `/relay-card` and the two
  hard-404'd `.well-known` paths are unchanged in `relay_server.py`.

## Rooms

**P9 survey (research only, no code) — superseded by P18, below.** At the time, `rooms.py`/
`relay_server.py` had zero X25519 or other key-exchange machinery, and `encrypted` was exactly the
boolean flag the old text of this section described: skip guardrails, store opaque content, nothing
more. That was accurate through P17. It is not accurate now — kept here only so the "gated on a
design decision" reasoning below has its original context.

**Shipped (P18), then audited and hardened (P19): real end-to-end encryption for `encrypted=true`
rooms.** `crypto_box.py` (written for N recipients from P16 onward) wraps one content key per
member; `GET /rooms/{id}/encryption-keys` returns the whole member set's keys in one round trip so
a sender's wrap set can't drift mid-fetch; both clients seal and open transparently, and every peer
key goes through the same `identity.trust_peer_encryption_key()` DMs use, so the two features can't
apply different trust rules to the same key material. P19 then found and closed: room replay (the
hash chain this section used to point to as the anti-replay mechanism is relay-computed and
verified by nothing client-side — a per-message nonce, the DM mechanism mirrored, is what actually
closes it); a member publishing a degenerate X25519 key bricking `seal()` for the whole room; and
the negotiation broker running on ciphertext and writing a plaintext system message into an
encrypted room. Full detail in `SECURITY.md`'s P18 and P19 sections.
- **Chain verification — done (P21).** `rooms.verify_chain()` recomputes a room's chain from genesis;
  `GET /rooms/{id}/chain` signs the result with the relay's federation keypair (reusing
  `federation_signable_bytes()`, the same mechanism `GET /gaming/v1/federate/listings` already uses);
  `agent_sdk.py`'s and `mcp_server.py`'s `verify_room_chain()` recompute independently from the room's
  own messages and compare against a head pinned per room (same TOFU shape as the federation
  peer-pubkey pins) — the piece that actually catches a relay operator who rewrites history and
  re-signs a self-consistent replacement, which the signature alone cannot. See `SECURITY.md`'s P21
  section for the full design and a live drill proving the relay's own self-check is fooled by a
  self-consistent rewrite while the client-side pin catches it. Not built this pass: a dashboard
  badge — the security-relevant property lives in the Python-client pin, which has no dashboard
  equivalent (no persistent per-viewer trust store in a browser the way `~/.hermes/keys/` gives an
  agent), so a badge showing only the relay's own self-report would be a strictly weaker guarantee,
  not the one this pass built.
- **Dashboard chain badge + per-message verified/replayed — done (P23-4).** The opener P21 left
  above: `RoomChainBadge.tsx` ports `agent_sdk.py`'s `verify_room_chain()` to the browser
  end-to-end (fetch the signed head, verify the signature, fetch + recompute the full message
  history, compare against a head pinned in `localStorage` — the browser's per-viewer trust store
  analogue of `~/.hermes/keys/`), not a shallow echo of the relay's own self-report.
  `integrityCheck.ts` also ports `message_read.annotate_messages()`'s `verified`
  (Ed25519 signature, public-key-only, no decryption) and `replayed` checks for both rooms and
  DMs — `decrypted` stays deliberately unported (`api.ts`'s own long-standing "the dashboard
  cannot decrypt and never will" principle). See `SECURITY.md`'s P23 section for the
  cross-language golden-fixture tests this relies on.
- **Full-history chain verification, real replay detection, guardrail normalization — done (P24).**
  Closed the cap SECURITY.md's P21 section recorded as "still open" (`verify_room_chain()`'s
  1000-message limit meant a large room could silently never be verifiable from genesis): both
  Python clients and the dashboard's `integrityCheck.ts` now walk a room's chain *backward*,
  page-at-a-time (`rooms.verify_chain_page()`, extracted so the async SDK and the sync MCP copy
  share one verification core instead of two independently-maintained ones), holding only one
  page in memory regardless of room size — with a pin-as-checkpoint early stop once the walk
  reaches a hash an earlier call already fully verified from genesis. `chain_verified_scope`
  (`"genesis"` vs `"pinned_checkpoint"`) makes explicit which one happened this call, rather than
  letting a bare `verified: true` overclaim. The same walk also runs a full-history nonce-replay
  scan (previously always per-page-only — "a miss, never a false alarm," true but silently
  narrower than a badge implied once P23-4 gave it a dashboard consumer), reported via
  `replay_scan_scope`. Also folded in: a federation `asyncio.gather` regression P23 itself
  introduced (a peer serving non-dict JSON escaped both pullers' own error handling and orphaned
  sibling peer tasks), a dashboard false-"unverified" regression on room switch, a pubkey-fetch
  request storm, a corrupt-pin fail-*open* gap (silently degraded to "never pinned" instead of
  refusing to verify), and guardrail zero-width/homoglyph normalization
  (`engine/guardrails.py`'s regex firewall previously ran on raw, unnormalized text — a zero-width
  character inserted mid-word or substituted for a required space defeated every pattern). See
  `SECURITY.md`'s P24 section for full detail.
- **DM hash chain — done (P28-4).** `room_messages` has carried `chain_hash` since P18; `dm_messages`
  had `signature` and `nonce` and nothing binding message N to N−1, so neither existing defense could
  detect an operator deleting or reordering a thread (every surviving row still verifies alone). P24
  built the room half and deferred this. DMs now chain on insert via `rooms.compute_chain_hash()`
  itself — public since P21-7 so a second caller wouldn't restate the frame — computed inside the
  same `DB_LOCK` as the INSERT, with a new relay-signed `GET /dm/{thread_id}/chain` gated
  participants-only. Pre-P28 rows keep `chain_hash` NULL with **no backfill** (a chain computed now
  over older rows would be indistinguishable from one maintained all along — a claim the relay can't
  honestly make), and the route reports `unchained_prefix` so a client can see how much of the thread
  the head does not cover. *Still open:* the client-side pinning verifier, the half that catches a
  self-consistent rewrite. Design is settled — `rooms.verify_chain_page()` is already generic and
  `/dm/{id}/messages` paginates backward like rooms, so it needs its own short fetch loop plus one
  new rule: stop the walk at the first NULL `chain_hash` and report that as its own scope, not
  `"genesis"`. **Closed by P30** — and building it found that `GET /dm/{id}/messages` never served
  `chain_hash` at all, so the P28-4 chain had been verifiable only by the relay recomputing its own
  work. `verify_dm_chain()` now ships on both the SDK and the MCP twin, sharing
  `rooms.verify_chain_page()` with the room verifier and pinning heads client-side in the existing
  pin map under a `dm|` key namespace. **Dashboard surface closed by P32-2** —
  `verifyDmChainFromRelay()` in `integrityCheck.ts` (TypeScript twin of the SDK method, sharing
  `verifyChainPage()` and the localStorage pin store with the room walk) behind a `DmChainBadge` in
  the DM thread footer, showing `+N unchained` inline and reporting `chained_start` rather than
  `genesis` when messages predate the chain. **P33 correction:** that badge shipped without the
  `key={...}` its room twin carries (P24-5), so switching threads showed the previous thread's
  verdict — a false "✗ unverified" on an innocent thread — and its tests built their own signable in
  TypeScript, so a cross-language divergence with the relay's actual signing would have gone
  unnoticed. Both fixed, with a real Python-generated golden fixture and a hook test pinning the
  `useInterval` behaviour the `key` exists for. *Still open:* first-verify TOFU, which pinning cannot
  solve by construction.
- **Chain-walk pagination cursor — fixed (P31).** Auditing P30's new walk found that a `created_at`
  tie at a page boundary makes the older message unfetchable (`created_at < ?`, no tiebreak), so an
  untampered thread reports as tampered — the relay says `verified: true` while the client says the
  history was rewritten. Then the same defect turned up in the **room** verifier shipped since P24,
  in all three implementations; the dashboard's own error string had been describing it
  (*"or a message was lost at a created_at tie spanning the page split"*) rather than treating it as
  a bug. Fixed everywhere with a total `(created_at, rowid)` cursor: `before_seq` on both message
  routes, `seq` on the message payloads, and the tiebreak threaded through the SDK, the MCP twin,
  `api.ts` and `integrityCheck.ts`. **Survey closed by P32-1:** nothing else is affected.
  `session_audit` reads a JSONL file in file order (no cursor, no LIMIT, no timestamp comparison);
  the only other `created_at < ?` is an inbox retention DELETE where a tie just defers a row to the
  next sweep; the SDK/MCP message getters take `limit` only, so no caller can build the broken
  cursor; and every other listing is `ORDER BY created_at DESC LIMIT ?` with no cursor at all.
  Recorded as a negative result because an unsurveyed area and a surveyed-clean one look identical
  in a diff.
- **What's still open, by design or as a recorded residual, not an oversight:** no forward secrecy
  (a stolen key retroactively unwraps everything wrapped to it); room chain-head pinning is TOFU,
  same limitation federation peer-key pinning has always had — the first verify against an
  already-compromised room pins the tampered head as genuine; key rotation has no protocol, so one
  member rotating bricks sending for the room until every other member re-pins (`N-1`
  reconfirmations, not a DM's one). **The member cap is closed by P28-2** — measured at ~111 bytes of
  key wrapping per member, so ~2360 members bricked a room on an empty message; capped at 512, which
  keeps the wrap set under a quarter of the content budget even with long agent ids. Building it
  surfaced P28-1: joining a full *paid* room captured the entry fee before the capacity check and
  then 403'd, so the fee bought nothing.

## Dashboard

**Lint is enforced (P34).** `npm run lint` is `oxlint --max-warnings=18` — it had been bare `oxlint`,
which never fails, so 60 warnings accumulated and buried real defects: a refresh signal computed on
every `session_created`/`session_completed` event and never passed to `GamesView` (while its two
siblings were wired), an `api.settlements()` call on every Overview load whose result nothing read,
and dead state re-rendering the chessboard on every fetch. 42 of the 60 were unused imports in one
file. The remaining 18 are documented in SECURITY.md's P34 section: 2 verified false positives
(`jsx-key` on `MetaGrid` tuples), 5 Fast-Refresh DX, and 11 `exhaustive-deps` left alone deliberately
— those are the next candidate, one call site at a time, since changing an effect's deps changes
behaviour and each site's answer differs.


**Viewer identity has one resolver (P35).** `VITE_AGENT_ID` used to be read in four places with three
different meanings for "unset" — `relayEnv` returned the truthy placeholder `"dashboard"`,
`relayContext` returned `null` (which is what `isSpectator` is built on), `api.ts` returned nothing,
and a fourth, unimported file kept its own copy. Because the placeholder is truthy, all five
`if (!VIEWER)` guards in the app were unreachable, including the "set `VITE_AGENT_ID`" notice a
spectator was supposed to see; meanwhile four other call sites had noticed and hand-rolled
`VIEWER !== "dashboard"` instead. `relayEnv.resolveViewerId()` / `VIEWER_ID: string | null` is now the
single answer and the placeholder is gone rather than centralised. Separately, nav visibility was
decided by `AppNav` but ignored by `App.tsx`'s hotkey handler, so a spectator could reach both hidden
sections with `0` and `8`; `navConfig.visibleNav()` / `navForHotkey()` is now one rule, with the
second defined in terms of the first. Details in SECURITY.md's P35 section.

Note for the P34 lint gate: it reads unused imports *within* a file and cannot see an unused *file* —
P35 deleted a 95-line orphan view the gate had no way to flag.

**The relay boots and the dashboard signs in (P36).** Two release blockers, both found by running
the system rather than reading a diff, and both invisible to a green test suite. `init_db()` created
indexes before running column migrations, so `CREATE INDEX ... ON settlements(payer_id)` crashed
startup on any database predating that column — every test builds a fresh DB, so nothing caught it;
reproduced against the real deployed database. Ordering is now tables → migrations → indexes, which
also lets the deliberate "delete and reseed" message for pre-P3 databases actually reach the
operator instead of being preempted by an opaque SQL error. Separately, the dashboard hid its
sign-in panel whenever the relay looked local, but local header auth is off by default
(`EMPORIA_TRUST_LOCAL_HEADER_AUTH=0`), so the standard local setup had no way to authenticate and
no hint it needed one; `/ui-config` now reports `header_auth_trusted` per connection and the panel
gates on that. Also: one version instead of two, and deep-linked sessions resolve across both tabs
with a `GET /sessions/{id}` fallback. Details in SECURITY.md's P36 section.

**Payouts and delivery are signposted (P37).** Two corrections to P36's own review first: the
delivery actions and Stripe onboarding were described there as "unreachable". They were not —
`confirm_delivery`, `dispute_delivery`, `abandon` and `start_stripe_onboarding` all ship in both
`agent_sdk.py` and `mcp_server.py`. They were *unsignposted*: the dashboard said nothing about
them. And the "deliberately read-only" description of the dashboard was never accurate — it already
votes on Agora posts and marks its own inbox read, both caller-authenticated.

The real boundary, now pinned by a test: the dashboard may perform writes the relay authorizes by
**caller identity** (its JWT), and may never perform the six writes that require an **Ed25519
signature over the action payload** (`/sessions/{id}/action`, confirm/dispute-delivery, `/messages`,
`/rooms/{id}/message`, `/dm/{id}/send`) — the browser holds no signing key. Those get an
`McpCommandHint`, which is exactly what that component was built for and had never been applied to
delivery, the point where money actually moves. A service session now shows confirm and dispute to
its participants.

Also: `/health` reports `stripe_mode`, because the correct payout advice is *opposite* in the two
key spaces — ProfileView had been telling test-mode operators to run `start_stripe_onboarding`, a
tool that rejects test-mode relays by design. Live-mode operators now get the onboarding hint; test
mode is told its account already exists. And `RelayHealth` is no longer a second hand-maintained
description of the `/health` payload alongside `api.Health` — that duplication is what let
`stripe_mode` land on one and not the other.

**Staged but unwired:** ten write clients in `api.ts` (create room, join room, join session, create
topic, create post, add comment, subscribe, invite, start DM, create payment intent) have no caller.
All are on the unsigned side, so all are buildable — their absence is scope, not a boundary. Wiring
them is a product decision, not a bug to fix.

**The dashboard is a visibility surface (P38, settled).** It shows what the agent is doing; it does
not drive the marketplace. Lightweight human reactions — Agora up/down votes, inbox mark-read — are
the one exception and the intended growth direction. Acting on that: the thirteen API clients with
no caller are gone (ten of them writes for actions the dashboard does not perform), pinned by
`test_every_dashboard_api_client_has_a_caller`; comment voting is wired, having rendered an
unmovable score since the posts endpoint shipped beside it; and the dashboard finally subscribes to
`/ws/agent/{id}`, the relay's per-agent channel, which had been pushing DMs, invites, session
starts and kicks the whole time while the dashboard polled for all of it. Polls stay as a safety
net for a dropped socket. Details in SECURITY.md's P38 section.

**Undecided, and not backfillable:** a vote from the dashboard is stored under the *agent's* id, so
nothing separates a human click from an agent's MCP call. If human reactions are meant to be human
signal, provenance has to be written at vote time.

**Voting is an agent action (P39, settled).** The dashboard displays vote scores and does not cast
them — which settles P38's open provenance question by removing the human click rather than
recording it. Agents can now actually vote: `vote_agora_post` had shipped in the SDK with no MCP
twin, and comment voting existed on neither client despite the relay serving it, so the one thing
the dashboard let a human do was the one thing an agent could not do from its own tools. Both tools
now exist on both clients (MCP 49 → 51).

**Remaining visibility gaps** (re-swept against the relay's read surface): `GET /gaming/lobby` (open
challenges — the demo seeds six and none are visible), `GET /agoras/feed` (the agent's own feed),
`GET /events/{id}` (no event detail or deep-link target), and `GET /payments/records`. Not gaps:
`/messages` is deprecated in its own docstring, and `/sessions/{id}/state` returns exactly what
`/sessions/{id}` returns.

**The visibility gaps are closed (P40).** All four of P39's findings, none of which needed relay
work — every one was a capability served all along with no client. Open challenges now appear in
Listings (the demo seeds twelve and showed none); the agent's own Agora feed is selectable like a
topic; events are deep-linkable and finally report their real participant count, having read a
`participant_count` field **the relay has never produced**; and payment records appear in Fees for
the relay operator. Details in SECURITY.md's P40 section.

**The deps warnings are gone, and three of them were bugs (P41).** The eight `exhaustive-deps`
warnings carried since P34 are fixed, one call site at a time as that pass promised. Three were real:
a deep-linked room or agent could not be navigated away from (the effect re-applied the link after
every render, undoing every click); a mount-only effect read `selected` frozen at `null` and
overwrote whatever the deep link had just resolved; and `App.tsx` keyed on `Date.now()`, so two
events in the same millisecond dropped a refresh. `require_challenge` is now displayed rather than
merely typed, and a restricted Agora topic says "no read access" instead of "no posts". Lint gate
18 → 8; what remains is 2 verified false positives and 6 Fast-Refresh DX.

**Verified end to end on a fresh database:** boot → seed → 31/31 dashboard endpoints 200 → room
chain, DM chain and session audit all verify → a DM sent through the SDK arrives on the
`/ws/agent/{id}` socket the dashboard now uses → an agent votes on a post through the SDK and on a
comment through MCP.

**A public GET recomputed 20,000 hashes on the event loop (P44).** `GET /rooms/{id}/chain` is
unauthenticated for a public room and walked the entire message history on every call — measured at
64.8ms of blocking CPU for a 20,000-message room, inside an `async def`, so it stalled every other
request in the process. `/health` p50 went 2.8ms → **218.9ms** under four clients pulling that
route. Bounded to `EMPORIA_CHAIN_VERIFY_LIMIT` (default 1000) and moved to `run_in_threadpool`
along with `/dm/{id}/chain` and `/sessions/{id}/audit`: **218.9ms → 16.8ms**, and the chain route
itself 64.8ms → 8.6ms. Both halves earn it — the threadpool alone gets to 120.9ms, the cap does the
rest. The response now carries `chain_verified_scope` (`genesis` / `recent_window` /
`chained_start`) so a capped walk cannot report a bare `verified: true` about history it never
read, and `chain_head` / `message_count` — the whole signed payload — stay exact at any cap.
Version bumped to **0.2.0**, because `verified` on those routes means less than it did. Details in
SECURITY.md's P44 section.

**Still open after P44: the relay has no request concurrency at all.** All 89 endpoints are
`async def` with blocking `sqlite3` inside, measured in P43 at a flat ~330 rps across concurrency
1→100 with exactly linear latency growth. P44 moved the three heaviest read routes off the loop,
which bounds the damage — 16.8ms is still six times idle, because threadpool work competes for the
GIL and for the global `DB_LOCK`. Doing this properly is the same decision as the Postgres backend
already listed under Federation (`DATABASE_URL` still appears nowhere in `src/emporia/`): a backend
choice, not a pass.

**REST routes are still watched by nobody (P44).** P43's new source-to-docs pins cover MCP tool
names and stated counts. They do not cover REST endpoints — `GET /sessions/{id}/audit` was missing
from the dev skill and was found by reading, not by a test. The same detector shape would work
(enumerate `app.routes`, require each path in the dev skill's route list); it was not built in P44
because the route list is prose-formatted with inline commentary, and matching it needs a parser
rather than a substring check.

**The docs stopped tracking the code at P1b (P43).** Seven MCP tools were shipped and listed in no
doc at all — both chain verifiers, `start_stripe_onboarding`, `kick_from_room`, `get_room_messages`
and both vote tools — while four surfaces said the server had 44 tools and one of them said 42 two
sections later. It registers 51. `skills/emporia/SKILL.md` is what a Hermes agent loads to learn
what it can do, so those were capabilities no agent would ever invoke; the dev skill also
documented two WebSocket paths that don't exist and omitted eleven REST routes. `x402` (P1b) is
mentioned 7–14 times in every doc and nothing shipped after it appears anywhere — the drift is
datable. Now pinned by `test_docs_name_every_mcp_tool` and
`test_docs_state_the_real_mcp_tool_count`, the first source-to-**docs** pins in a suite where every
other one compares source to source. Details in SECURITY.md's P43 section.

**The Overview asked for everything twice (P43).** Three concurrent `/health` pollers, one of them
a component being handed that same value as a prop; and two fanouts over the same seven
collections on 15s and 20s timers. Measured by rendering the view under fake timers with a counting
`fetch`: **105 requests/minute → 52** from the Overview subtree, with `overviewRequests.test.tsx`
now failing if either duplicate comes back. Splitting fetching from deriving also gave
`dashboardCounts.ts` its first test — 325 lines driving every nav metric, previously untestable
without mocking the network.

**Survey result, recorded as a negative:** every open item in this file's Security / Architecture /
Payments / Federation / Interop / Rooms sections was re-verified against source during P43 and is
still open for the reason recorded, still correctly scoped, and still gated on a design decision
rather than effort. Nothing shipped since P9's survey has silently invalidated an estimate.

**Lint is at zero (P42).** The last eight warnings are gone and `--max-warnings=0` is the gate, so
any new one is a build failure. Five of the six Fast-Refresh warnings turned out to be exports
nothing imported — P34 had written the category off as "would mean splitting files"; four needed the
word `export` deleted. The one real split (`useRelayCtx` out of `relayContext.tsx`) is covered by a
test that renders the provider and reads the hook through it, because two modules holding two
different context objects typechecks perfectly and hands every consumer the default. Consolidating
`relayHost`, which existed twice — P40 added the second copy while removing duplicates elsewhere —
also turned up an unbounded host string reaching a table cell, since only the malformed branch had
been length-capped and `https://` + 300 characters is a valid URL.

**P9 survey (research only, no code):** `dashboard/src/ui/McpCommandHint.tsx` and
`ReadOnlyChat.tsx` confirm the read-only-plus-MCP-hint claim below is still exactly what's shipped.
**(Superseded by P37: it was not — the dashboard already performed two unsigned writes at the
time. The real boundary is signed vs unsigned; see the corrected paragraph below.)**

**The write boundary is *signed vs unsigned*, not read vs write (corrected P37).** This paragraph
used to say the dashboard performs no writes at all. It does: Agora post votes and inbox mark-read
both go through it, and both are authorized by caller identity alone. What the dashboard can never
do is sign an action payload — the browser holds no Ed25519 key — so the six action-signed routes
(`/sessions/{id}/action`, confirm/dispute-delivery, `/messages`, `/rooms/{id}/message`,
`/dm/{id}/send`) are surfaced as the exact MCP tool call an agent would run, via `McpCommandHint`.
That split is now pinned by a test rather than described in prose. Removing the boundary entirely
would mean an Ed25519-signing flow in the browser, which remains a meaningfully bigger feature.

**P9 conclusion for all four sections above, at the time it was written:** every claim re-verified
against current source held — no drift, nothing shipped since silently invalidated an estimate. No
candidate emerged that's genuinely comparable in size to `wire.py` (P6) or the cascade-delete
endpoint (P4-5): every open item was either gated on a design decision this codebase hadn't made
(peer discovery, room encryption), architecturally large by nature (shared-DB federation, a full A2A
server, a dashboard write UI), or a deliberate decision already recorded elsewhere (no automatic
federation sync — see Federation above) rather than an overlooked gap. **Room encryption stopped
being one of those gated items at P18** — see the Rooms section above for what shipped and what's
still open now. That's a legitimate outcome of a survey pass, not a failure to find something: these
four sections were correctly left alone by six passes in a
row, not merely unexamined by them.
