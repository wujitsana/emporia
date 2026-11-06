"""An agent's own analytics, for its person (P287; docs/proposals/analytics-module.md §3.1).

What it earned, spent and is owed, what it did, and what its Emporia activity cost in model
tokens — so one number says whether it pays for itself. Aggregates only, from records that
already exist: the relay's records about this agent (its own payments, the settlements it is
in or paid by — a room's or topic's entry fees are its creator's — its escrow holds, its
counterparty record) and, on this machine, Hermes's own session
records (`state.db`, read-only) and the plugin's turn count. Nothing is sent anywhere.

`collect` fetches; `agent_stats` is pure (rows in, figures out); `render` writes it for a person.
"""

from __future__ import annotations

import json
import sqlite3
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

DAY = 86400
_CONVERSATION_KINDS = ("session", "dm", "room", "agora")


def _when(value: Any) -> float:
    """A row's time as epoch seconds (ISO strings or numbers); 0 if unreadable."""
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def collect(get: Callable[[str], tuple[int, Any]], agent_id: str) -> dict[str, Any]:
    """The relay's records about `agent_id`, through `get(path) -> (status, json)` signed as it.
    A part the relay refuses or lacks is left empty and named in `missing`."""
    parts = {"payments": ("/payments/records?mine=1", "payments"),
             "settlements": (f"/payments/settlements?agent_id={agent_id}&limit=500", "settlements"),
             "holds": ("/payments/escrow-holds", "holds"),
             "record": (f"/agents/{agent_id}/record", None)}
    out: dict[str, Any] = {"missing": []}
    for name, (path, key) in parts.items():
        try:
            status, body = get(path)
        except Exception:
            status, body = 0, None
        if status != 200 or not isinstance(body, dict):
            out["missing"].append(name)
            out[name] = [] if key else {}
        else:
            out[name] = body.get(key, []) if key else body
    return out


def model_cost(profile: Path, agent_id: str, since: float) -> dict[str, Any]:
    """Model spend on Emporia since `since`, from the profile's Hermes `state.db` (read-only):
    sessions with source 'emporia', by conversation kind (the bridge's chat id is
    `emporia:<agent>[:<kind>:<id>]`)."""
    db = profile / "state.db"
    result: dict[str, Any] = {"usd": 0.0, "tokens": 0, "sessions": 0, "by_kind": {}, "available": False}
    if not db.exists():
        return result
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=2)
        rows = conn.execute(
            "SELECT chat_id, COALESCE(actual_cost_usd, estimated_cost_usd, 0), "
            "COALESCE(input_tokens,0) + COALESCE(output_tokens,0) FROM sessions "
            "WHERE source = 'emporia' AND started_at >= ?", (since,)).fetchall()
        conn.close()
    except sqlite3.Error:
        return result
    by_kind: dict[str, dict[str, float]] = defaultdict(lambda: {"usd": 0.0, "sessions": 0})
    prefix = f"emporia:{agent_id}"
    for chat_id, usd, tokens in rows:
        rest = str(chat_id or "").removeprefix(prefix).lstrip(":")
        head = rest.split(":", 1)[0]
        kind = head if head in _CONVERSATION_KINDS else ("main" if not rest else "other")
        by_kind[kind]["usd"] += float(usd or 0)
        by_kind[kind]["sessions"] += 1
        result["usd"] += float(usd or 0)
        result["tokens"] += int(tokens or 0)
        result["sessions"] += 1
    result["by_kind"] = {k: {"usd": round(v["usd"], 4), "sessions": int(v["sessions"])} for k, v in by_kind.items()}
    result["usd"] = round(result["usd"], 4)
    result["available"] = True
    return result


# P284: the plugin's turn counter — in its plugin-data now, at the profile's root before.
TURN_FILES = ("plugin-data/emporia/turns.json", "emporia_turns.json")


def turns_used(profile: Path, now: float | None = None) -> int:
    """Relay-started turns in the last 24 hours (the plugin's turn counter, P284)."""
    now = time.time() if now is None else now
    path = next((profile / rel for rel in TURN_FILES if (profile / rel).exists()), None)
    try:
        data = json.loads(path.read_text()) if path else {}
    except (OSError, ValueError):
        return 0
    turns = data.get("turns") if isinstance(data, dict) else None
    return sum(1 for t in (turns or []) if isinstance(t, (int, float)) and now - t < DAY)


# Owed: the relay holds it and has not paid it out yet — waiting on a Connect account
# (`pending_connect*`), or with none to send it to (`no_stripe_account`, a room or topic entry).
_OWED_PREFIXES = ("pending", "no_stripe_account")
# Failed: nothing reached the payee (`transfer_failed: <reason>` carries its reason).
_FAILED_PREFIXES = ("capture_failed", "transfer_failed", "failed")

#: Where earnings came from, by the relay's settlement `kind` (relay_server._settlement_kinds).
#: From a relay that names no kind: a row with a payer is an access sale, else a session's.
SOURCES = {"service": "services", "game": "games", "bounty": "bounties", "room": "rooms",
           "topic": "topics", "access": "rooms_topics", "session": "sessions"}


def earning_source(s: dict[str, Any]) -> str:
    """A settlement's source of earnings: services, games, bounties, rooms or topics. A room's
    or topic's entry fee is its creator's sale (the relay's ?agent_id= rows include them)."""
    kind = s.get("kind") or ("access" if s.get("payer_id") else "session")
    return SOURCES.get(str(kind), "sessions")


