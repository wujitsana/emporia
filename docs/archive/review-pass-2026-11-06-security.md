# Security review, pass 2 (2026-11-06)

An independent read of the last two days' work, on branch `review2-security` (from `preview-2`,
2eeb6f1) and, for purse, `purse-solana-pay` (b0527a1). Nothing here is pushed or merged.

Fixes are small, each with a test that fails without it (`tests/test_review2_security.py`, and
one dashboard test). Anything larger or debatable is **proposed** below. Items already accepted
in `docs/security-log.md`, `docs/review-pass-2026-11-05.md`, `docs/proposals/signer-phase-2.md`
and `docs/proposals/gamify-bargain.md` are not repeated.

## Fixed (branch `review2-security`)

| Sev | Where | Scenario | Commit |
|---|---|---|---|
| Medium | `signer._agent` | `agent_dir()` folds case and odd characters, so `X-Emporia-Agent: P2_AGENT` (or ` p2_agent`, `p2_agent!`) found p2_agent's token, while `_OPENS` (the `EMPORIA_SIGNER_OPEN_PER_MIN` cap and the 300-in-10-minutes backlog alert) and approvals keyed the spelling sent. A takeover holding the agent's token read the whole backlog past the cap, never tripping the alert, by changing spelling each minute. The header must be the id exactly as the signer files it | cac794f |
| Medium | `key_guard` | `emporia wallet mainnet` is person-only and `wallet limits` waits for the person's yes, but `hermes config set EMPORIA_X402_MAINNET 1` (plus the pay networks), `echo EMPORIA_X402_MAINNET=1 >> …/.env`, or a key in config.yaml passed with no word from the person: an agent on the `local` backend unlocked real-money payments in-process with two plain commands. Now: assigning the mainnet switch in a call that names the agent's config is refused as person-only; the pay networks, caps, approval threshold, signing limits, payout list and playbook go to the person's approval. Source and tests naming these are untouched | 57b58c0 |
| Low | `key_guard` | `~/.metamask` was matched only at the end of a command or before `/`: `tar czf x ~/.metamask && …` and `cp -r ~/.metamask /x` copied the signer user's `mm` session; `npx @metamask/agent-wallet wallet sign-typed-data` signed; a signer folder set by `EMPORIA_SIGNER_DIR` (run as the agents' user) had `admin.token`, `backends/<name>.secret` and `agents/<id>/keys` readable by path | 57b58c0 |
| Low | `mcp_server.join_session` (Bargain) | Nothing but the relay binds `deal_commitment`. A hostile relay refused the join (503), served a new commitment on the retry and got a second deterministic share, so a second deal; repeated until the hands suited it. The client now keeps the first commitment it signed a share for, per agent and game, and refuses a game whose commitment moved (or is not 64 hex) | a57c452 |
| Low | `agent_wallet.sign_exact`, `sign_escrow` | The day's total was charged before the back end was asked and kept when it refused (a provider's policy, MetaMask Beast Mode or a 2FA timeout, a signature from another key, an offer refused at signing). Three refused $4.00 payments took a $2.50 day to $14.50: every refusal used up the cap until the agent could pay nothing. The charge is given back when signing raises; Tempo, which sends a transaction, keeps its charge | bdd2f38 |
| Low | dashboard `chat/chatModel.ts` | Only key_guard's APPROVE text was recognised as an Emporia approval, so RELAY_CONTROL's (house start/stop, a snapshot, the expedition) was drawn with Hermes's "session" choice. Both now get once or deny. Bundles rebuilt | c3f797e |

## Proposals (not built)

**P1 (medium). Bug and idea verdicts are person-only by the CLI's word, not the signer's.**
- `emporia ideas … adopt` and `bugs … verdict` refuse without a person's terminal
  (`_persons_terminal`) and the key guard refuses their words. Underneath, `idea-verdict` is an
  agent kind in `signing.KINDS` (`person=False`), so the signer signs it with the agent's own
  token; and the relay takes `/bugs/{id}/verdict` on the operator's bearer token alone, which the
  operator agent's process mints with a `sign-in`.
- Scenario: a takeover of the relay operator's agent (or a disguised command on `local`) awards
  points and adopted ideas on the season board, which nobody can take back.
- Proposed: `idea-verdict` (and a new signed `bug-verdict`) become `person=True` kinds, signed
  through `signer.person_sign` like `key-rotation`, and the relay accepts a verdict only with that
  signature. Without a signer it stays the CLI's word, as today's testnet trade-off.

**P2 (low). `emporia house stop` trusts a PID file the agent can write.**
- `_house_pid` reads `<profile>/emporia_house.pid` and checks only that the process's command line
  contains `emporia.house`, which every profile's runner has; no owner check.
- Scenario: profile A's agent writes B's runner PID (or any process of its own naming
  `emporia.house`) into A's file; the person's `house stop A` stops B's runner.
- Proposed: start the runner with `--home <profile>` in its argv and match that, and check the
  process's owner is this user.

**P3 (low, purse). The same Solana Pay reference can be paid twice.**
- A reference marks one payment (Solana Pay v1.1). After a `paid: "unknown"` the agent may retry
  the same link and pay again; under the approval threshold nothing stops a loop paying one link
  until the daily cap.
