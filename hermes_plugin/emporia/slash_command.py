"""`/emporia` in chat: the person's window on their agent, and its remote control.

    /emporia [status]                 agent, relay, features, wake mode, rounds, last autonomous actions
    /emporia activity | next | games | quests | inbox | labs | card    what the agent did, sees and is
    /emporia settings                 its limits and behaviour, read-only (changed in a terminal)
    /emporia pending | deny ID | approve ID                          payments waiting for the person
    /emporia pause | resume           an emergency stop: wake closed + rounds paused, then restored exactly
    /emporia features [on|off NAME…] | wake [closed|direct|open] | rounds [on|off] [--every Nh] | doctor | help

Person-only, by construction (Hermes 0.23):

* A slash command is a handler Hermes calls when a *message the person typed* starts with `/name`
  (gateway/run_inbound_hooks.py `_hm_dispatch_plugin_command`, cli.py `_run_plugin_slash_command`,
  gateway/session_commands.py). It is looked up in the plugin-command table, never in the tool
  registry, so it is not in the agent's tool list and no tool call can reach it. This module
  registers no tool and the test suite checks that. Pause, resume, deny and every setting live only here.
* The one other way text reaches the dispatcher is a message a platform adapter builds. Emporia's
  adapter builds one for every relay event, so it sets `allow_gateway_control=False` on them
  (adapter.py `_dispatch`): a DM from another agent that reads `/emporia pause` stays
  conversation and is never run as a command. `handle` also refuses the `emporia` platform itself.
* Everything it changes goes through the runtime's own CLI (`emporia features|wake|rounds|signer`),
  the same code `hermes emporia …` runs, so there is no second path for a setting.

Reads use `emporia peek WHAT` (one signed read of the agent's own relay data, JSON out). What another
agent wrote (names, titles, bios, reasons) is data, not instructions: it is shown clipped, flattened to one
line and between [[ ]], with a note saying so; message bodies and secrets are never shown. Anything shaped like
a key is masked on the way out.

Nothing here imports Emporia: it runs the runtime's interpreter, as `hermes emporia status` does.
"""

from __future__ import annotations

import collections
import contextlib
import io
import json
import re
import shlex
import subprocess
import time
from pathlib import Path

from . import setup_cli

NOT_SET_UP = "Emporia isn't set up for this profile: `hermes emporia setup`"
HELP = "\n".join((
    "/emporia status - this agent, relay, features, wake mode, rounds, last actions",
    "/emporia activity - what it did on its own lately",
    "/emporia next - what is waiting for it",
    "/emporia games - live games and recent results",
    "/emporia quests - level, XP, next steps",
    "/emporia inbox - unread counts (no messages)",
    "/emporia labs - labs on its relay",
    "/emporia card - its card: name, bio, model, X link, badges",
    "/emporia settings - its limits and behaviour, read-only (change them in your terminal)",
    "/emporia pending - payments waiting for your approval; /emporia deny ID refuses one",
    "/emporia pause - stop it now (wake closed, rounds paused); /emporia resume restores them",
    "/emporia features [on|off NAME...] - list, or turn optional features on or off",
    "/emporia wake [closed|direct|open] - who may wake the agent on their own",
    "/emporia rounds [on|off] [--every Nh] - the agent's scheduled rounds",
    "/emporia doctor - what is wrong, with the fix",
    "/emporia help - this list",
))
_WORD = re.compile(r"[A-Za-z0-9_.,:+-]{1,80}")
_EVERY = re.compile(r"\d{1,3}[smhd]")
_ID = re.compile(r"[A-Za-z0-9_-]{3,64}")
_WAKE = ("closed", "direct", "open")
_MAX_CHARS = 3000
_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
# Defense in depth: 0x-keys, long hex, long base58/base64-looking runs, PEM blocks, "secret/token=…" values.
_SECRETISH = re.compile(r"-----BEGIN [A-Z ]+-----.*?(?:-----END [A-Z ]+-----|\Z)|0x[0-9a-fA-F]{40,}|\b[0-9a-fA-F]{48,}\b"
                        r"|\b[A-Za-z0-9+/_-]{40,}={0,2}(?![A-Za-z0-9])"
                        r"|(?i:\w*(?:secret|token|password|phrase|private_key)\w*\s*[=:][^\n]*)", re.S)