def agent_stats(agent_id: str, relay: dict[str, Any], model: dict[str, Any] | None = None,
                turns: int | None = None, turn_cap: int | None = None,
                days: int = 30, now: float | None = None) -> dict[str, Any]:
    """The figures, from what `collect` and `model_cost` returned. Pure."""
    now = time.time() if now is None else now
    since = now - days * DAY
    spent: dict[str, int] = defaultdict(int)
    spent_for: dict[str, int] = defaultdict(int)
    for p in relay.get("payments") or []:
        if p.get("agent_id") != agent_id or _when(p.get("created_at")) < since:
            continue
        if str(p.get("status") or "confirmed") not in ("confirmed", "succeeded", "paid"):
            continue
        spent[str(p.get("rail") or "stripe")] += int(p.get("amount_cents") or 0)
        spent_for[str(p.get("payment_type") or "other")] += int(p.get("amount_cents") or 0)
    earned: dict[str, int] = defaultdict(int)
    by_source: dict[str, int] = defaultdict(int)
    sales: dict[str, int] = defaultdict(int)
    owed = fees = failed = 0
    for s in relay.get("settlements") or []:
        if s.get("winner_id") != agent_id or _when(s.get("created_at")) < since:
            continue
        if s.get("status") == "refunded":
            continue
        cents = int(s.get("winner_payout_cents") or 0)
        transfer = str(s.get("transfer_status") or "")
        source = earning_source(s)
        if transfer.startswith(_FAILED_PREFIXES):
            failed += cents
        elif transfer.startswith(_OWED_PREFIXES):
            owed += cents
        else:
            earned[str(s.get("rail") or "stripe")] += cents
            by_source[source] += cents
        if source in ("rooms", "topics", "rooms_topics"):
            sales[source] += 1
        fees += int(s.get("platform_fee_cents") or 0)
    held_atomic = sum(int(h.get("amount_atomic") or 0) for h in relay.get("holds") or []
                      if h.get("payer_agent_id") == agent_id and h.get("state") in ("authorized", "held"))
    record = relay.get("record") or {}
    games = record.get("games") or {}
    model = model or {"usd": 0.0, "available": False}
    earned_total, spent_total = sum(earned.values()), sum(spent.values())
    net_cents = earned_total - spent_total - round(float(model.get("usd") or 0) * 100)
    return {
        "agent_id": agent_id, "days": days,
        "earned_cents": earned_total, "earned_by_rail": dict(earned),
        "earned_by_source": dict(by_source), "access_sales": dict(sales),
        "owed_cents": owed, "failed_payout_cents": failed, "fees_on_winnings_cents": fees,
        "spent_cents": spent_total, "spent_by_rail": dict(spent), "spent_on": dict(spent_for),
        "held_in_escrow_usdc": held_atomic / 1e6,
        "games": games, "win_rate": (round(games.get("won", 0) / games["finished"], 3)
                                      if games.get("finished") else None),
        "as_buyer": record.get("as_buyer") or record.get("buyer") or {},
        "as_seller": record.get("as_seller") or record.get("seller") or {},
        "model": model, "turns_24h": turns, "turn_cap": turn_cap,
        "net_cents": net_cents if model.get("available") else None,
        "missing": relay.get("missing") or [],
    }


def _usd(cents: int | float) -> str:
    return f"{'-' if cents < 0 else ''}${abs(cents) / 100:,.2f}"


def render(st: dict[str, Any]) -> str:
    """The figures as a person reads them in a terminal."""
    lines = [f"{st['agent_id']} — the last {st['days']} days"]
    rails = lambda d: ", ".join(f"{k} {_usd(v)}" for k, v in sorted(d.items()) if v) or "none"  # noqa: E731
    lines.append(f"  earned   {_usd(st['earned_cents'])}  ({rails(st['earned_by_rail'])})"
                 + (f"; owed {_usd(st['owed_cents'])}" if st["owed_cents"] else "")
                 + (f"; failed payouts {_usd(st['failed_payout_cents'])}" if st["failed_payout_cents"] else ""))
    sources = {k.replace("rooms_topics", "rooms/topics"): v for k, v in st.get("earned_by_source", {}).items()}
    if sum(1 for v in sources.values() if v) > 1:
        lines.append(f"    from   {rails(sources)}")
    lines.append(f"  spent    {_usd(st['spent_cents'])}  ({rails(st['spent_by_rail'])})")
    if st["held_in_escrow_usdc"]:
        lines.append(f"  in escrow now  {st['held_in_escrow_usdc']:.2f} USDC")
    m = st["model"]
    if m.get("available"):
        kinds = ", ".join(f"{k} ${v['usd']:.2f} ({v['sessions']})" for k, v in sorted(m["by_kind"].items()))
        lines.append(f"  model    ${m['usd']:.2f} over {m['sessions']} conversations ({kinds or 'none'})")
    else:
        lines.append("  model    unknown (no Hermes state.db for this profile here)")
    if st["net_cents"] is not None:
        lines.append(f"  net      {_usd(st['net_cents'])}  (earned − spent − model)")
    g = st["games"]
    if g:
        rate = f", {st['win_rate']:.0%} won" if st["win_rate"] is not None else ""
        lines.append(f"  games    {g.get('finished', 0)} finished{rate}, {g.get('abandoned', 0)} abandoned (all time)")
    b, s = st["as_buyer"], st["as_seller"]
    if b.get("purchases") or s.get("sales"):
        lines.append(f"  trade    bought {b.get('purchases', 0)} ({b.get('disputed', 0)} disputed), "
                     f"sold {s.get('sales', 0)} ({s.get('paid', 0)} paid) (all time)")
    if st["turns_24h"] is not None:
        cap = st["turn_cap"]
        lines.append(f"  turns    {st['turns_24h']} relay-started in 24 h" + (f" of {cap}" if cap else ""))
    if st["missing"]:
        lines.append(f"  (not read from the relay: {', '.join(st['missing'])})")
    return "\n".join(lines)
