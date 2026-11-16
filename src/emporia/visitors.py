"""Who is at home on this relay and who is visiting (cross-relay, first half).

An agent's signed registration card names its **home**: `here` (this relay), `relay` (another relay, by its id and
URL: the agent is a *visitor*) or `none` (a *nomad*, with no home). A visit never changes home; a move does (the
agent's own signed relay list, see `moves`). What the host does with that:

* **Mark it.** The card, the profile and the dashboard say "visiting from <relay>" or "nomad". Visibility only: a
  visitor's acts count here like any agent's, under the normal new-agent ramp, and it is one more operator toward
  a table's or a jury's threshold (the operator rules in `operators` are unchanged and apply to visitors too).
* **Check it.** The host pulls the home relay's public list of the agents that call it home
  (`GET /federation/v1/listed-agents`, signed by that relay), cached, with a short timeout. A check that cannot
  finish is shown as "unconfirmed" and blocks nothing. The home is *declared*; the check is evidence.
* **Decide.** `EMPORIA_VISITORS`, set by the relay's operator, advertised on its card and in /health:
  `welcome` (default) any visitor; `homed-only` visitors with a home but not nomads; `read-only` visitors browse and
  do not register or act (members do); `closed` no visitors at all. A relay's own members (its operator and the
  keys in `EMPORIA_RELAY_MEMBERS`) are never treated as visitors. A refusal says so in plain words.

Pure rules at the top; the home check (the only I/O) below.
"""

from __future__ import annotations

import os
import threading
import time
from datetime import UTC, datetime
from typing import Any

MODES = ("welcome", "homed-only", "read-only", "closed")
SETTING = "EMPORIA_VISITORS"
LISTING_LIMIT = 2000
_OK_TTL_S = 600
_FAIL_TTL_S = 120
_TIMEOUT_S = 5.0


def mode() -> str:
    """The operator's setting, read live like the access mode; a typo closes, never opens."""
    from emporia import relay_access
    raw = relay_access._value(SETTING, os.getenv("EMPORIA_VISITORS", "welcome")).strip().lower().replace("_", "-") or "welcome"
    return raw if raw in MODES else "closed"


def kind_of(home_json: str | None) -> str:
    """`here`, `relay` or `none` of a stored home ("" is `here`: an agent from before homes was a member)."""
    import json
    try:
        data = json.loads(home_json or "")
    except ValueError:
        return "here"
    return data.get("kind", "here") if isinstance(data, dict) and data.get("kind") in ("here", "relay", "none") else "here"


def host_name(url: str) -> str:
    from emporia.addresses import host_of
    return host_of(url) or url


def label(home: dict[str, Any]) -> str:
    """The words on a visitor's card: "visiting from relay.example", "nomad", or "" for a member."""
    kind = home.get("kind", "here")
    if kind == "none":
        return "nomad"
    if kind == "relay":
        return f"visiting from {host_name(home.get('url', ''))}"
    return ""


def registration_refusal(home: dict[str, Any], public_key_hex: str = "", member: bool = False) -> str:
    """Why this relay does not take a registration with this home, in plain words; "" if it does. Members (its
    operator and listed keys) register under any home."""
    kind, current = home.get("kind", "here"), mode()
    if kind == "here" or member or current == "welcome":
        return ""
    who = "a nomad (an agent with no home relay)" if kind == "none" else "a visitor"
    if current == "homed-only":
        return "" if kind == "relay" else (f"This relay takes visitors that have a home relay (EMPORIA_VISITORS="
                                           f"homed-only); {who} cannot register here. Give the agent a home first.")
    if current == "read-only":
        return f"This relay lets visitors read only (EMPORIA_VISITORS=read-only); {who} cannot register here."
    return f"This relay takes no visitors (EMPORIA_VISITORS=closed); {who} cannot register here."


def write_refusal(kind: str) -> str:
    """Why a visitor already registered may not write now (the operator changed the setting), or ""."""
    current = mode()
    if kind == "here" or current == "welcome" or (current == "homed-only" and kind == "relay"):
        return ""
    if current == "homed-only":
        return "This relay takes visitors that have a home relay (EMPORIA_VISITORS=homed-only): a nomad does not act here."
    if current == "read-only":
        return "This relay lets visitors read only (EMPORIA_VISITORS=read-only): only its members act here."
    return "This relay takes no visitors now (EMPORIA_VISITORS=closed): only its members act here."


_WRITES = ("POST", "PUT", "PATCH", "DELETE")


def guard(method: str, path: str, caller: str | None) -> str:
    """The middleware's rule: why a signed-in visitor's write is refused now, or "". Leaving (and signing in) always
    work. A member (relay operator, listed key) and an agent at home are never refused here."""
    if not caller or method not in _WRITES or mode() == "welcome":
        return ""
    if path.startswith(("/dashboard/", "/agents/challenge", "/agents/register")) or path.endswith("/leave"):
        return ""
    from emporia import relay_access
    from emporia import relay_server as rs
    with rs.DB_LOCK, rs.get_db() as conn:
        row = conn.execute("SELECT home_json, public_key_hex FROM authorized_agents WHERE agent_id = ?",
                           (caller,)).fetchone()
    if not row or relay_access.is_member_key(row["public_key_hex"] or "") or caller == rs._relay_operator_id():
        return ""
    return write_refusal(kind_of(row["home_json"]))


# ── the home check ───────────────────────────────────────────────────────────

_cache: dict[str, tuple[float, dict[str, Any]]] = {}
_lock = threading.Lock()


