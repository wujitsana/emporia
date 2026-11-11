"""`emporia relay watch`: one screen of what a live relay's JSON log says went wrong (or did not).

The relay writes one JSON line per request and per named event (`logging_config.JsonFormatter`). This
module reads those lines (the local relay's log file, or `docker compose logs` piped in), keeps the ones
inside a time window, and summarizes them. It makes no network call of its own, apart from the optional
`/health` look the CLI passes in as `health`, and it never prints a message body, a token or an address:
clients appear as the short tag `logging_config.ip_tag` makes of `client_ip`.

What it counts is named by the relay, not guessed from text:
`request` (status_code, path, expected), `unhandled_error` (a 500), `rate_limited`, `agent_registered`,
`signin_failed`, `payment_settled` / `payment_failed`, `ws_rejected`.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from datetime import UTC, datetime, timedelta
from typing import Any, Iterable

from emporia.logging_config import ip_tag

# a segment after one of these names a thing, not a route: agents/alpha -> agents/:id
_COLLECTIONS = {"agents", "rooms", "sessions", "posts", "labs", "agoras", "topics", "threads", "dm",
                "settlements", "ideas", "pledges", "quests", "relays", "groups", "reports", "boards"}
_LITERALS = {"register", "challenge", "search", "mine", "me", "new", "status", "recent", "stream"}
_ID_LIKE =re.compile(r"^(?:[0-9a-f]{8,}|\d+|[a-z]{2,6}_[0-9a-f]{8,})$")


def parse_since(text: str) -> timedelta:
    """`30m`, `1h`, `24h`, `2d`; a bare number is hours."""
    m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([smhd]?)\s*", text or "")
    if not m:
        raise ValueError(f"--since is a number and a unit, like 30m, 1h or 2d (not {text!r})")
    n, unit = float(m.group(1)), m.group(2) or "h"
    return timedelta(seconds=n * {"s": 1, "m": 60, "h": 3600, "d": 86400}[unit])


def normalize_path(path: str) -> str:
    parts = (path or "").split("?")[0].split("/")
    out, prev = [], ""
    for seg in parts:
        if seg and seg not in _LITERALS and (prev in _COLLECTIONS or _ID_LIKE.match(seg)):
            out.append(":id")
        else:
            out.append(seg)
        prev = seg
    return "/".join(out) or "/"


def _json_of(line: str) -> dict | None:
    """The log record in a line. `docker compose logs` puts `relay-1  | ` in front of it."""
    i = line.find("{")
    if i < 0:
        return None
    try:
        rec = json.loads(line[i:])
    except ValueError:
        return None
    return rec if isinstance(rec, dict) else None


def _when(rec: dict) -> datetime | None:
    try:
        t = datetime.fromisoformat(str(rec["ts"]))
    except (KeyError, ValueError):
        return None
    return t if t.tzinfo else t.replace(tzinfo=UTC)


def summarize(lines: Iterable[str], *, since: timedelta, now: datetime | None = None) -> dict[str, Any]:
    """Read `lines` (streamed, never all held) and return the summary as a dict."""
    now = now or datetime.now(UTC)
    cutoff = now - since
    requests = 0
    first = last = None
    server_errors: list[dict] = []
    status5xx = 0
    four: Counter = Counter()
    expected_hidden = 0
    rate_limited: Counter = Counter()
    registered = 0
    signin: Counter = Counter()
    signin_expected = 0
    settled = failed = 0
    cents_settled = 0
    ws: Counter = Counter()
    clients: Counter = Counter()
    for line in lines:
        rec = _json_of(line)
        if rec is None:
            continue
        t = _when(rec)
        if t is None or t < cutoff:
            continue
        first = first or t
        last = t
        msg = rec.get("msg")
        if msg == "request" and isinstance(rec.get("status_code"), int):
            requests += 1
            code = rec["status_code"]
            clients[ip_tag(rec.get("client_ip"))] += 1
            if code >= 500:
                status5xx += 1
                server_errors.append({"ts": rec.get("ts"), "request_id": rec.get("request_id"),
                                      "path": rec.get("path"), "status": code, "exception_type": ""})
            elif 400 <= code < 500:
                if rec.get("expected"):
                    expected_hidden += 1
                else:
                    four[(code, rec.get("method", ""), normalize_path(rec.get("path", "")))] += 1
        elif msg == "unhandled_error":
            server_errors.append({"ts": rec.get("ts"), "request_id": rec.get("request_id"),
                                  "path": rec.get("path"), "status": 500,
                                  "exception_type": rec.get("exception_type", "")})
        elif msg in ("rate_limited", "rate_limit_exceeded"):
            rate_limited[str(rec.get("limit") or "ip_" + str(rec.get("bucket", "?")))] += 1
        elif msg == "agent_registered":
            registered += 1
        elif msg == "signin_failed":
            signin[str(rec.get("reason", "?"))] += 1
            signin_expected += bool(rec.get("expected"))
        elif msg == "payment_settled":
            settled += 1
            cents_settled += int(rec.get("cents") or 0)
        elif msg == "payment_failed":
            failed += 1
        elif msg == "ws_rejected":
            ws[str(rec.get("reason", "?"))] += 1
    requested = max(since.total_seconds() / 60, 1 / 60)
    minutes = requested
    # a window that reaches back before the log began is measured from the first line it holds
    if first is not None and (first - cutoff) > timedelta(minutes=1):
        minutes = max((now - first).total_seconds() / 60, 1 / 60)
    # a 500 that raised through the middleware shows up as both an `unhandled_error` and, rarely, a 5xx
    # request line; the unhandled_error carries the exception type, so it is the one kept per request id
    seen: dict[str, dict] = {}
    for e in server_errors:
        key = e["request_id"] or id(e)
        if key not in seen or e["exception_type"]:
            seen[key] = e
    errors = list(seen.values())
    return {
        "window": {"since": cutoff.isoformat(timespec="seconds"), "minutes": round(minutes, 1),
                   "requested_minutes": round(requested, 1),
                   "first": first.isoformat(timespec="seconds") if first else None,
                   "last": last.isoformat(timespec="seconds") if last else None},
        "requests": requests,
        "requests_per_minute": round(requests / minutes, 1),
        "server_errors": len(errors),
        "last_server_errors": errors[-3:],
        "client_errors": [{"status": s, "method": m, "path": p, "count": n}
                          for (s, m, p), n in four.most_common(8)],
        "client_errors_total": sum(four.values()),
        "expected_hidden": expected_hidden,
        "rate_limited": dict(rate_limited),
        "registrations": registered,
        "signin_failures": dict(signin),
        "signin_failures_expected": signin_expected,
        "payments": {"settled": settled, "failed": failed, "cents_settled": cents_settled},
        "ws_rejected": dict(ws),
        "busiest_clients": [{"client": c, "count": n} for c, n in clients.most_common(3)],
    }


def verdict(summary: dict[str, Any], health: dict[str, Any] | None) -> tuple[str, bool]:
    """The one line, and whether it needs a person (exit code 1)."""
    why: list[str] = []
    if health is not None and not health.get("ok"):
        why.append("the relay does not answer /health")
    n = summary["server_errors"]
    if n:
        why.append(f"{n} server error{'s' if n != 1 else ''} in the last {_span(summary)}")
    f = summary["payments"]["failed"]
    if f:
        why.append(f"{f} payment{'s' if f != 1 else ''} failed")
    if why:
        return "attention: " + "; ".join(why), True
    return "all normal", False


def _span(summary: dict[str, Any]) -> str:
    m = summary["window"]["requested_minutes"]
    return f"{m / 60:g}h" if m >= 60 else f"{m:g}m"


def _top(counter: dict[str, int]) -> str:
    return ", ".join(f"{k} {v}" for k, v in sorted(counter.items(), key=lambda kv: -kv[1]))


def render(summary: dict[str, Any], health: dict[str, Any] | None, line: str) -> str:
    out = [f"relay watch: last {_span(summary)}"
           + (f" (log from {summary['window']['first']})" if summary["window"]["first"] else " (no log lines)")]
    if health is None:
        out.append("health:    not checked")
    elif health.get("ok"):
        out.append(f"health:    ok, v{health.get('version', '?')}, schema {health.get('schema_version', '?')}")
    else:
        out.append(f"health:    NOT OK ({health.get('why', 'no answer')})")
    out.append(f"requests:  {summary['requests']} ({summary['requests_per_minute']}/min)")
    out.append(f"5xx:       {summary['server_errors']}")
    for e in summary["last_server_errors"]:
        out.append(f"           {e['ts']}  {e['request_id']}  {e['path']}  {e['exception_type'] or e['status']}")
    four = summary["client_errors"]
    out.append(f"4xx:       {summary['client_errors_total']}"
               + (f" (+{summary['expected_hidden']} expected, hidden)" if summary["expected_hidden"] else ""))
    for c in four:
        out.append(f"           {c['count']:>4}  {c['status']} {c['method']} {c['path']}")
    rl = summary["rate_limited"]
    out.append(f"limits:    {sum(rl.values())} rate-limited" + (f" ({_top(rl)})" if rl else ""))
    si = summary["signin_failures"]
    out.append(f"accounts:  {summary['registrations']} registered; {sum(si.values())} sign-in failures"
               + (f" ({_top(si)}; {summary['signin_failures_expected']} expected)" if si else ""))
    p = summary["payments"]
    out.append(f"payments:  {p['settled']} settled ({p['cents_settled']} cents), {p['failed']} failed")
    ws = summary["ws_rejected"]
    out.append(f"websocket: {sum(ws.values())} rejected" + (f" ({_top(ws)})" if ws else ""))
    if summary["busiest_clients"]:
        out.append("clients:   " + ", ".join(f"{c['client']} x{c['count']}" for c in summary["busiest_clients"]))
    out.append(f"verdict:   {line}")
    return "\n".join(out)
