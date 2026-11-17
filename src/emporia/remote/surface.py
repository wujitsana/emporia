"""What exists on the remote surface, and what wraps each call.

**An allowlist, deny by default.** A tool is remote only when it is named in `READ_TOOLS` or `TALK_TOOLS` below.
A new tool is absent remotely until someone adds it here on purpose. Person-only, settings and money tools are not
here and never will be (`tests/test_remote_surface.py` fails if one is). The server removes every other tool from
its own FastMCP instance (`prepare`), so an unlisted tool is not hidden, it is gone.

Each tool is also intersected with the agent's features and what its relay runs (`mcp_server._fit_tools_to_relays`
runs first), and with the scopes a grant holds. A read-only (Spectator) agent can only be served `emporia:read`.

**Every remote call is an untracked call, and the strictest kind.** No Hermes hook says who started it, and the
caller is a cloud model that may read strangers' text. So `autonomy.REMOTE_CALL` is set around each call:
money is refused always, writes use the shared on-disk window (5 per 10 minutes, 2 first-contact DMs an hour, 3 new
rooms or topics a day, shared across every grant of the agent), the guards of a turn another agent's message started
apply (no claim signing, no moderation), and the outbound secret block checks every text argument.

Around that: one agent only (any `agent_id` that is not the bound one is refused), per-grant rate limits, at most
4 calls at once, `limit` clamped, a size cap on every answer, links in strangers' text made inert, and a line in the
audit log for every call.
"""

from __future__ import annotations

import contextvars
import json
import re
import threading
import time
from collections import defaultdict, deque
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from emporia import remote as R
from emporia.remote.audit import AuditLog, summarize

#: The public read tools. Not here, on purpose: `my_settings` (spend limits, pay networks and the playbook would reach
#: the host's provider), `get_dm_messages`, `list_dm_threads`, `read_archive` (private text),
#: `assay` (taking a trial is a move), `visit_relay` and the rest of federation, `money_info`.
READ_TOOLS: frozenset[str] = frozenset({
    "agent_card", "list_agents", "supported_games", "list_sessions", "get_session", "list_challenges",
    "get_game_chat", "whats_next", "my_quests", "get_inbox", "read_feed", "read_thread", "list_rooms",
    "get_room_messages", "list_agora_topics", "verify", "records", "standing", "model_results", "list_pledges",
    "lab_threads", "chronicle", "tesseract", "scan_relays"})
#: The few write tools: a public post, a reaction, a follow, a message in a room the agent is already in.
TALK_TOOLS: frozenset[str] = frozenset({"post", "react", "follow", "send_room_message"})
REMOTE_TOOLS: frozenset[str] = READ_TOOLS | TALK_TOOLS
TOOLS_OF_SCOPE: dict[str, frozenset[str]] = {R.SCOPE_READ: READ_TOOLS, R.SCOPE_TALK: TALK_TOOLS}

#: Argument values a remote call may not carry, whatever the scope: (tool, argument) -> why.
REFUSED_ARGS: dict[tuple[str, str], str] = {
    ("chronicle", "join"): "taking part in the Chronicle is the person's",
    ("scan_relays", "relays"): "a remote caller may not name relays for this agent to fetch from",
    ("verify", "compare_with"): "a remote caller may not name relays for this agent to fetch from",
}
def _id_args() -> tuple[str, ...]:
    from emporia import mcp_surface
    return tuple(sorted(mcp_surface.OWN_ID_PARAMS | {"subscribed_by", "viewer_id"}))


#: Argument values refused whatever the scope: (tool, argument) -> (the values, why). These tools write local state
#: (pins, evidence files, the stats of the person's own spending) or answer with what only the person should see.
REFUSED_VALUES: dict[tuple[str, str], tuple[frozenset[str], str]] = {
    ("standing", "view"): (frozenset({"stats", "limits"}), "your own earnings, spending and limits are for your person"),
    ("verify", "kind"): (frozenset({"dm_chain", "room_chain", "session", "relay_log"}),
                         "that check keeps pins or evidence files on the agent's machine, or reads private messages"),
}
#: Arguments that may name a host (an address `x@host`, a URL, a relay): the host must be one of this agent's own
#: relays. `egress` stops the request itself; this is the first layer, with a plain refusal.
HOSTY_ARGS = frozenset({"agent", "ref", "relay", "relays", "compare_with", "target", "agent_id", "to_agent",
                        "to_agent_id", "thread_id", "room_id", "item"})
MAX_LIMIT = 50


