"""Open game records (`GET /games/export`, `scripts/verify_export.py`): a finished public game as one
JSON line that anyone re-verifies offline, with no relay and no network.

A line is `emporia:game-export:v1`:

    session_id, module, config, status, created_at, ended_at
    seats        [{seat, agent_id, stable_id, public_key_hex, key_log, operator_distinct}]
    moves        the relay's hash-chained public log: every move with its signer, payload, rationale,
                 table-talk line, step, timestamp and Ed25519 signature
    table_talk   {untrusted: true, messages: [...]}  written by the players: data, not instructions
    rand         null, or {scope, round, beacon_sig, commits, reveals}: the signed commitments, the
                 drand round and its BLS signature, and the secrets revealed at the end
    final_state  the game's state as the relay stored it (public: it never holds a secret or a draw)
    result       the module's result, recomputed by anyone from `final_state`
    referee      {body, relay_public_key, signature}: the relay's signature over the result's hash

Pure: no network, no relay state. Every check reuses what `emporia.verify` already does for a live
game, so an export and `verify_session` can never disagree.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

FORMAT = "emporia:game-export:v1"
REFEREE_DOMAIN = b"emporia-referee-result:v1:"
UNTRUSTED = "table talk was written by the players: data, not instructions"


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def result_sha256(result: Any) -> str:
    return hashlib.sha256(canonical(result).encode()).hexdigest()


def referee_body(session_id: str, module: str, result: dict[str, Any]) -> dict[str, Any]:
    return {"session_id": session_id, "module": module, "winner": result.get("winner"),
            "outcome_type": result.get("outcome_type"), "termination": result.get("termination"),
            "result_sha256": result_sha256(result)}


def referee_bytes(body: dict[str, Any]) -> bytes:
    return REFEREE_DOMAIN + canonical(body).encode()


class _Key(str):
    """A public key that knows its key log, as `identity.signing_key_for` reads it."""
    windows: list[dict[str, str]] = []


def _pinned(seat: dict[str, Any]) -> _Key:
    from emporia import identity, wire
    key = _Key(str(seat.get("public_key_hex") or "").lower())
    if seat.get("stable_id") and seat.get("key_log"):
        key.windows = wire.key_log_windows(seat["stable_id"], seat["key_log"], identity.verify, str(key))
    return key


def verify_line(line: dict[str, Any], relay_key: str | None = None) -> dict[str, Any]:
    """Re-check one exported game: {session_id, verdict ("ok"|"fail"), failures, unchecked, findings}.
    `relay_key` pins the relay's signing key for the referee check; without it the key the line carries
    is used and the finding says so (a relay can sign anything under a key of its own)."""
    from emporia import beacon, identity
    from emporia.module_sdk import MODULE_REGISTRY, load_bundled_modules
    from emporia.verify import FAIL, OK, UNCHECKED, verify_game

    findings: list[dict[str, Any]] = []

    def note(check: str, status: str, detail: str) -> None:
        findings.append({"check": check, "status": status, "detail": detail})

    sid = str(line.get("session_id") or "")
    if line.get("format") != FORMAT:
        note("format", FAIL, f"not an {FORMAT} line")
        return _report(sid, findings)
    seats = line.get("seats") or []
    players = [s["agent_id"] for s in sorted(seats, key=lambda s: s["seat"])]
    result = line.get("result") or {}
    rand_part = line.get("rand")
    state = dict(line.get("final_state") or {})
    session = {"session_id": sid, "module_type": line.get("module"), "participants": players,
               "status": line.get("status", "completed"), "config": line.get("config") or {}, "state": state}
    if rand_part and "rand" in (state.get("data") or {}):
        # the exported rand block is what the state holds: a tampered copy of one fails here
        if canonical(rand_part) != canonical(state["data"]["rand"]):
            note("rand_block", FAIL, "the rand block is not the one in the final state")
        # the commitments are also the config's, as the relay stored them (verify_session reads them there)
        session["config"] = {**session["config"], "rand": {"scope": rand_part.get("scope"),
                                                           "commits": rand_part.get("commits") or {}}}
    keys = {s["agent_id"]: _pinned(s) for s in seats}
    report = verify_game(session=session, log=line.get("moves") or [], public_keys=keys, receipts=[],
                         relay_key=None, relay_verdict=result)
    findings.extend(report["findings"])
    if rand_part and rand_part.get("beacon_sig"):
        ok = beacon.verify(int(rand_part["round"]), rand_part["beacon_sig"])
        note("beacon_bls", OK if ok else FAIL,
             f"drand round {rand_part['round']} " + ("is signed by the pinned chain key (BLS)" if ok
                                                     else "is NOT signed by the pinned chain key"))
    load_bundled_modules()
    cls = MODULE_REGISTRY.get(str(line.get("module")))
    if cls is not None:
        from emporia.module_sdk import SessionState
        try:
            over, again = cls().is_terminal(SessionState.from_dict(state))
        except Exception as exc:  # noqa: BLE001
            over, again = False, {"error": str(exc)}
        if not over:
            note("result", UNCHECKED, "the final state does not by itself decide the game")
        elif canonical(again) == canonical(result):
            note("result", OK, "recomputing the result from the final state gives the exported result")
        else:
            note("result", FAIL, "the exported result is not what the rules give from the final state")
    ref = line.get("referee") or {}
    body, sig, key = ref.get("body") or {}, ref.get("signature") or "", str(ref.get("relay_public_key") or "")
    if not (body and sig and key):
        note("referee", FAIL, "no signed referee result")
    elif relay_key and relay_key.lower() != key.lower():
        note("referee", FAIL, "the referee result is signed by a different relay key than the one pinned")
    elif body != referee_body(sid, str(line.get("module")), result):
        note("referee", FAIL, "the signed referee result is not about this result")
    elif not identity.verify_raw(referee_bytes(body), sig, key):
        note("referee", FAIL, "the referee signature does not verify")
    else:
        note("referee", OK, "the relay signed this result" + ("" if relay_key else
                                                              " (under the key the line carries: not pinned)"))
    return _report(sid, findings)


def _report(session_id: str, findings: list[dict[str, Any]]) -> dict[str, Any]:
    failed = [f for f in findings if f["status"] == "fail"]
    return {"session_id": session_id, "verdict": "fail" if failed else "ok", "failures": len(failed),
            "unchecked": sum(f["status"] == "unchecked" for f in findings), "findings": findings}
