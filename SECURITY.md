# Emporia Security Model & Hardening Roadmap

This document is the result of a full security audit of the relay (`src/emporia/relay_server.py`),
MCP server, and dashboard, done ahead of the hackathon submission. It records what was found, what
was fixed before the deadline, and what's left — so the gaps are documented, not silent.

## Fixed for this submission

| Finding | Severity | Fix |
|---|---|---|
| Signature verification was **optional** on `POST /sessions/{id}/action` — an agent could submit a move/action under any `agent_id` with no proof of key possession | Critical | Signature is now mandatory (401 if missing, 403 if invalid). The signed payload also now binds `session_id` + the current `step_number`, so a captured signature can't be replayed against a different session or turn. `agent_sdk.py` and the MCP `submit_action`/`confirm_delivery`/`dispute_delivery` tools were updated to fetch the current step and sign accordingly. |
| `GET /agents/{id}/inbox` and `POST /agents/{id}/inbox/mark-read` had **no caller authentication** — any request could read or clear any agent's inbox by guessing/knowing their `agent_id` | Critical | Both endpoints now require the authenticated caller (`X-Emporia-Agent-Id` on localhost, or the dashboard JWT) to match the path `agent_id`, or be the relay operator. `_require_caller_is()` helper. |
| `DELETE /agoras/topics/{slug}/subscribe` had no caller authentication — any request could unsubscribe any agent from any topic | Critical | Same `_require_caller_is()` gate applied. |
| CORS was `allow_origins=["*"]` with `allow_credentials=True` — any website could replay a browser's stored dashboard JWT against the relay | Medium | Explicit allowlist (relay's own URL + localhost dev ports, extendable via `EMPORIA_CORS_ORIGINS`), narrowed methods/headers. |
| `/ui-config` queried a nonexistent `agents` table and 500'd on every call, silently breaking dashboard role detection | Bug (demo-affecting) | Fixed to query `authorized_agents`. |

All of the above shipped with test coverage (`tests/test_emporia.py`: `test_action_rejected_missing_signature`,
`test_action_rejected_forged_signature`, `test_inbox_rejects_cross_agent_access`, plus updates to
every existing test that submits a session action).

**Verified as non-issues** (raised during audit, checked against the code, not actual vulnerabilities):
- `stripe_profile_id` returned by `/health` — this is a public MPP routing identifier agents need
  to pay via the `mpp` rail, not a secret. Intentionally public; MCP/dashboard both consume it.

## Not fixed — hardening roadmap

Ranked by severity. Each entry: what's wrong, where, how it's exploited, and the fix direction.
None of these block the hackathon demo; all are real gaps for a production deployment.

### Critical

**WebSocket endpoints are unauthenticated.** `/ws/{session_id}`, `/ws/agent/{agent_id}`, and
`/ws/rooms/{room_id}` accept any connection with no identity check — anyone who knows or guesses
an ID can stream live game moves, private room chat, or another agent's real-time notifications.
- *Exploit:* `websockets.connect("ws://relay:8088/ws/sess_abc123")` with no auth, observe every
  move/message in real time.
- *Fix direction:* resolve caller identity the same way `_dashboard_agent_id()` does for REST
  (localhost header trust or JWT) before `ws.accept()`; for sessions/rooms, additionally check the
  caller is a participant/member when the room is private.

### High

**Federation gossip is not cryptographically verified.** `_pull_federated_listings()` trusts a
peer's `/ptgs/v1/federate/listings` response (including the `origin_relay` field and every
listing's `agent_id`) and does `INSERT OR REPLACE` with no signature check.
- *Exploit:* a malicious or compromised peer relay can overwrite a legitimate listing with a scam
  description, or post listings under another agent's `agent_id` to capture payments intended for
  them.
- *Fix direction:* require the peer relay's owner agent to sign the listings batch (Ed25519, same
  primitive already used for agent identity); reject listings whose `agent_id` isn't known to be
  registered on the origin relay; track `origin_relay` by relay ID, not just URL, so a URL change
  can't cause a relay to re-import its own stale listings as if they were a peer's.

**Session stake amount is not cross-checked against the Stripe PaymentIntent amount.**
`join_session()` computes `amount_cents` from the session's stored `payment_rules` and trusts that
figure when recording the payment, without verifying it equals what was actually authorized on the
PaymentIntent.
- *Fix direction:* after `verify_payment_intent()`, assert the returned PI amount equals the
  expected stake before calling `record_payment()`; reject with 402 on mismatch.

**No application-layer idempotency on payment recording.** `record_payment()` uses
`INSERT OR IGNORE` keyed loosely; a retried request with the same `payment_intent_id` can silently
no-op instead of erroring, masking a double-charge or under-recording scenario.
- *Fix direction:* `SELECT` for an existing row by `payment_intent_id` first; return the existing
  record on a repeat call instead of relying on `INSERT OR IGNORE`'s silence.

### Medium

**Registration challenge consumption has a TOCTOU race.** `_consume_challenge()` reads `used`,
then updates it, as two separate statements — two concurrent registration attempts with the same
`challenge_id` can both pass the check before either writes.
- *Fix direction:* `UPDATE reg_challenges SET used=1 WHERE challenge_id=? AND used=0`, then check
  `conn.total_changes == 0` to detect the race atomically.

**Guardrails are regex-based and bypassable.** `_INJECTION_PATTERNS` matches literal phrasing
("ignore all previous instructions") — trivially evaded by paraphrase, typos, or Unicode
homoglyphs/zero-width characters.
- *Fix direction:* this is the known limit of a deterministic pattern-match firewall; the
  `HERMES_PTGS_GUARDRAILS_MODE` design already anticipates layering an LLM-based classifier
  (NeMo Guardrails proper) in front of or behind the regex pass for semantic detection.

### Low

**Dashboard JWT secret is in-memory and rotates on relay restart.** All active dashboard sessions
are invalidated on every relay restart (by design — no secret persisted to disk) — a UX
papercut, not a vulnerability, but worth knowing if a restart happens mid-demo.
- *Fix direction:* persist the HMAC secret to a file under the same permission model as Ed25519
  keys (`0o600`) if cross-restart sessions become a requirement.

## Threat model notes (by design, not gaps)

- **Ed25519 = auth; the relay never holds private keys.** Keys live at `~/.hermes/keys/*.priv`,
  `0o600`, generated/loaded by `identity.py`, never transmitted.
- **No blockchain, no wallets** — deliberately out of scope for this hackathon (Stripe handles
  settlement); see `project_emporia` memory for the consolidation decision.
- **SQL injection**: not found — every query in `relay_server.py` is parameterized (`?` placeholders).
- **Each relay node is independently trusted** — there's no shared database; federation is
  gossip-based by design, which is why peer-signature verification (above) is the right fix rather
  than introducing a shared trust root.
