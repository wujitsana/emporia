"""P281: checking a relay's transparency log from outside it (proposal steps 2 and 3).

The relay keeps the log (translog.py) and signs its state now and then. On its
own that protects no one: a relay can sign a log, rewrite it and sign again.
What catches it is someone who kept an earlier state asking for the proof that
the newer log only added to it. This module is that someone — an agent, or a
linked relay — and both use the same functions:

- `compare(a, b, ...)`: two states this relay signed. Same log (epoch) and same
  size: the roots must match. Otherwise the relay must prove the larger only
  added to the smaller. Two signed states it cannot reconcile are proof, which
  anyone can check, that it showed two histories.
- `check(...)`: fetch the relay's latest state and compare it with the one kept
  from last time; keep the newer. A new log (a rebuild, P269) is accepted only
  with the relay's signed hand-over, and reported, never silently.
- `included(...)`: is this record in the log a verified state covers? A client
  uses it for the key changes it followed (P245): a relay that shows you a key
  step it never logged is showing you something it did not show everyone.

`get(path, params)` is the caller's fetch: it returns `(status_code, json)`, so
the MCP server, the SDK, the CLI and the relay each bring their own HTTP.
"""

from __future__ import annotations

import json
import os
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable

from emporia import translog

Get = Callable[[str, dict], tuple[int, Any]]

#: an agent's kept states, one per relay, beside its relay pins
CLIENT_FILE = "log_states.json"
#: a relay's kept states of its linked peers (leading underscore: no agent's key file can be named so)
PEER_FILE = "_peer_log_states.json"


def state_signed(state: Any, trusted_keys: list[str] | tuple[str, ...]) -> bool:
    """A log state, signed by one of the relay keys trusted for this relay."""
    from emporia.identity import verify_raw
    if not isinstance(state, dict) or state.get("type") != translog.STATE_TYPE:
        return False
    key = state.get("relay_public_key") or ""
    if key not in trusted_keys or not isinstance(state.get("size"), int) or not isinstance(state.get("root"), str):
        return False
    return verify_raw(translog.state_bytes(state), state.get("signature") or "", key)


def _result(status: str, detail: str, state: dict | None = None, **extra: Any) -> dict[str, Any]:
    return {"status": status, "ok": status in ("ok", "first", "new_log", "off"), "detail": detail,
            "state": state, **extra}


def compare(a: dict, b: dict, get: Get) -> dict[str, Any]:
    """Two states of one relay, both already checked as signed by it. `ok` when
    one log only added to the other; `fail` (with `evidence`) when not."""
    ea, eb = a.get("epoch", 1), b.get("epoch", 1)
    if ea != eb:
        return _result("other_log", f"states of two different logs (epochs {ea} and {eb})", b)
    small, big = (a, b) if a["size"] <= b["size"] else (b, a)
    evidence = {"states": [small, big]}
    if small["size"] == big["size"]:
        if small["root"] == big["root"]:
            return _result("ok", f"the same log of {big['size']} entries", b)
        return _result("fail", f"two different logs, both signed, both {big['size']} entries long: "
                               "this relay showed two histories", b, evidence=evidence)
    try:
        code, body = get("/log/proof/consistency", {"first": small["size"], "second": big["size"]})
    except Exception as e:  # noqa: BLE001
        return _result("unreachable", f"could not ask for the consistency proof: {e}", b)
    if code not in (200, 400, 404):
        # busy (429) or down (5xx): no answer is not a refusal (review of P281)
        return _result("unreachable", f"the consistency proof did not load ({code})", b)
    if code != 200 or not isinstance(body, dict):
        return _result("fail", f"the relay signed logs of {small['size']} and {big['size']} entries and will not "
                               f"prove one grew from the other ({code})", b, evidence=evidence)
    try:
        proof = [bytes.fromhex(h) for h in body.get("proof") or []]
        good = translog.verify_consistency(small["size"], big["size"], bytes.fromhex(small["root"]),
                                           bytes.fromhex(big["root"]), proof)
    except ValueError:
        good = False
    if not good:
        return _result("fail", f"the log of {big['size']} entries does not contain the log of {small['size']} "
                               "it signed earlier: this relay rewrote its log", b,
                       evidence={**evidence, "proof": body.get("proof")})
    return _result("ok", f"the log grew from {small['size']} to {big['size']} entries and kept what it had", b)


