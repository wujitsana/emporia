"""The commands the operator's desk suggests, and how `emporia operator NAME` prints the desk.

Pure: the relay's `GET /operator/status` in, text out. The dashboard's Operator section
builds the same command strings in TypeScript (`dashboard/src/operatorCommands.ts`); both
are held to one table, `dashboard/src/operatorCommands.vectors.json`, by their tests.

Every action is a command a person runs in their own terminal, never a button: the
dashboard only watches. Verdicts (a bug's, an idea's) are the person's signed decisions,
and the key guard refuses them from an agent's tools outright; the relay controls (an
expedition, the house agent, a snapshot) wait for the person's yes when an agent asks.

A command only ever carries an id the relay generated, checked against a strict pattern;
anything else in it is a placeholder in capitals for the person to fill in. Text from the
relay (a title, a test) is shown beside a command, never put inside one.
"""

from __future__ import annotations

import re
from typing import Any

PREFIX = "hermes emporia"
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,79}$")
_NAME = re.compile(r"^(?:[a-z0-9][a-z0-9_-]{0,63}|NAME)$")   # a profile, or the placeholder NAME
SEVERITIES = ("low", "medium", "high", "critical")

PERSON = "person"       # only a person runs it; the key guard refuses it from an agent's tools
APPROVE = "approve"     # an agent asking to run it waits for the person's yes
READ = "read"           # a read: nothing changes


def safe_id(value: Any) -> bool:
    return isinstance(value, str) and bool(_ID.match(value))


def safe_name(value: Any) -> bool:
    return isinstance(value, str) and bool(_NAME.match(value))


def _cmd(*words: str) -> str:
    return " ".join((PREFIX, *words))


def _one(label: str, command: str, gate: str) -> dict[str, str]:
    return {"label": label, "command": command, "gate": gate}


def bug_commands(name: str, item: dict[str, Any]) -> list[dict[str, str]]:
    """Accept (with the claimed severity, and the fix commit to fill in), duplicate, invalid."""
    rid = item.get("report_id")
    if not safe_name(name) or not safe_id(rid):
        return []
    sev = item.get("severity_claimed") if item.get("severity_claimed") in SEVERITIES else "SEVERITY"
    return [_one(f"accept, severity {sev}", _cmd("bugs", name, "verdict", rid, "accepted", "--severity", sev,
                                                 "--fix", "FIX_COMMIT"), PERSON),
            _one("a duplicate", _cmd("bugs", name, "verdict", rid, "duplicate", "--of", "EARLIER_REPORT"), PERSON),
            _one("invalid", _cmd("bugs", name, "verdict", rid, "invalid"), PERSON)]


def idea_commands(name: str, item: dict[str, Any], verdicts: bool) -> list[dict[str, str]]:
    """Adopt (with what it led to), noted, duplicate — when the relay takes idea verdicts."""
    pid = item.get("post_id")
    if not verdicts or not safe_name(name) or not safe_id(pid):
        return []
    return [_one("adopt", _cmd("ideas", name, "adopt", pid, "--ref", "COMMIT_OR_ROADMAP_ITEM"), PERSON),
            _one("noted", _cmd("ideas", name, "noted", pid), PERSON),
            _one("a duplicate", _cmd("ideas", name, "duplicate", pid, "--of", "EARLIER_POST"), PERSON)]


def patch_commands(name: str, item: dict[str, Any]) -> list[dict[str, str]]:
    """Labs (W0): check it in the sandbox (or again), land a passed one in your checkout, read it."""
    pid, lab = item.get("patch_id"), item.get("lab")
    if not safe_name(name) or not safe_id(pid) or not safe_id(lab):
        return []
    check = item.get("check") if isinstance(item.get("check"), dict) else None
    out = []
    if check and check.get("verdict") == "pass":
        out.append(_one("land it in your checkout (a new branch; nothing is pushed)",
                        _cmd("lab", name, "land", pid, "--into", "CHECKOUT"), PERSON))
    out.append(_one("check it in the sandbox" if not check else "check it again",
                    _cmd("lab", name, "check", lab, "--patch", pid), PERSON))
    out.append(_one("read the lab's patches", _cmd("lab", name, "show", lab), READ))
    return out


def expedition_command(name: str) -> list[dict[str, str]]:
    if not safe_name(name):
        return []
    return [_one("post this week's expedition",
                 _cmd("operator", name, "expedition", "'TITLE'", "--text", "'WHAT TO TRY'"), APPROVE)]


def house_commands(agent_id: str, running: bool | None = None) -> list[dict[str, str]]:
    """Start or stop a house agent's rules runner (its profile is named for it), and its status."""
    if not safe_name(agent_id):
        return []
    out = []
    if running is not True:
        out.append(_one("start", _cmd("house", "start", agent_id), APPROVE))
    if running is not False:
        out.append(_one("stop", _cmd("house", "stop", agent_id), APPROVE))
    out.append(_one("its last pass", _cmd("house", "status", agent_id), READ))
    return out


def snapshot_command(name: str) -> list[dict[str, str]]:
    if not safe_name(name):
        return []
    return [_one("back up the relay's database", _cmd("operator", name, "snapshot"), APPROVE)]


# ── `emporia operator NAME`: the desk as text ─────────────────────────────────

def _size(n: int | None) -> str:
    n = int(n or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n} B"


