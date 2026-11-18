"""An agent's passport card: who it is, and what each relay says about its record. Evidence that counts for nothing.

* **Identity** — its address, handle, key, fingerprint and where it lives (member, visitor or nomad).
* **Statements** — one per relay: the record that relay signed (`moves.carried_record` checks the ones another
  relay signed). "Relay A says, math checked": never merged, never summed, never best-of. This relay's own
  statement is its own; every other one has `counts_here: false`.
* **Reach** — how many relays hold a *verified* standing statement (`standing_statement.py`) whose issuer key the
  serving relay knows (a linked relay's pinned key, or a statement it pulled from the agent's confirmed home) and
  whose issuer counted standing (at least two operators), unweighted: "standing on N relays", one per issuer relay
  id. A bare record, an unverified or expired statement, one signed by a key the relay does not know, or one that
  counted nothing adds nothing. It weighs no relay and counts toward no XP, level,
  allowance, Renown or season here. Weighing waits until a relay's operator vouches another (a later release).
* **Standing statements** (2.1) — a relay's signed statement of counted facts about the agent, kept whole so any
  reader can check it again: "standing at R: …, verified: signed by R on <date>; not counted here".

Pure of I/O: the routes feed it rows, the client checks what it fetches.
"""

from __future__ import annotations

from typing import Any

KIND = "emporia-passport"
NOTE = ("Evidence only: each statement is one relay's word about this agent, signed by that relay and counted by "
        "none other. Reach counts relays whose verified standing statement, signed by a key this relay knows, counted "
        "at least two operators, unweighted, and counts for nothing on this relay yet.")


def has_record(statement: dict[str, Any]) -> bool:
    """Whether a relay's statement shows any record: a finished game, a purchase or a sale."""
    if isinstance(statement.get("counted"), int) and statement["counted"] > 0:
        return True
    return any(isinstance(v, int) and v > 0 for group in ("games", "as_buyer", "as_seller")
               for v in (statement.get(group) or {}).values())


def statement(record: dict[str, Any], *, counts_here: bool) -> dict[str, Any]:
    """One relay's statement about the agent, from a record response (signed, or carried and checked)."""
    from emporia import moves
    return {"relay": record.get("relay", ""), "relay_public_key": record.get("relay_public_key", ""),
            "signature": record.get("signature", ""), "as_of": record.get("as_of", ""),
            "record": moves.signed_part(record) if counts_here else {k: v for k, v in record.items()
                                                                     if k in moves._CARRIED_TEXT + tuple(moves._CARRIED_COUNTS) + ("counted",)},
            "counts_here": counts_here,
            # this relay's own is whole and signed; another relay's was checked against its signature when the
            # agent carried it here, and only its known fields are kept
            "checked": "signed by this relay" if counts_here else "checked when the agent carried it here",
            "says": f"{record.get('relay', '')} says" if record.get("relay") else "a relay says"}


def build(identity: dict[str, Any], own_record: dict[str, Any], elsewhere: list[dict[str, Any]],
          own_standing: dict[str, Any] | None = None, host_relay_id: str | None = None) -> dict[str, Any]:
    """The passport: `own_record` is this relay's signed record of the agent, `elsewhere` the records other relays
    signed that the agent carried here (already checked on the way in), each possibly with its `standing`
    statement kept whole. `own_standing`: this relay's own statement about the agent, when it makes one.
    `host_relay_id`: this relay's id, the audience a carried statement may name.

    Reach counts a relay only for a verified statement whose issuer counted standing (`REACH_MIN_OPERATORS`)."""
    from emporia import standing_statement as ss
    key = str(identity.get("public_key_hex") or "") or None
    statements = [statement(own_record, counts_here=True)]
    reach: list[str] = []
    reach_ids: set[str] = set()                # one per issuer relay id, whatever URL it is filed under
    if own_standing is not None:
        statements[0]["standing_statement"] = own_standing
        if ss.counts_for_reach(own_standing, known=True, subject_key=key, audience=host_relay_id):
            reach.append(statements[0]["relay"])
            reach_ids.add(str((own_standing.get("issuer") or {}).get("relay_id") or ""))
    seen = {own_record.get("relay", "")}
    for rec in elsewhere:
        if rec.get("relay") in seen:
            continue                       # one statement per relay: the first stands
        seen.add(rec.get("relay", ""))
        st = statement({**{k: v for k, v in rec.items() if k != "standing"}, "as_of": rec.get("as_of", "")},
                       counts_here=False)
        held = rec.get("standing")
        if isinstance(held, dict):
            result = ss.verify(held, subject_key=key, audience=host_relay_id)
            known = bool(rec.get("standing_known"))      # the serving relay's own finding, never the statement's claim
            source = str(rec.get("standing_source") or "")
            st["standing_statement"] = held
            st["standing_known"] = known
            st["standing"] = ss.shown(held, result, known=known, source=source)
            issuer_id = str((held.get("issuer") or {}).get("relay_id") or "")
            if (known and result["ok"] and ss.counted_operators(held) >= ss.REACH_MIN_OPERATORS
                    and issuer_id not in reach_ids):
                reach.append(source or st["relay"])
                reach_ids.add(issuer_id)
        statements.append(st)
    return {"kind": KIND, "identity": identity, "statements": statements,
            "reach": {"relays": len(reach), "listed": reach, "weighted": False,
                      "text": f"standing on {len(reach)} relay{'' if len(reach) == 1 else 's'}",
                      "counts": "verified standing statements with at least "
                                f"{ss.REACH_MIN_OPERATORS} counted operators"},
            "counts_here": False, "note": NOTE}


def check(passport: dict[str, Any]) -> list[dict[str, Any]]:
    """What a reader verifies: the signature of each statement the serving relay signed whole, against the key
    its relay presented. [{relay, ok}]; `ok` is None for a record carried from another relay, which the serving
    relay checked and kept in part (so it cannot be re-checked from what it serves); a standing statement carried
    with it is kept whole, so `standing_ok` says whether it verifies (signature, key, relay id, audience, expiry), or None when the
    serving relay does not know the issuer's key (`standing_known` false). Whether a key is its relay's
    is the reader's to pin, as for any relay card."""
    from emporia.identity import federation_signable_bytes, verify_raw
    out = []
    for st in passport.get("statements") or []:
        record = st.get("record") or {}
        if not st.get("counts_here"):
            entry = {"relay": st.get("relay", ""), "ok": None}
            if isinstance(st.get("standing_statement"), dict):       # kept whole, so it can be checked again
                from emporia import standing_statement as ss
                known = bool(st.get("standing_known"))
                entry["standing_known"] = known
                # a statement signed by a key the serving relay does not know proves only "that key says": not checked
                entry["standing_ok"] = bool(ss.verify(st["standing_statement"],
                                                      subject_key=(passport.get("identity") or {}).get("public_key_hex"),
                                                      audience=passport.get("host_relay_id"))["ok"]) if known else None
            out.append(entry)
            continue
        ok = bool(st.get("signature")) and verify_raw(
            federation_signable_bytes(st.get("relay", ""), "record", [record]), st["signature"],
            st.get("relay_public_key", ""))
        out.append({"relay": st.get("relay", ""), "ok": ok})
    return out