- Proposed: refuse a link whose references were already in a `paid` or `signed`/unknown history
  entry, saying to check the explorer (`verify_by_reference`) first.

**P4 (low, purse). "expired" rests on one RPC answer.**
- `send_and_confirm` refunds the day's charge when `getBlockHeight` passes the blockhash's last
  valid height and `getSignatureStatuses` (with history) is empty. A load-balanced RPC whose
  status node lags the height node calls a landed transfer expired, and the cap undercounts it.
- Proposed: keep the charge for "expired" when `sendTransaction` itself succeeded, or ask the
  status twice a few seconds apart before refunding.

**P5 (low, purse). An approval is recorded when it is asked, not when it is given.**
- `guard.approval_request` stores `(tool, args) → cents` at prompt time; a denied prompt's record
  stays for 15 minutes. Today only a handler call that skipped `pre_tool_call` could consume it, and
  Hermes fires the hook for code-execution RPC calls too, so I found no path.
- Proposed: key the record by Hermes's approval request id, or drop it on a deny if Hermes gives a
  hook for the answer.

**P6 (low, both guards). Paths are matched as written.** `~/.hermes//.env`, `~/.hermes/./.env`,
`cd ~/.hermes && cat .env`, `plugin-data//purse/spend.json` pass both guards (the result masks
still hide a key assignment). Accepted as "not a wall"; proposed only: collapse `//` and `/./`
before matching, cheap and without false positives.

**P7 (info). MetaMask's Guard Mode check is never cached.** `signer.get_backend` builds a new
back end per request, so `_modes` (5 minutes) is always empty and every signature runs
`mm wallet trading-mode get` too. Safe (it checks more often), slower.

**Status (11-06):** P1, P2, P6 (Emporia's guard) and P7 built on `security-followups-2`; P3, P4,
P5 and P6 (purse's guard) on `purse-followups-2`. See `docs/security-log.md`. P1 took the user's
choice: a separate operator verdict key, not the agent's identity through `signer.person_sign`.

## Checked and found sound

- **Signer back ends:** every result is verified before use (KMS Ed25519 against the key, KMS
  ECDSA by recovery and low-s, Privy/CDP/MetaMask typed data by recovering the address, Privy's
  Solana signature over the unchanged message). The signer's own caps and rules run before any
  back end (`agent_wallet` under the Scope; `signing.check` before `identity.sign_payload`).
  MetaMask select+sign under one lock, Beast Mode refused, `MM_*` stripped from every `mm` call
  but `login`, errors redacted. Back-end credentials 0600 in the signer's folder only.
- **2b/2c:** `open_rule` opens only envelopes wrapped for the agent (id or `id@host`) in a DM, room
  or session context; encryption bindings must name the key the signer holds; keys made in the
  signer never reach the profile; restore never overwrites a held key; backups scrypt +
  AES-GCM with the format as AAD.
- **Operator routes:** whoami/status/snapshot take the caller from the bearer token only;
  `_require_operator` on status and snapshot; commands carry only relay ids matched by
  `^[A-Za-z0-9][A-Za-z0-9_.:-]{0,79}$` and profile names, the same in `operator_desk.py` and
  `operatorCommands.ts`, so no relay text reaches a copied shell command.
- **`GET /agents/{id}/next`:** 403 unless the bearer token is that agent's; no operator override.
- **Bargain hands:** the seed lives in `session_deals` (not logged, `translog.NOT_LOGGED`); state
  is concealed before every store, log and broadcast while the game runs; the move's artifacts
  carry the result only at the end; `private` serves the caller's own hand; `verify` says
  "not published" before the end. A player cannot grind (it never sees the relay's seed); the
  relay could only by re-dealing, now refused (a57c452).
- **Calls** are inside the v2 commitment and declared in the clear; **pledges and pledge acts**
  are signed by kind and checked against the maker or counterparty.
- **Dashboard visibility-only:** the only POSTs are the sign-in challenge/session and the local
  console's open routes; chat approvals write to Hermes only.
- **Console socket:** `_private_dir` refuses a link or another user's folder (lstat, uid, 0700),
  the hash subfolder is checked before the console starts, the secret goes on stdin.
- **purse:** Solana Pay parsing is strict (amount, USDC mint required, no unknown or repeated
  parameters, canonical base58); a transaction request's transaction is decoded, held to one
  classic-Token USDC transfer from this wallet's ATA (no lookup tables, no other signer, no
  pre-filled signature, bounded compute budget, at most one memo, read-only references, no other
  program, so no durable nonce, Token-2022 or ATA creation) and **rebuilt** with this wallet as
  fee payer and a blockhash it fetched; devnet pinned by genesis hash; URL guards connect to the
  checked address with TLS on the name, refuse redirects, private and IPv4-mapped addresses, and
  cap bodies; the spend log is charged under flock before signing and refunded only on a final
  failure; an approval is consumed once for the exact arguments and never above the approved cents.

## Not checked

- Live back ends (AWS KMS, Privy, CDP, MetaMask `mm`, 1Password, Bitwarden): fakes only.
- Real Solana devnet and Base Sepolia transactions: none sent.
- The dashboard in a running browser (CSP, rendering of relay text): source only.
