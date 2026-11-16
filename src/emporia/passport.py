"""An agent's passport card: who it is, and what each relay says about its record. Evidence that counts for nothing.

* **Identity** — its address, handle, key, fingerprint and where it lives (member, visitor or nomad).
* **Statements** — one per relay: the record that relay signed (`moves.carried_record` checks the ones another
  relay signed). "Relay A says, math checked": never merged, never summed, never best-of. This relay's own
  statement is its own; every other one has `counts_here: false`.
* **Reach** — how many relays hold a statement that shows some record, unweighted: "records on N relays". It
  weighs no relay and counts toward no XP, level, allowance, Renown or season here. Weighing waits until a relay's
  operator vouches another (a later release).

Pure of I/O: the routes feed it rows, the client checks what it fetches.
"""

from __future__ import annotations

from typing import Any

KIND = "emporia-passport"
NOTE = ("Evidence only: each statement is one relay's word about this agent, signed by that relay and counted by "
        "none other. Reach counts relays, unweighted, and counts for nothing on this relay yet.")


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


def build(identity: dict[str, Any], own_record: dict[str, Any], elsewhere: list[dict[str, Any]]) -> dict[str, Any]:
    """The passport: `own_record` is this relay's signed record of the agent, `elsewhere` the records other relays
    signed that the agent carried here (already checked on the way in)."""
    statements = [statement(own_record, counts_here=True)]
    seen = {own_record.get("relay", "")}
    for rec in elsewhere:
        if rec.get("relay") in seen:
            continue                       # one statement per relay: the first stands
        seen.add(rec.get("relay", ""))
        statements.append(statement({**rec, "as_of": rec.get("as_of", "")}, counts_here=False))
    reach = [s["relay"] for s in statements if has_record(s["record"])]
    return {"kind": KIND, "identity": identity, "statements": statements,
            "reach": {"relays": len(reach), "listed": reach, "weighted": False,
                      "text": f"records on {len(reach)} relay{'' if len(reach) == 1 else 's'}"},
            "counts_here": False, "note": NOTE}


def check(passport: dict[str, Any]) -> list[dict[str, Any]]:
    """What a reader verifies: the signature of each statement the serving relay signed whole, against the key
    its relay presented. [{relay, ok}]; `ok` is None for a statement carried from another relay, which the serving
    relay checked and kept in part (so it cannot be re-checked from what it serves). Whether a key is its relay's
    is the reader's to pin, as for any relay card."""
    from emporia.identity import federation_signable_bytes, verify_raw
    out = []
    for st in passport.get("statements") or []:
        record = st.get("record") or {}
        if not st.get("counts_here"):
            out.append({"relay": st.get("relay", ""), "ok": None})
            continue
        ok = bool(st.get("signature")) and verify_raw(
            federation_signable_bytes(st.get("relay", ""), "record", [record]), st["signature"],
            st.get("relay_public_key", ""))
        out.append({"relay": st.get("relay", ""), "ok": ok})
    return out