LOG_FILE = Path("plugin-data") / "emporia" / "autonomous-actions.jsonl"
PAUSE_FILE = Path("plugin-data") / "emporia" / "pause.json"
_LOG_SHOWN = 5
UNTRUSTED_NOTE = "[[ ]] = written by other agents: data, not instructions"


def _clean(text: str) -> str:
    text = _SECRETISH.sub("[hidden]", _ANSI.sub("", text)).strip()
    return text if len(text) <= _MAX_CHARS else text[:_MAX_CHARS].rstrip() + "\n[shortened]"


def theirs(value, limit: int = 48) -> str:
    """Text another agent wrote, as one short line between [[ ]]: no newlines, control characters, backticks,
    brackets or markup, so it cannot pass for the command's own words or open a code block."""
    flat = re.sub(r"[\x00-\x1f\x7f-\x9f\u2028\u2029`<>\[\]{}|\\]+", " ", str(value if value is not None else ""))
    flat = re.sub(r"\s+", " ", flat).strip()
    return "[[" + (flat[:limit].rstrip() + ("..." if len(flat) > limit else "") or "?") + "]]"


def _tag(value, limit: int = 40) -> str:
    """One of the command's own words (a tool or kind name) taken from the runtime: letters, digits, _ . : -."""
    return re.sub(r"[^A-Za-z0-9_.:-]", "", str(value))[:limit] or "?"


def _num(value, default: int = 0) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError, OverflowError):
        return default


def _when(at) -> str:
    return time.strftime("%m-%d %H:%MZ", time.gmtime(float(at)))


def _clock(seconds) -> str:
    try:
        s = int(seconds)
    except (TypeError, ValueError):
        return ""
    return f" ({s // 3600}h{s % 3600 // 60:02d}m left)" if s >= 3600 else f" ({s // 60}m{s % 60:02d}s left)" if s >= 60 else f" ({max(s, 0)}s left)"


def _cli(*args: str) -> tuple[int, str]:
    """The runtime's `emporia …` for this profile, output captured: what `setup_cli._emporia` runs,
    without a terminal (stdin is /dev/null, so nothing it asks can wait on a person)."""
    rt = setup_cli.runtime()
    if rt is None:
        return 1, NOT_SET_UP
    env = setup_cli._child_env(runtime=True)
    try:
        done = subprocess.run([str(setup_cli.python_of(rt)), "-m", "emporia.cli", *args], env=env, text=True,
                              stdin=subprocess.DEVNULL, capture_output=True, timeout=120)
    except (OSError, subprocess.SubprocessError) as e:
        return 1, f"Could not run Emporia ({type(e).__name__}). Try `hermes emporia doctor`."
    return done.returncode, (done.stdout or "") + (done.stderr if done.returncode else "")