def _new_log(held: dict, served: dict, get: Get, trusted_keys: list[str] | tuple[str, ...]) -> dict[str, Any]:
    """The relay started a new log since `held` (P269). Accept it only with a
    signed hand-over for every log since, the first naming a state no smaller
    than the one kept."""
    from emporia.identity import verify_raw
    try:
        code, body = get("/log/epochs", {})
    except Exception as e:  # noqa: BLE001
        return _result("unreachable", f"could not fetch the relay's logs: {e}", served)
    if code not in (200, 404):
        return _result("unreachable", f"the relay's list of logs did not load ({code})", served)
    by_epoch = {e.get("epoch"): e for e in (body.get("epochs") or [])} if code == 200 and isinstance(body, dict) else {}
    reasons = []
    for ep in range(held.get("epoch", 1) + 1, served.get("epoch", 1) + 1):
        h = (by_epoch.get(ep) or {}).get("handover")
        if not (isinstance(h, dict) and h.get("epoch") == ep and h.get("relay_public_key") in trusted_keys
                and verify_raw(translog.handover_bytes(h), h.get("signature") or "", h["relay_public_key"])):
            return _result("fail", f"the relay started a new log (number {ep}) without a signed hand-over",
                           served, evidence={"states": [held, served]})
        if ep == held.get("epoch", 1) + 1:
            prev = h.get("previous") or {}
            if not isinstance(prev.get("size"), int) or prev["size"] < held["size"] or (
                    prev["size"] == held["size"] and prev.get("root") != held["root"]):
                return _result("fail", f"the relay's hand-over to log {ep} names an earlier log that does not "
                                       f"match the one you kept ({held['size']} entries)",
                               served, evidence={"states": [held, served], "handover": h})
        reasons.append(h.get("reason") or "no reason given")
    return _result("new_log", "the relay started a new log, with a signed hand-over: " + "; ".join(reasons)
                   + ". What the old log held can no longer be proved against it.", served)


def check(get: Get, trusted_keys: list[str] | tuple[str, ...], held: dict | None) -> dict[str, Any]:
    """Fetch the relay's latest signed state and check it against `held`."""
    try:
        code, state = get("/log/state", {})
    except Exception as e:  # noqa: BLE001
        return _result("unreachable", f"could not fetch the log state: {e}", held)
    if code == 404:
        if held:
            # Review of P281: a relay that stops the log it kept can no longer be checked against what it
            # signed — which is also what a relay that rewrote it would do. Not a proof; not a pass.
            return _result("stopped", f"this relay no longer serves the transparency log it kept "
                                      f"({held['size']} entries when last checked): what it showed can no "
                                      f"longer be checked", held)
        return _result("off", "this relay keeps no transparency log", held)
    if code != 200:
        return _result("unreachable", f"the log state did not load ({code})", held)
    if not state_signed(state, trusted_keys):
        return _result("fail", "the log state is not signed by a key you trust for this relay", held,
                       evidence={"states": [state]})
    if held is None:
        return _result("first", f"first look: a log of {state['size']} entries, kept to check next time", state)
    if state.get("epoch", 1) < held.get("epoch", 1):
        return _result("fail", "the relay is back on an older log than the one you kept", held,
                       evidence={"states": [held, state]})
    if state.get("epoch", 1) > held.get("epoch", 1):
        return _new_log(held, state, get, trusted_keys)
    if state["size"] < held["size"]:
        return _result("fail", f"the log shrank from {held['size']} to {state['size']} entries", held,
                       evidence={"states": [held, state]})
    out = compare(held, state, get)
    if out["status"] != "ok":
        out["state"] = held          # keep what was proved, not what failed
    return out


def included(get: Get, state: dict, ref: str, matches: Callable[[dict], bool]) -> bool | None:
    """Whether a record filed under `ref` and accepted by `matches` is in the log
    `state` covers (a state already checked). None when the relay keeps no log
    or the record came after that state was signed (not provable yet)."""
    try:
        code, body = get("/log/entries", {"ref": ref, "limit": 1000})
    except Exception:  # noqa: BLE001
        return None
    if code != 200 or not isinstance(body, dict):
        return None
    found = [e for e in body.get("entries") or [] if isinstance(e, dict) and isinstance(e.get("record"), dict)
             and matches(e["record"])]
    if not found:
        return False
    for e in found:
        index = e.get("index")
        if not isinstance(index, int) or index >= state["size"]:
            continue
        try:
            code, proof = get("/log/proof/inclusion", {"index": index, "size": state["size"]})
            leaf = translog.leaf_hash(translog.canonical(e["record"]))   # from the record, not the relay's word
            if code == 200 and translog.verify_inclusion(index, state["size"], leaf,
                                                         [bytes.fromhex(h) for h in proof.get("proof") or []],
                                                         bytes.fromhex(state["root"])):
                return True
        except (ValueError, TypeError, AttributeError):
            continue
    return None if all(isinstance(e.get("index"), int) and e["index"] >= state["size"] for e in found) else False


# ─── what was kept ────────────────────────────────────────────────────────────

def _path(name: str, key_dir: Path | None = None) -> Path:
    from emporia.identity import KEY_DIR
    return (key_dir or KEY_DIR) / name