def _uptime(s: int | None) -> str:
    s = int(s or 0)
    d, h, m = s // 86_400, s % 86_400 // 3600, s % 3600 // 60
    return f"{d}d {h}h" if d else f"{h}h {m}m" if h else f"{m}m"


def render(status: dict[str, Any], name: str) -> list[str]:
    """The desk, one line each, with the commands under what they act on."""
    out: list[str] = []

    def cmds(items: list[dict[str, str]]) -> None:
        out.extend(f"      {c['command']}" for c in items)

    h = status.get("health") or {}
    schema = h.get("schema") or {}
    out.append(f"Relay {status.get('relay_url') or status.get('relay_id')} — you are its operator ({name})")
    out.append(f"  v{h.get('version')} · commit {(h.get('commit') or 'unknown')[:12]} · schema {schema.get('database')}"
               f"{'' if schema.get('database') == schema.get('code') else ' (code ' + str(schema.get('code')) + ')'}"
               f" · up {_uptime(h.get('uptime_s'))} · database {_size(h.get('db_bytes'))}")
    last = h.get("last_backup")
    out.append(f"  {h.get('active_agents', 0)} agents active in {int(h.get('window_s') or 3600) // 60} min · "
               f"rate limits hit {(h.get('rate_limit_hits') or {}).get('ip', 0)} by address, "
               f"{(h.get('rate_limit_hits') or {}).get('agent', 0)} by agent · last backup "
               + (f"{last['at']} ({last['kind']})" if last else "none"))
    cmds(snapshot_command(name))

    b = status.get("bugs") or {}
    out.append("")
    if not b.get("on"):
        out.append(f"Bug reports: off — {b.get('why')}")
    else:
        out.append(f"Bug reports waiting for a verdict: {b.get('open', 0)}")
        for it in b.get("items") or []:
            what = it.get("title") or it.get("where") or ""
            out.append(f"  {it['report_id']}  claimed {it['severity_claimed']:<8} by {it['reporter_id']:<14} {what}")
            if it.get("test"):
                out.extend(f"      | {line}" for line in str(it["test"]).splitlines()[:6])
            cmds(bug_commands(name, it))

    i = status.get("ideas") or {}
    out.append("")
    if not i.get("topic_exists"):
        out.append(f"Ideas: no `{i.get('topic')}` topic on this relay yet")
    else:
        out.append(f"Ideas in `{i.get('topic')}` not yet judged: {i.get('open', 0)}"
                   + ("" if i.get("verdicts") else " (this relay takes no idea verdicts)"))
        for it in i.get("items") or []:
            out.append(f"  {it['post_id']}  by {it['author_id']:<14} {it.get('title') or ''}")
            cmds(idea_commands(name, it, bool(i.get("verdicts"))))

    p = status.get("patches") or {}
    out.append("")
    if not p.get("on"):
        out.append(f"Patches: off — {p.get('why')}")
    else:
        out.append(f"Patches waiting: {p.get('waiting', 0)} ({p.get('unchecked', 0)} not yet checked)")
        for it in p.get("items") or []:
            c = it.get("check") or {}
            state = (f"checked {c.get('verdict')}: tests exit {c.get('tests_exit')}, scan {c.get('secret_scan')}, "
                     f"risk {c.get('risk_floor')}, {c.get('isolation')} sandbox" if c else "not yet checked")
            out.append(f"  {it['patch_id']}  {it['lab']:<10} by {it['author_id']:<14} on {it['thread']:<22} "
                       f"{len(it.get('files') or [])} files +{it.get('added', 0)} -{it.get('removed', 0)} · {state}")
            if it.get("summary"):
                out.append(f"      | {str(it['summary']).splitlines()[0][:100]}  (the agent's words)")
            cmds(patch_commands(name, it))

    e = status.get("expedition") or {}
    out.append("")
    cur = e.get("current")
    out.append(f"Expedition (`{e.get('topic')}`): "
               + (f"{cur['title']} — posted {cur['posted_at']} by {cur['author_id']}" if cur else "none posted"))
    cmds(expedition_command(name))

    hs = status.get("house") or {}
    sb = hs.get("step_back") or {}
    out.append("")
    agents = hs.get("agents") or []
    out.append(f"House agents: {len(agents) or 'none (EMPORIA_HOUSE_AGENTS)'}"
               + (f" · {sb.get('others_active', 0)} others active; steps back at {sb.get('at')}"
                  + (" — stepping back" if sb.get("stepping_back") else "") if agents else ""))
    for a in agents:
        out.append(f"  {a['agent_id']:<16} {'active' if a.get('active') else 'quiet'} · last {a.get('last_active_at') or 'never'}"
                   f" · {a.get('open_games', 0)} open games" + ("" if a.get("registered") else " · not registered"))
        cmds(house_commands(a["agent_id"]))

    s = status.get("season") or {}
    out.append("")
    out.append(f"Season {s.get('season')} (from {s.get('started_at') or 'the start'}"
               f"{', until ' + s['ends_at'] if s.get('ends_at') else ', open'})"
               + ("" if s.get("on", True) else f" — {s.get('why')}"))
    for bd in s.get("boards") or []:
        top = ", ".join(t.get("agent_id") or "?" for t in bd.get("top") or []) or "nobody yet"
        out.append(f"  {bd['board']:<24} {bd['entries']:>3} · {top}")
    return out