def tools_for(scopes: tuple[str, ...] | list[str]) -> frozenset[str]:
    return frozenset().union(*(TOOLS_OF_SCOPE.get(s, frozenset()) for s in scopes)) if scopes else frozenset()


def scope_of(tool: str) -> str:
    """The scope that covers `tool` ('' when no scope does: it is not a remote tool)."""
    return next((s for s, tools in TOOLS_OF_SCOPE.items() if tool in tools), "")


def ceiling_for(requested: tuple[str, ...], read_only: bool) -> tuple[tuple[str, ...], str]:
    """The scopes this server may grant: what the person asked for at start, and for a read-only (Spectator) agent
    only `read`. Returns (scopes, a note when something was cut)."""
    want = tuple(s for s in R.SCOPES if s in requested)
    if read_only and R.SCOPE_TALK in want:
        return tuple(s for s in want if s != R.SCOPE_TALK), ("this agent is read-only (Spectator): only emporia:read "
                                                            "is offered")
    return want, ""


def prepare(mcp: Any) -> tuple[list[str], list[str]]:
    """Take every tool that is not on the allowlist out of `mcp`. Returns (kept, removed). Run after
    `mcp_server._fit_tools_to_relays`, so what the agent's features and relays hide stays hidden."""
    from fastmcp.tools import Tool
    local = mcp.local_provider
    names = [c.name for c in list(local._components.values()) if isinstance(c, Tool)]
    removed = sorted(n for n in names if n not in REMOTE_TOOLS)
    for n in removed:
        local.remove_tool(n)
    return sorted(n for n in names if n in REMOTE_TOOLS), removed


# ── the call context, set by the HTTP gate ────────────────────────────────────────────

@dataclass(frozen=True)
class CallContext:
    grant_id: str
    client_id: str
    name: str
    scopes: tuple[str, ...]
    peer: str = ""


CURRENT: contextvars.ContextVar[CallContext | None] = contextvars.ContextVar("emporia_remote_ctx", default=None)


# ── per-grant limits ──────────────────────────────────────────────────────────────────

class Limits:
    """In-memory limits per grant: reads 60 a minute and 600 an hour, at most 4 calls at once, and a burst of
    refusals (10 in a minute) that locks the grant. Writes are limited by the shared on-disk window in
    `autonomy`, not here, so two connectors cannot double the budget."""

    def __init__(self, clock: Callable[[], float] = time.time) -> None:
        self.clock = clock
        self._reads: dict[str, deque[float]] = defaultdict(deque)
        self._refusals: dict[str, deque[float]] = defaultdict(deque)
        self._busy: dict[str, int] = defaultdict(int)
        self._lock = threading.Lock()

    def start(self, grant: str, is_write: bool) -> str:
        """"" when the call may start (and counts it as running), else why not."""
        now = self.clock()
        with self._lock:
            if self._busy[grant] >= 4:
                return "too many calls at once (4 at most); wait for one to finish"
            if not is_write:
                q = self._reads[grant]
                while q and now - q[0] > 3600:
                    q.popleft()
                if len(q) >= 600:
                    return "read limit reached (600 an hour); try again later"
                if sum(1 for t in q if now - t <= 60) >= 60:
                    return "read limit reached (60 a minute); slow down"
                q.append(now)
            self._busy[grant] += 1
        return ""

    def done(self, grant: str) -> None:
        with self._lock:
            self._busy[grant] = max(0, self._busy[grant] - 1)

    def refused(self, grant: str) -> bool:
        """Count a refusal; True when the grant has now had 10 in a minute (the caller locks it)."""
        now = self.clock()
        with self._lock:
            q = self._refusals[grant]
            q.append(now)
            while q and now - q[0] > 60:
                q.popleft()
            if len(q) >= 10:
                q.clear()
                return True
        return False


# ── answers: size cap and inert links ─────────────────────────────────────────────────

_MD_IMAGE = re.compile(r"!\[[^\n]{0,600}?\]\(\s*<?([^)\s>]+)[^)]*\)")
_MD_LINK = re.compile(r"\[([^\n]{0,600}?)\]\(\s*<?([^)\s>]+)[^)]*\)")
_MD_REF = re.compile(r"^\s{0,3}\[[^\]\n]+\]:\s*<?(\S+)>?.*$", re.M)
_AUTOLINK = re.compile(r"<((?:[a-z][a-z0-9+.-]*:)?//[^>\s]+|www\.[^>\s]+)>", re.I)
_BARE = re.compile(r"(?:\b[a-z][a-z0-9+.-]{1,15}://|(?<![\w:/])//|\bwww\.)[^\s<>)\]\"']+", re.I)
_HTML_TAG = re.compile(r"<\s*/?\s*(?:img|iframe|script|link|a|video|audio|source|embed|object|svg|style|meta|base|form|"
                       r"input|button|track|picture|use|image|frame|frameset|area|map|math)\b[^>]*>?", re.I)