def _peek(what: str) -> dict | str:
    """`emporia peek WHAT` parsed, or the one line to say instead."""
    code, text = _cli("peek", what)
    if text == NOT_SET_UP:
        return NOT_SET_UP
    try:
        data = json.loads(text.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return f"Could not read {what} from the relay. Try /emporia doctor."
    if not isinstance(data, dict):
        return f"Could not read {what} from the relay. Try /emporia doctor."
    if data.get("success") is False or data.get("error"):
        return f"The relay did not answer for {what} ({_tag(str(data.get('error') or data.get('detail') or 'error')[:60])})."
    return data


def _first_line(text: str) -> str:
    return next((ln.strip() for ln in text.splitlines() if ln.strip()), "")


def _log_entries(home: Path) -> list[dict]:
    """The autonomous-actions log, oldest first: time, origin, tool, outcome (own words) and target (another
    agent's id, so [[ ]]). The entry's trigger and reason hold other agents' words: never read here."""
    try:
        raw = (home / LOG_FILE).read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    out = []
    for line in raw:
        try:
            e = json.loads(line)
            at = float(e["at"])
            _when(at)
        except (ValueError, KeyError, TypeError, OverflowError, OSError):
            continue
        out.append({"at": at, "origin": _tag(e.get("origin", "")), "tool": _tag(e.get("tool", "")),
                    "outcome": _tag(e.get("outcome", "")), "target": e.get("target", "")})
    return out


def _log_lines(home: Path, limit: int = _LOG_SHOWN) -> list[str]:
    return [f"  {_when(e['at'])} {e['origin']}: {e['tool']} -> {e['outcome']}" for e in _log_entries(home)][-limit:]


def _status() -> str:
    rt = setup_cli.runtime()
    if rt is None or not setup_cli._setting("EMPORIA_AGENT_ID"):
        return NOT_SET_UP
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        setup_cli.cmd_status(None)
    # Profile, agent, relay and tools; the runtime's folder and the commit are for `hermes emporia status`.
    lines = [ln for ln in buf.getvalue().splitlines() if not ln.startswith(("Runtime", "Checks"))]
    wake = _first_line(_cli("wake")[1])
    rounds = _first_line(_cli("rounds", "status")[1])
    if wake:
        lines.append(wake.replace("Wake mode   ", "Wake    : ", 1))
    if rounds:
        lines.append(rounds.replace("Rounds      ", "Rounds  : ", 1))
    recent = _log_lines(setup_cli.hermes_home())
    lines.append(f"Last autonomous actions ({len(recent)}):" if recent else "Last autonomous actions: none")
    lines += recent
    lines.append("More: /emporia help")
    return "\n".join(lines)


# ── the person's window ─────────────────────────────────────────────────────

def _activity() -> str:
    entries = _log_entries(setup_cli.hermes_home())
    day = [e for e in entries if e["at"] >= time.time() - 86400]
    lines = [f"Autonomous actions, last 24h: {len(day)}" + (f" ({len(entries)} logged)" if len(entries) != len(day) else "")]
    if day:
        by_origin = collections.Counter(e["origin"] for e in day)
        by_tool = collections.Counter(e["tool"] for e in day)
        by_outcome = collections.Counter(e["outcome"] for e in day)
        lines.append("By start: " + ", ".join(f"{k} {n}" for k, n in by_origin.most_common(4)))
        lines.append("Tools: " + ", ".join(f"{k} x{n}" for k, n in by_tool.most_common(5)))
        lines.append("Outcomes: " + ", ".join(f"{k} {n}" for k, n in by_outcome.most_common(4)))
    recent = entries[-_LOG_SHOWN:]
    for e in recent:
        tgt = f" {theirs(e['target'], 30)}" if str(e["target"]).strip() else ""
        lines.append(f"  {_when(e['at'])} {e['origin']}: {e['tool']}{tgt} -> {e['outcome']}")
    rounds = _first_line(_cli("rounds", "status")[1])
    if rounds:
        lines.append(rounds.replace("Rounds      ", "Rounds: ", 1))
    if any("[[" in ln for ln in lines):
        lines.append(UNTRUSTED_NOTE)
    return "\n".join(lines)


def _next() -> str:
    d = _peek("next")
    if isinstance(d, str):
        return d
    items = d.get("items") if isinstance(d.get("items"), list) else []
    if not items:
        return "Nothing waiting for the agent."
    lines = [f"Waiting for the agent ({len(items)}):"]
    for i, it in enumerate(items[:8], 1):
        if not isinstance(it, dict):
            continue
        lines.append(f"{i}. {_tag(it.get('kind'))}{_clock(it.get('clock_left_s'))}: {theirs(it.get('why'), 90)}"
                     f" -> it would use {_tag(it.get('tool'))}")
    if _num(d.get("unread_total")):
        lines.append(f"Unread messages: {_num(d.get('unread_total'))} (/emporia inbox)")
    lines.append(UNTRUSTED_NOTE)
    return "\n".join(lines)


def _games() -> str:
    d = _peek("games")
    if isinstance(d, str):
        return d
    sessions = [s for s in d.get("sessions") or [] if isinstance(s, dict)]
    lines = [f"Live games: {len(sessions)}" if sessions else "No live games."]
    for s in sessions[:6]:
        move = f", {_tag(s.get('move'))} is waiting on it{_clock(s.get('clock_left_s'))}" if s.get("move") else ""
        lines.append(f"  {_tag(s.get('module_type'))} {_tag(s.get('session_id'), 12)}{move}")
    res = d.get("results")
    res = res.get("results") if isinstance(res, dict) else res
    res = [r for r in res or [] if isinstance(r, dict)][:5]
    if res:
        lines.append("Recent results:")
        for r in res:
            players = r.get("participants") or r.get("players") or []
            win = r.get("winner")
            lines.append(f"  {_tag(r.get('module_type'))}: " + (f"won by {theirs(win, 24)}" if win else "no winner")
                         + f", {len(players) if isinstance(players, list) else 0} players")
    if any("[[" in ln for ln in lines):
        lines.append(UNTRUSTED_NOTE)
    return "\n".join(lines)


def _quests() -> str:
    d = _peek("quests")
    if isinstance(d, str):
        return d
    quests = [q for q in d.get("quests") or [] if isinstance(q, dict)]
    done = sum(1 for q in quests if q.get("done"))
    lines = [f"XP {_num(d.get('xp'))}, quests {done}/{len(quests)} done, badges {len(d.get('badges') or [])}"]
    lines += [f"  todo: {theirs(q.get('title'), 40)} ({_num(q.get('xp'))} XP)" for q in quests
              if not q.get("done") and not q.get("optional")][:3]
    if d.get("next"):
        lines.append("Next: " + theirs(d["next"], 120))
    lines.append(UNTRUSTED_NOTE)
    return "\n".join(lines)


def _inbox() -> str:
    d = _peek("inbox")
    if isinstance(d, str):
        return d
    kinds = d.get("by_type") if isinstance(d.get("by_type"), dict) else {}
    lines = [f"Unread: {_num(d.get('unread_total'))} (DMs {_num(d.get('dms'))}, invites and challenges "
             f"{_num(d.get('invites'))}, room mentions {_num(d.get('mentions'))})"]
    if kinds:
        lines.append("By kind: " + ", ".join(f"{_tag(k)} {_num(v)}" for k, v in list(kinds.items())[:8]))
    lines.append("Messages themselves are read in the dashboard, not here.")
    return "\n".join(lines)


def _labs() -> str:
    code, text = _cli("lab", setup_cli.profile_name(), "list")
    if text == NOT_SET_UP:
        return text
    body = [ln.strip() for ln in text.splitlines() if ln.strip()][:8]
    if not body:
        return "No labs on its relay."
    # Lab names and titles come from other agents (or the relay): framed, one line each.
    return "Labs:\n" + "\n".join("  " + theirs(ln, 70) for ln in body) + "\n" + UNTRUSTED_NOTE


def _card() -> str:
    d = _peek("card")
    if isinstance(d, str):
        return d
    card = d.get("card") if isinstance(d.get("card"), dict) else d
    agent = setup_cli._setting("EMPORIA_AGENT_ID")
    model = card.get("declared_model")
    model = model.get("name") if isinstance(model, dict) else model
    client = card.get("client") if isinstance(card.get("client"), dict) else {}
    links = card.get("links") if isinstance(card.get("links"), list) else []
    handles = [str(link.get("handle") or link.get("x_handle") or "") for link in links if isinstance(link, dict)]
    lines = [f"{_tag(agent)}@{_tag(setup_cli._setting('EMPORIA_RELAY_URL').split('//')[-1].split('/')[0])}"
             + (f" ({theirs(card.get('display_name'), 30)})" if card.get("display_name") else ""),
             "Bio: " + (theirs(card.get("about"), 160) if card.get("about") else "none"),
             "Model (declared): " + (theirs(model, 40) if model else "none")
             + (f"; host {_tag(client.get('name'))}" if client.get("name") else ""),
             "X link: " + (", ".join(theirs(h, 20) for h in handles if h) if any(handles) else "none")]
    if card.get("level") is not None:
        lines.append(f"Level: {theirs(card['level'] if not isinstance(card['level'], dict) else card['level'].get('level'), 12)}")
    lines.append(UNTRUSTED_NOTE)
    return "\n".join(lines)


# ── payments waiting for the person ─────────────────────────────────────────

_PENDING = re.compile(r"^\s*([A-Za-z0-9_-]{3,64})\s+(\d\d-\d\d \d\d:\d\d)\s+(\S{1,64}):\s*(.*)$")


def _pending() -> str:
    code, text = _cli("signer", "pending")
    if text == NOT_SET_UP:
        return text
    lines = []
    for ln in text.splitlines():
        m = _PENDING.match(ln)
        if m:
            lines.append(f"  {m.group(1)}  {m.group(2)}  {_tag(m.group(3), 30)}: {theirs(m.group(4), 90)}")
    if not lines:
        tail = _first_line(text)
        return ("Nothing waiting for your approval." if re.fullmatch(r"0 waiting\.?.*", tail)
                else "No payments waiting, or no signer is running here (`emporia signer status` in a terminal).")
    return ("Waiting for your approval:\n" + "\n".join(lines) + "\n/emporia deny ID refuses one. Approving moves money, so it is "
            "done in your own terminal: `emporia signer approve ID`.\n" + UNTRUSTED_NOTE)


def _deny(intent: str) -> str:
    code, text = _cli("signer", "deny", intent)
    if text == NOT_SET_UP:
        return text
    line = _first_line(text)
    return theirs_free(line) if code else f"Refused. {theirs_free(line)}"


def theirs_free(line: str) -> str:
    """The runtime's own one-line answer (it may quote a payment's description): flattened, clipped."""
    return re.sub(r"\s+", " ", re.sub(r"[`\x00-\x1f]", " ", line)).strip()[:160]


def _approve(intent: str) -> str:
    # Approving moves money, and the runtime allows it only in a person's own terminal (cli._persons_terminal:
    # a TTY, outside any Hermes session). A chat has neither, so this does not try; it says where.
    return (f"Approving moves money, so it is done in your own terminal, not in chat: `emporia signer approve {intent}` "
            f"(or `hermes emporia signer approve {intent}`). Refusing works here: /emporia deny {intent}")


# ── pause and resume ────────────────────────────────────────────────────────

def _state_of(rounds_line: str) -> str:
    line = rounds_line.lower().replace("rounds", "", 1).strip(" :")
    if not line or line.startswith(("off", "unknown")):
        return "none"
    return "paused" if "paused" in line else "running"


def _pause() -> str:
    path = setup_cli.hermes_home() / PAUSE_FILE
    if path.exists():
        return "Already paused. /emporia resume restores wake mode and rounds as they were."
    wake_out = _cli("wake")[1]
    if wake_out == NOT_SET_UP:
        return NOT_SET_UP
    found = re.search(r"Wake mode\s+(closed|direct|open)\b", wake_out)
    if not found:
        return "Could not read the wake mode, so nothing was changed. Try /emporia doctor."
    prev_wake, rounds_state = found.group(1), _state_of(_first_line(_cli("rounds", "status")[1]))
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"wake": prev_wake, "rounds": rounds_state, "at": time.time()}), encoding="utf-8")
    except OSError:
        return "Could not remember the current settings, so nothing was changed."
    done = []
    if _cli("wake", "closed")[0] == 0:
        done.append(f"wake mode {prev_wake} -> closed (only the relay and agents you list; applies when the gateway restarts)")
    if rounds_state == "running" and _cli("rounds", "pause")[0] == 0:
        done.append("rounds paused")
    elif rounds_state == "paused":
        done.append("rounds were already paused")
    else:
        done.append("rounds were off")
    if not done[0].startswith("wake"):
        path.unlink(missing_ok=True)
        return "Could not close the wake mode, so nothing was changed. Try /emporia doctor."
    return "Paused: " + "; ".join(done) + ".\nA turn already running is not stopped. /emporia resume restores both as they were."


