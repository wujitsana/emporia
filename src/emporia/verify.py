"""Check a game without trusting the relay that ran it (P144).

Pure: the caller fetches the session, its public log, the players' public
keys and its own receipts; this says what holds and what does not. Every
finding names its evidence, so a report built from it can be handed to anyone
— another relay, the players, a court of opinion — and re-checked by them.

What it checks:

- **The log is one unbroken hash chain** — recomputed here, not taken from
  the relay's "verified" flag.
- **Every move is signed by its player**, over exactly what the relay recorded
  (needs `step_number` and `rationale` in the entry; logs from before P144
  lack them, and those moves are reported as uncheckable, not as good).
- **Every receipt this agent holds is the relay's signature, and its move is
  in the log** with the same content. A receipt whose move is missing is the
  relay having accepted a move and then dropped it.
- **The relay's verdict is what the rules give.** The moves are replayed
  through the game's own module, here. A game ended by the clock cannot be
  replayed without the times, so it is reported as such, with the receipts'
  acceptance times as the evidence to weigh.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from emporia import identity
from emporia.module_sdk import MODULE_REGISTRY, SessionAction
from emporia.session_audit import verify_chain_entries
from emporia.wire import signed_action_payload

OK, FAIL, UNCHECKED = "ok", "fail", "unchecked"


def _payload_sha(payload: Any) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def verify_game(*, session: dict[str, Any], log: list[dict[str, Any]], public_keys: dict[str, str],
                receipts: list[dict[str, Any]], relay_key: str | list[str] | None,
                relay_verdict: dict[str, Any] | None) -> dict[str, Any]:
    findings: list[dict[str, Any]] = []

    def note(check: str, status: str, detail: str, **evidence: Any) -> None:
        findings.append({"check": check, "status": status, "detail": detail, **evidence})

    sid = session["session_id"]
    entries = [e for e in log if not e.get("genesis")]

    chain_ok, why = verify_chain_entries(log)
    note("log_chain", OK if chain_ok else FAIL, why)

    for i, e in enumerate(entries):
        sender, sig = e.get("sender", ""), e.get("signature", "")
        if "step_number" not in e or "rationale" not in e:
            note("move_signature", UNCHECKED, f"entry {i} was logged without step and rationale (before P144)",
                 block_hash=e.get("block_hash"))
            continue
        key = public_keys.get(sender)
        if not key:
            note("move_signature", UNCHECKED, f"entry {i}: no public key for {sender}", block_hash=e.get("block_hash"))
            continue
        signed = signed_action_payload(sender, e.get("action", ""), e.get("payload", {}),
                                       e.get("rationale", ""), sid, e["step_number"],
                                       say=e.get("say", ""))
        # P245: the key the player had when the relay logged the move.
        at_key = identity.signing_key_for(key, e.get("timestamp"))
        good = bool(at_key) and identity.verify(signed, sig, at_key)
        note("move_signature", OK if good else FAIL,
             f"entry {i} ({sender} {e.get('action')}): " + ("signed by its player" if good else
                                                             "signature does not match what was logged"),
             block_hash=e.get("block_hash"))

    by_hash = {e.get("block_hash"): e for e in entries}
    for r in receipts:
        if r.get("session_id") != sid:
            continue
        # P191: a relay that rotated its key signed older receipts with an older
        # key in the chain the agent trusts; each receipt checks against its own.
        trusted = [relay_key] if isinstance(relay_key, str) else list(relay_key or [])
        named = r.get("relay_public_key")
        signed_by_relay = identity.verify_action_receipt(r, named if named in trusted or not trusted else trusted[-1])
        if not signed_by_relay:
            note("receipt", FAIL, f"receipt for step {r.get('step_number')} is not signed by the relay key",
                 receipt=r)
            continue
        if trusted and named not in trusted:
            note("receipt", FAIL, "receipt signed by a different relay key than the one pinned", receipt=r)
            continue
        logged = by_hash.get(r.get("block_hash"))
        if logged is None:
            note("receipt", FAIL, (f"the relay accepted {r.get('agent_id')}'s {r.get('action_type')} at "
                                   f"{r.get('accepted_at')} and its public log does not contain it"), receipt=r)
        elif _payload_sha(logged.get("payload", {})) != r.get("payload_sha256"):
            note("receipt", FAIL, "the logged move differs from the move the relay accepted", receipt=r)
        else:
            note("receipt", OK, f"{r.get('action_type')} accepted at {r.get('accepted_at')} is in the log")

    from emporia.module_sdk import load_bundled_modules
    load_bundled_modules()
    cls = MODULE_REGISTRY.get(session["module_type"])
    if cls is None:
        note("verdict", UNCHECKED, f"{session['module_type']} is not installed here, so its rules cannot be replayed")
    else:
        note(*_replay(cls(), session, entries, relay_verdict))

    failed = [f for f in findings if f["status"] == FAIL]
    return {"session_id": sid, "verdict": "fail" if failed else "ok", "failures": len(failed),
            "unchecked": sum(f["status"] == UNCHECKED for f in findings), "findings": findings}


def _replay(module, session: dict[str, Any], entries: list[dict[str, Any]],
            relay_verdict: dict[str, Any] | None) -> tuple[str, str, str]:
    state = module.initial_state(list(session["participants"]), session.get("config") or {})
    outcome: dict[str, Any] | None = None
    for i, e in enumerate(entries):
        try:
            res = module.apply_action(state, SessionAction(agent_id=e.get("sender", ""),
                                                           action_type=e.get("action", ""),
                                                           payload=e.get("payload", {})))
        except Exception as exc:
            return "verdict", FAIL, f"the log's move {i} is not legal under the rules: {exc}"
        state = res.new_state
        if res.artifacts and isinstance(res.artifacts.get("result"), dict):
            outcome = res.artifacts["result"]
    if relay_verdict is None:
        return "verdict", UNCHECKED, "the relay has not declared a result"
    if outcome is None:
        return "verdict", UNCHECKED, (f"the moves do not finish the game; the relay says it ended by "
                                      f"{relay_verdict.get('termination') or 'its clock'} — weigh the "
                                      f"receipts' acceptance times")
    same = (outcome.get("winner"), outcome.get("outcome_type")) == \
        (relay_verdict.get("winner"), relay_verdict.get("outcome_type"))
    if same:
        return "verdict", OK, f"replaying the moves gives the relay's result ({outcome.get('winner') or 'draw'})"
    return "verdict", FAIL, (f"replaying the moves gives {outcome.get('winner') or 'a draw'}, the relay "
                             f"declared {relay_verdict.get('winner') or 'a draw'}")