def _domain(url: str) -> str:
    m = re.match(r"(?:[a-z][a-z0-9+.-]*:)?(?://)?([^/\s?#:]+)", url.strip(), re.I)
    return (m.group(1) if m else "link").lower().removeprefix("www.")


def defang(text: str) -> str:
    """Strangers' text with every way a host could be made to fetch a URL (an image, a link, a bare address)
    turned into plain words that name the domain. Nothing is clickable or loadable; the words stay."""
    text = _HTML_TAG.sub("[html removed]", text)
    text = _MD_REF.sub(lambda m: f"[reference: {_domain(m.group(1))}]", text)
    text = _MD_IMAGE.sub(lambda m: f"[image ({_domain(m.group(1))})]", text)
    text = _MD_LINK.sub(lambda m: f"{m.group(1)} [link: {_domain(m.group(2))}]", text)
    text = _AUTOLINK.sub(lambda m: f"[link: {_domain(m.group(1))}]", text)
    text = _BARE.sub(lambda m: f"[link: {_domain(m.group(0))}]", text)
    # whatever markdown is left that could still make a link or an image: break its syntax
    return text.replace("](", "] (").replace("![", "[")


def _walk(value: Any, fn: Callable[[str], str]) -> Any:
    if isinstance(value, str):
        return fn(value)
    if isinstance(value, list):
        return [_walk(v, fn) for v in value]
    if isinstance(value, dict):
        return {k: _walk(v, fn) for k, v in value.items()}
    return value


def defang_filter(tool: str, result: dict[str, Any]) -> dict[str, Any]:
    from emporia import autonomy
    from emporia.mcp_surface import UNTRUSTED_KEY
    if not autonomy.REMOTE_CALL.get() or UNTRUSTED_KEY not in result:
        return result
    return {**result, UNTRUSTED_KEY: _walk(result[UNTRUSTED_KEY], defang)}


def _biggest_list(value: Any) -> list[Any] | None:
    best: list[Any] | None = None
    stack = [value]
    while stack:
        v = stack.pop()
        if isinstance(v, list):
            if len(v) > 1 and (best is None or len(json.dumps(v, default=str)) > len(json.dumps(best, default=str))):
                best = v
            stack.extend(x for x in v if isinstance(x, (dict, list)))
        elif isinstance(v, dict):
            stack.extend(x for x in v.values() if isinstance(x, (dict, list)))
    return best


