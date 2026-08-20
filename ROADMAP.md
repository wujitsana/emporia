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
- **What's still open, by design or as a recorded residual, not an oversight:** no forward secrecy
  (a stolen key retroactively unwraps everything wrapped to it); room chain-head pinning is TOFU,
  same limitation federation peer-key pinning has always had — the first verify against an
  already-compromised room pins the tampered head as genuine; key rotation has no protocol, so one
  member rotating bricks sending for the room until every other member re-pins (`N-1`
  reconfirmations, not a DM's one); and there's no member cap, so a large enough encrypted room can
  hit the per-message content-size limit on ordinary sends.

## Dashboard

**P9 survey (research only, no code):** `dashboard/src/ui/McpCommandHint.tsx` and
`ReadOnlyChat.tsx` confirm the read-only-plus-MCP-hint claim below is still exactly what's shipped.

**Write actions are deliberately read-only + "show the MCP command."** The dashboard surfaces
exactly what MCP tool call an agent would run (join a session, message an agent) rather than
performing writes itself. This is a scope choice, not a gap — see `README.md`'s Dashboard section
for the rationale — but a future iteration could add an actual write UI (would need its own
Ed25519-signing flow in the browser, which is a meaningfully bigger feature).

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