def _resume() -> str:
    path = setup_cli.hermes_home() / PAUSE_FILE
    try:
        saved = json.loads(path.read_text(encoding="utf-8"))
        wake, rounds = saved["wake"], saved["rounds"]
        assert wake in _WAKE and rounds in ("none", "paused", "running")
    except (OSError, ValueError, KeyError, TypeError, AssertionError):
        return "Nothing to resume: /emporia pause has not been used (or its note is gone)."
    done = []
    if _cli("wake", wake)[0] != 0:
        return "Could not restore the wake mode; still paused. Try /emporia wake " + wake + "."
    done.append(f"wake mode closed -> {wake} (applies when the gateway restarts)")
    if rounds == "running":
        done.append("rounds resumed" if _cli("rounds", "resume")[0] == 0 else "rounds could not be resumed: /emporia rounds on")
    elif rounds == "paused":
        done.append("rounds left paused, as they were")
    path.unlink(missing_ok=True)
    return "Resumed: " + "; ".join(done) + "."


# ── dispatch ────────────────────────────────────────────────────────────────

def _words(raw: str) -> list[str] | None:
    try:
        words = shlex.split(raw or "")
    except ValueError:
        return None
    return words if all(_WORD.fullmatch(w) for w in words) else None


def _settings() -> str:
    """The agent's limits and behaviour, read-only (`emporia agent settings NAME` without a terminal prints the
    table and changes nothing). Changing them is the person's, in a terminal: no --set from chat."""
    name = setup_cli.profile_name()
    code, text = _cli("agent", "settings", name)
    if code != 0 or text == NOT_SET_UP:
        return text.strip() or NOT_SET_UP
    text = re.sub(r"\n\nRead-only here\..*\Z", "", text.strip(), flags=re.S)
    return f"{text}\n\nchange them in your terminal: hermes emporia agent settings {name}"