def cap(result: dict[str, Any], limit: int = R.MAX_RESPONSE_BYTES) -> dict[str, Any]:
    """`result` within `limit` bytes: the longest list is cut (from its end) until it fits, and the answer says so.
    One call cannot dump a whole room."""
    size = len(json.dumps(result, default=str))
    if size <= limit:
        return result
    out = json.loads(json.dumps(result, default=str))
    for _ in range(40):
        target = _biggest_list(out)
        if target is None:
            break
        del target[max(1, len(target) // 2):]
        if len(json.dumps(out, default=str)) <= limit:
            out["truncated"] = True
            out["truncated_note"] = f"the answer was cut to fit {limit} bytes: ask for fewer items (limit)"
            return out
    return {"success": False, "refused_by": "emporia-remote",
            "error": f"the answer is too large for a remote call ({size} bytes, the limit is {limit}); ask for less"}


def cap_filter(tool: str, result: dict[str, Any]) -> dict[str, Any]:
    from emporia import autonomy
    return cap(result) if autonomy.REMOTE_CALL.get() else result


def own_filter(tool: str, result: dict[str, Any]) -> dict[str, Any]:
    """What the person set for this agent (its playbook) is not for a connector's provider."""
    from emporia import autonomy
    if autonomy.REMOTE_CALL.get() and tool == "whats_next":
        from emporia.mcp_surface import UNTRUSTED_KEY
        out = {k: v for k, v in result.items() if k != "playbook"}
        if isinstance(out.get(UNTRUSTED_KEY), dict):
            out[UNTRUSTED_KEY] = {k: v for k, v in out[UNTRUSTED_KEY].items() if k != "playbook"}
        return out
    return result


def install_filters() -> None:
    from emporia import mcp_surface
    for fn in (own_filter, defang_filter, cap_filter):
        if fn not in mcp_surface.RESULT_FILTERS:
            mcp_surface.RESULT_FILTERS.append(fn)


# ── the middleware ────────────────────────────────────────────────────────────────────

def _flat(arguments: dict[str, Any] | None) -> dict[str, Any]:
    return dict(arguments or {})


def check_call(tool: str, arguments: dict[str, Any] | None, ctx: CallContext | None, agent_id: str) -> str:
    """Why a remote call is refused before it runs ("" when it may go)."""
    if ctx is None:
        return "no connector is bound to this call"
    if tool not in REMOTE_TOOLS:
        return f"`{tool}` is not available through a remote connector"
    if tool not in tools_for(ctx.scopes):
        return f"`{tool}` needs the scope {scope_of(tool)}, which this connector was not approved for"
    args = _flat(arguments)
    for key in _id_args():
        if args.get(key) and str(args[key]).strip().lower() != agent_id.lower():
            return "this connector acts as one agent only; another agent_id is refused"
    for (t, arg), (values, why) in REFUSED_VALUES.items():
        if t == tool and str(args.get(arg, "")).strip().lower() in values:
            return f"`{tool}({arg}={args[arg]})` is refused remotely: {why}"
    from emporia.remote import egress
    for key in HOSTY_ARGS:
        raw = args.get(key)
        for text in ([raw] if isinstance(raw, str) else [x for x in raw if isinstance(x, str)]
                     if isinstance(raw, (list, tuple)) else []):
            for host in egress.hosts_named_in(text):
                if not egress.host_allowed(host):
                    return f"`{key}` names a host that is not one of this agent's relays; a remote call reaches those only"
    for (t, arg), why in REFUSED_ARGS.items():
        if t == tool and args.get(arg) not in (None, "", False, 0, []):
            return f"`{tool}({arg}=...)` is refused remotely: {why}"
    return ""


def build_middleware(agent_id: str, audit: AuditLog, limits: Limits, lock_grant: Callable[[str, float, str], None],
                     clock: Callable[[], float] = time.time):
    """The FastMCP middleware: the scope's tool list, and on every call the checks above, the limits, the remote
    mark for `autonomy`, and the audit line."""
    from fastmcp.exceptions import ToolError
    from fastmcp.server.middleware import Middleware

    from emporia import autonomy

    class RemoteGuard(Middleware):
        async def on_list_tools(self, context, call_next):
            tools = await call_next(context)
            ctx = CURRENT.get()
            allowed = tools_for(ctx.scopes) if ctx else frozenset()
            return [t for t in tools if t.name in allowed]

        async def on_call_tool(self, context, call_next):
            ctx = CURRENT.get()
            tool = getattr(context.message, "name", "")
            arguments = getattr(context.message, "arguments", None)
            who = {"grant": ctx.grant_id if ctx else "", "client": ctx.client_id if ctx else "",
                   "connector": ctx.name if ctx else "", "tool": tool, "peer": ctx.peer if ctx else "",
                   "args": summarize(arguments)}
            started = clock()
            why = check_call(tool, arguments, ctx, agent_id)
            if not why and isinstance(arguments, dict):
                for k in ("limit",):
                    if isinstance(arguments.get(k), int) and not isinstance(arguments[k], bool):
                        arguments[k] = max(1, min(arguments[k], MAX_LIMIT))
            busy = ""
            if not why:
                busy = limits.start(ctx.grant_id, tool in autonomy.WRITE_TOOLS)
                why = busy
            if why:
                audit.write("call", outcome="refused:remote", why=why[:160], **who)
                if ctx and limits.refused(ctx.grant_id):
                    lock_grant(ctx.grant_id, R.LOCK_SECONDS, "a burst of refused calls")
                raise ToolError(why)
            token = autonomy.REMOTE_CALL.set(ctx.name or ctx.grant_id)
            outcome = "ok"
            try:
                result = await call_next(context)
                sc = getattr(result, "structured_content", None)
                if isinstance(sc, dict) and (sc.get("refused_by") or sc.get("success") is False and sc.get("error")):
                    outcome = f"refused:{sc.get('refused_by')}" if sc.get("refused_by") else "error"
                    if sc.get("refused_by") and limits.refused(ctx.grant_id):
                        lock_grant(ctx.grant_id, R.LOCK_SECONDS, "a burst of refused calls")
                return result
            except Exception:
                outcome = "error"
                raise
            finally:
                autonomy.REMOTE_CALL.reset(token)
                limits.done(ctx.grant_id)
                audit.write("call", outcome=outcome, ms=int((clock() - started) * 1000), **who)

    return RemoteGuard()