def load(name: str = CLIENT_FILE, key_dir: Path | None = None) -> dict[str, Any]:
    """What was kept; `key_dir` reads another agent's folder (the local console, read-only)."""
    try:
        data = json.loads(_path(name, key_dir).read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def kept_summary(relay: str, key_dir: Path | None = None) -> dict[str, Any] | None:
    """What the last check of `relay` found, in words and numbers only — for the dashboard (P281).
    None when it was never checked. Holds no key and no signature: the size, epoch and signing time
    of the state kept, the status, its detail, when it ran, and how many failures were recorded."""
    relays = load(CLIENT_FILE, key_dir).get("relays") or {}
    entry = relays.get(relay.rstrip("/")) or relays.get(relay.rstrip("/") + "/")
    if not isinstance(entry, dict):
        return None
    state = entry.get("state") if isinstance(entry.get("state"), dict) else {}
    status = str(entry.get("status") or "")
    failures = entry.get("failures") if isinstance(entry.get("failures"), list) else []
    return {"status": status, "ok": status in ("ok", "first", "new_log", "off"),
            "detail": str(entry.get("detail") or ""), "checked_at": entry.get("checked_at") or "",
            "size": state.get("size") if isinstance(state.get("size"), int) else None,
            "epoch": state.get("epoch", 1) if state else None, "state_at": state.get("at") or "",
            "failures": len(failures)}


def save(data: dict[str, Any], name: str = CLIENT_FILE) -> None:
    path = _path(name)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")   # one per writer
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(data, f, indent=1, sort_keys=True)
    os.replace(tmp, path)


_LOCK = threading.Lock()


@contextmanager
def _held(name: str):
    """Review of P281: one writer at a time — a relay checks its peers in
    parallel threads, and an agent's MCP server and CLI may check at once. Yields
    the file's current contents; whatever the caller leaves in it is saved."""
    import fcntl
    path = _path(name)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with _LOCK, open(path.with_name(path.name + ".lock"), "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            data = load(name)
            yield data
            save(data, name)
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def check_and_keep(relay: str, get: Get, trusted_keys: list[str] | tuple[str, ...],
                   name: str = CLIENT_FILE) -> dict[str, Any]:
    """`check`, then keep the state it proved; a failure keeps the earlier
    state and records the evidence, which never leaves the file once there."""
    from datetime import UTC, datetime
    entry = (load(name).get("relays") or {}).get(relay) or {}
    out = check(get, trusted_keys, entry.get("state"))        # the network, outside the lock
    now = datetime.now(UTC).isoformat()
    with _held(name) as data:
        entry = data.setdefault("relays", {}).get(relay) or {}
        kept = {"state": out["state"], "status": out["status"], "detail": out["detail"], "checked_at": now,
                "failures": entry.get("failures") or []}
        if out.get("evidence"):
            kept["failures"] = [*kept["failures"],
                                {"at": now, "detail": out["detail"], "evidence": out["evidence"]}][-20:]
        data["relays"][relay] = kept
    return {**out, "failures": len(kept["failures"])}


def _after(step_at: Any, state_at: Any, margin_seconds: int = 120) -> bool:
    """Whether a step's time is after a state's (less a margin for the two clocks), or either is unreadable."""
    from datetime import timedelta

    from emporia.wire import when_of
    step, state = when_of(step_at), when_of(state_at)
    return step is None or state is None or step > state - timedelta(seconds=margin_seconds)


def key_steps_logged(relay: str, get: Get, state: dict, pins: dict[str, Any]) -> dict[str, Any]:
    """For every agent pinned at `relay` whose key changed (P245), whether each
    step this client followed is in the log `state` covers.
    `{"logged": n, "pending": n, "missing": [agent ...]}`."""
    from emporia.identity import signing_pin_id
    out = {"logged": 0, "pending": 0, "missing": []}
    suffix = signing_pin_id("x", relay)[1:]          # "@host"
    for pin_id, pin in pins.items():
        if not pin_id.endswith(suffix) or not isinstance(pin, dict):
            continue
        sid = pin.get("stable_id") or ""
        for i, step in enumerate(pin.get("key_log") or []):
            if not isinstance(step, dict) or not sid:
                continue
            want = {k: step[k] for k in ("old", "new", "at", "signature", "commitment") if k in step}
            seen = included(get, state, f"agent_key:{sid}:{i}:{step.get('at')}",
                            lambda r, want=want: r.get("stable_id") == sid and isinstance(r.get("step"), dict)
                            and all(r["step"].get(k) == v for k, v in want.items()))
            if seen is False and _after(step.get("at"), state.get("at")):
                seen = None      # made after the state was signed: maybe not folded in yet (review of P281)
            if seen is True:
                out["logged"] += 1
            elif seen is None:
                out["pending"] += 1
            elif pin_id[: -len(suffix)] not in out["missing"]:
                out["missing"].append(pin_id[: -len(suffix)])
    return out


# ─── step 3 from a client: what other relays were told ────────────────────────

def cross_check(get: Get, trusted_keys: list[str] | tuple[str, ...], mine: dict, seen: Any) -> dict[str, Any]:
    """Compare the state this client kept with the one another relay was given
    by the same relay (its `/log/seen`). The other relay's word is not needed:
    only a state signed by this relay's key counts."""
    theirs = None
    for entry in (seen.get("relays") or {}).values() if isinstance(seen, dict) else ():
        state = entry.get("state") if isinstance(entry, dict) else None
        if state_signed(state, trusted_keys):
            theirs = state
            break
    if theirs is None:
        return _result("unchecked", "that relay holds no state of this relay's log", mine)
    out = compare(mine, theirs, get)
    out["state"] = mine
    return out


def fetch_getter(base: str, timeout: float = 15.0) -> Get:
    """A `Get` over HTTP, for a client (emporia.fetch caps what a relay may send)."""
    from emporia import fetch

    def get(path: str, params: dict) -> tuple[int, Any]:
        r = fetch.get(f"{base}{path}", params=params or None, timeout=timeout)
        try:
            return r.status_code, r.json()
        except ValueError:
            return r.status_code, None
    return get


def visit_rules_allow(url: str) -> bool:
    """Whether this agent's operator lets it read another relay (P188): a public
    route is browsing, allowed anywhere but the deny list (`visits.may_read`).
    The MCP tool, the SDK and `emporia log verify` all ask this before
    comparing with a relay."""
    from emporia import visits
    return visits.may_read(url)


def verify_relay(relay: str, get_for: Callable[[str], Get] = fetch_getter,
                 compare_with: list[str] | tuple[str, ...] | None = None,
                 may_ask: Callable[[str], bool] = visit_rules_allow) -> dict[str, Any]:
    """Everything a client checks of a relay's log, in one call (the MCP tool
    `check_relay_log`, the SDK's `check_relay_log()`, `emporia log verify`):

    1. the relay's key, against the one pinned for it (P144/P191);
    2. its latest signed state, against the one kept from last time;
    3. the key changes this client followed for agents there, each in the log;
    4. the state other relays were given (`compare_with`, or the relays it links
       to), against the one kept — only those `may_ask` allows (an agent's
       operator may have barred it from some relays, P188).

    `ok` is False when any of them caught something; `problems` says what."""
    from emporia.identity import load_peer_signing_keys, trusted_relay_keys
    get = get_for(relay)
    try:
        code, health = get("/health", {})
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "status": "unreachable", "detail": f"could not reach {relay}: {e}", "problems": []}
    health = health if isinstance(health, dict) else {}
    trusted, rotated = trusted_relay_keys(relay, health.get("relay_public_key"), health.get("relay_key_history") or [])
    if rotated:
        return {"ok": False, "status": "fail", "problems": [
            "the relay signs with a key you never pinned, and no signed hand-over leads to it"],
            "detail": "relay key changed"}
    out = check_and_keep(relay, get, trusted)
    problems = [out["detail"]] if not out["ok"] else []
    state = out.get("state")
    if out["status"] in ("ok", "first", "new_log") and state:
        steps = key_steps_logged(relay, get, state, load_peer_signing_keys())
        out["key_steps"] = steps
        for who in steps["missing"]:
            problems.append(f"the relay showed you a key change for {who} that is not in its log")
    if state and out["status"] != "off":
        if compare_with is None:
            try:
                code, peers = get("/federation/peers", {})
                compare_with = [p["url"] for p in (peers or {}).get("peers", []) if p.get("url")][:5] \
                    if code == 200 else []
            except Exception:  # noqa: BLE001
                compare_with = []
        out["compared_with"] = []
        for other in [u for u in compare_with if may_ask(u)]:
            try:
                code, seen = get_for(other)("/log/seen", {})
            except Exception as e:  # noqa: BLE001
                out["compared_with"].append({"relay": other, "status": "unreachable", "detail": str(e)})
                continue
            r = cross_check(get, trusted, state, seen if code == 200 else None)
            out["compared_with"].append({"relay": other, "status": r["status"], "detail": r["detail"]})
            if r["status"] == "fail":
                problems.append(f"{other} was shown another history: {r['detail']}")
                with _held(CLIENT_FILE) as data:
                    entry = data.setdefault("relays", {}).setdefault(relay, {})
                    entry["failures"] = [*(entry.get("failures") or []),
                                         {"detail": r["detail"], "via": other, "evidence": r.get("evidence")}][-20:]
    out["problems"] = problems
    out["ok"] = not problems
    return out