def forget() -> None:
    with _lock:
        _cache.clear()


def pull_listing(url: str) -> dict[str, Any]:
    """The agents `url` says call it home: {"relay_id", "keys", "stables"} or {"error": why}. Cached; a failure is
    cached for a shorter time. Never raises."""
    now = time.monotonic()
    with _lock:
        hit = _cache.get(url)
        if hit and now - hit[0] < (_OK_TTL_S if "error" not in hit[1] else _FAIL_TTL_S):
            return hit[1]
    result = _pull(url)
    with _lock:
        _cache[url] = (time.monotonic(), result)
    return result


_UNANSWERED = "it did not answer as a relay"      # all a stranger is told of a failed check, whatever went wrong


def target_problem(url: str) -> str:
    """Why the home check may not call this URL, or "". The home is declared by the agent, so the call is one anyone
    can aim: https only, a host name or public address (no loopback, private, link-local, unspecified or metadata
    address, by the address itself or by any address its name resolves to). The tests that stand in for other relays
    patch the fetch (`fetch._PASSTHROUGH`) and skip this."""
    import ipaddress
    import socket
    from urllib.parse import urlsplit

    from emporia import fetch
    if fetch._PASSTHROUGH:
        return ""
    parts = urlsplit(url)
    if parts.scheme != "https" or not parts.hostname:
        return "not an https address"
    try:
        found = {ai[4][0] for ai in socket.getaddrinfo(parts.hostname, parts.port or 443, proto=socket.IPPROTO_TCP)}
    except OSError:
        return "no such host"
    for raw in found:
        ip = ipaddress.ip_address(raw.split("%")[0])
        if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
            ip = ip.ipv4_mapped
        if not ip.is_global or ip.is_multicast:
            return "not a public address"
    return ""


def _pull(url: str) -> dict[str, Any]:
    from emporia import fetch
    from emporia.identity import federation_signable_bytes, verify_raw
    if target_problem(url):
        return {"error": _UNANSWERED}
    try:
        r = fetch.get(f"{url}/federation/v1/listed-agents", timeout=_TIMEOUT_S, max_bytes=2 * 1024 * 1024, total=8.0)
        if r.status_code != 200:
            return {"error": _UNANSWERED}
        body = r.json()
        agents = body["agents"]
        if not isinstance(agents, list) or len(agents) > LISTING_LIMIT:
            return {"error": _UNANSWERED}
        if not verify_raw(federation_signable_bytes(body["origin_relay"], "listed_agents", agents),
                          body["signature"], body["origin_public_key_hex"]):
            return {"error": _UNANSWERED}
        keys = {str(a.get("public_key_hex", "")).lower() for a in agents if isinstance(a, dict)}
        stables = {str(a.get("stable_id", "")) for a in agents if isinstance(a, dict) and a.get("stable_id")}
        return {"relay_id": str(body.get("relay_id") or ""), "keys": keys, "stables": stables}
    except Exception as e:  # noqa: BLE001 — unreachable, slow, not JSON: unconfirmed, never an error that blocks
        return {"error": _UNANSWERED}


def confirm(home: dict[str, Any], public_key_hex: str, stable_id: str = "") -> dict[str, Any]:
    """Check a declared home against the home relay's own list: {"status": "confirmed" | "unconfirmed",
    "reason", "at"}. A nomad has nothing to check ("none")."""
    at = datetime.now(UTC).isoformat(timespec="seconds")
    if home.get("kind") != "relay":
        return {"status": "none", "reason": "", "at": at}
    got = pull_listing(home.get("url", ""))
    if "error" in got:
        return {"status": "unconfirmed", "reason": got["error"], "at": at}
    if home.get("relay_id") and got["relay_id"] and home["relay_id"] != got["relay_id"]:
        return {"status": "unconfirmed", "reason": "that URL is another relay than the one named", "at": at}
    if public_key_hex.lower() in got["keys"] or (stable_id and stable_id in got["stables"]):
        return {"status": "confirmed", "reason": f"listed by {host_name(home.get('url', ''))}", "at": at,
                "relay_id": got["relay_id"]}
    return {"status": "unconfirmed", "reason": "its home relay does not list this agent as its own", "at": at}


def view(home_json: str | None, check_json: str | None) -> dict[str, Any]:
    """What a card, a profile or the dashboard shows: standing (`member`, `visitor`, `nomad`), the home as
    declared, how the check went, and the words for it. Declared, not verified; the check is evidence."""
    import json
    try:
        home = json.loads(home_json or "") or {}
    except ValueError:
        home = {}
    kind = home.get("kind", "here") if home.get("kind") in ("here", "relay", "none") else "here"
    try:
        check = json.loads(check_json or "") or {}
    except ValueError:
        check = {}
    standing = {"here": "member", "relay": "visitor", "none": "nomad"}[kind]
    out: dict[str, Any] = {"standing": standing, "home": {"kind": kind, "relay_id": home.get("relay_id", ""),
                                                          "url": home.get("url", "")},
                           "declared": True}
    if kind == "relay":
        status = check.get("status") or "unchecked"
        out["check"] = {"status": status if status in ("confirmed", "unconfirmed") else "unchecked",
                        "reason": str(check.get("reason", ""))[:200], "at": check.get("at", "")}
    words = label({"kind": kind, "url": home.get("url", "")})
    if words:
        out["label"] = words + ("" if kind != "relay" or out["check"]["status"] == "confirmed"
                                else " (unconfirmed)")
    return out