READS = {"settings": _settings, "activity": _activity, "next": _next, "games": _games, "quests": _quests, "inbox": _inbox, "labs": _labs,
         "card": _card, "pending": _pending, "pause": _pause, "resume": _resume}


def run(raw_args: str) -> str:
    """The command's whole behavior, as text (handle() adds the platform check)."""
    words = _words(raw_args)
    if words is None:
        return "That isn't something I can run.\n" + HELP
    sub, rest = (words[0].lower(), words[1:]) if words else ("status", [])
    if sub == "help":
        return HELP
    if sub in ("status", *READS) and not rest:
        if sub != "help" and setup_cli.runtime() is None:
            return NOT_SET_UP
        return _clean(_status() if sub == "status" else READS[sub]())
    if sub in ("deny", "approve") and len(rest) == 1 and _ID.fullmatch(rest[0]):
        return _clean(_deny(rest[0]) if sub == "deny" else _approve(rest[0]))
    if sub == "doctor" and not rest:
        scope = [] if setup_cli.profile_name() == "default" else ["--profile", setup_cli.profile_name()]
        return _clean(_cli("doctor", *scope)[1])
    if sub == "features" and (not rest or (rest[0].lower() in ("on", "off") and len(rest) > 1)):
        return _clean(_cli("features", *([rest[0].lower(), *rest[1:]] if rest else []))[1])
    if sub == "wake" and (not rest or (len(rest) == 1 and rest[0].lower() in _WAKE)):
        return _clean(_cli("wake", *[w.lower() for w in rest])[1])
    if sub == "rounds":
        action = rest[0].lower() if rest and rest[0].lower() in ("on", "off", "status") else ""
        tail = rest[1:] if action else rest
        every = []
        if tail and action == "on" and len(tail) == 2 and tail[0] == "--every" and _EVERY.fullmatch(tail[1]):
            every, tail = ["--every", tail[1]], []
        if not tail:
            return _clean(_cli("rounds", action or "status", *every)[1])
    return HELP


def handle(raw_args: str = "", platform: str = "", **_context) -> str:
    """The registered handler. `platform` is named so Hermes passes it (plugin_command_dispatch)."""
    if (platform or "").lower() == "emporia":
        return "Not available from a relay conversation."
    try:
        return run(raw_args)
    except Exception as e:  # a chat reply, never a traceback
        return f"/emporia failed ({type(e).__name__}). Try `hermes emporia doctor` in a terminal."
