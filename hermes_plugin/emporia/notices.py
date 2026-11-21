"""Emporia notices: what the person may want to know, in fixed words, and the optional daily message.

Two parts, both read-only about Emporia and about Hermes:

* `collect()`: the notices that hold now. The runtime behind the plugin,
  game moves the daily turn cap held back today, a reply waiting over 30 minutes with no round scheduled, and the
  relay's own update note. The dashboard tab shows them; `hermes emporia notices status` prints them.
* the opt-in job (`hermes emporia notices on`): one Hermes cron job per profile, `no_agent`, whose script runs
  `hermes emporia notices check` and prints at most one message a day, or nothing. Hermes delivers that to the
  person's own channel (`--deliver <platform>`, the platform's home channel); Emporia never holds chat
  credentials (catalog rule 11) and never sends anything itself.

Every text is fixed here. Nothing a relay or another agent wrote is ever repeated: the relay's update note is
only noticed (a frame of its type is in the inbox) and said again in our own words. No text carries a link.
Nothing here fetches or installs code (catalog rule 3): it looks at no Hermes catalog file, and
the update commands are only printed.

Stdlib only, no import of Emporia: it loads standalone by path (the dashboard API and the cron script do).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
JOB_NAME = "Emporia — notices"
SCRIPT = "emporia_notices.py"
#: Once a day at 09:00 (the schedule is the person's local time, as Hermes reads it).
SCHEDULE = "0 9 * * *"
STATE_FILE = "notices_state.json"
RELAY_NOTE_FRAME = "relay_update_note"
WAITING_REPLY_S = 1800
ROUNDS_JOB = "Emporia — rounds"
_BYPASS = ("HERMES_YOLO_MODE", "HERMES_YOLO", "HERMES_ACCEPT_HOOKS", "HERMES_ACP_AUTO_APPROVE", "PYTHONHOME", "PYTHONPATH")
_TIMEOUT_S = 90
_ID_LINE = re.compile(r"^\s*([0-9a-f]{8,32})\s+\[([^\]]+)\]\s*$")
_NAME_LINE = re.compile(r"^\s+Name:\s*(.*?)\s*$")
_PLATFORM = re.compile(r"[a-z][a-z0-9_]{1,31}")

#: The script written into the profile's `scripts/` folder (a bare filename is how Hermes resolves a no-agent job's
#: script). It only asks Hermes's own CLI for the check, as the rounds gate asks `peek next`; trouble means silence.
SCRIPT_TEXT = '''#!/usr/bin/env python3
"""Emporia notices. Written by `hermes emporia notices on`, removed by `hermes emporia notices off`.
Prints at most one short message a day (Hermes delivers it as is) and nothing when nothing is new."""
import os
import shutil
import subprocess
from pathlib import Path

HERMES = __HERMES__
PROFILE = Path(__file__).resolve().parent.parent


def main() -> None:
    hermes = HERMES if HERMES and Path(HERMES).exists() else shutil.which("hermes")
    if not hermes:
        return
    try:
        out = subprocess.run([hermes, "emporia", "notices", "check"], capture_output=True, text=True, timeout=240,
                             stdin=subprocess.DEVNULL, env={**os.environ, "HERMES_HOME": str(PROFILE)})
    except Exception:
        return
    if out.returncode == 0 and out.stdout.strip():
        print(out.stdout.strip())


main()
'''


def _home() -> Path:
    try:
        from hermes_constants import get_hermes_home
        return Path(get_hermes_home())
    except Exception:  # noqa: BLE001 — any failure falls back to the default Hermes home
        return Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")


def profile_name(home: Path | None = None) -> str:
    home = home or _home()
    return home.name if home.parent.name == "profiles" else "default"


def _scope(home: Path | None = None) -> str:
    name = profile_name(home)
    return "" if name == "default" else f"-p {name} "


def plugin_update_command(home: Path | None = None) -> str:
    """The one command that moves a catalog install of this plugin (Hermes 0.23.2 and later: `hermes plugins update`)."""
    return f"hermes {_scope(home)}plugins update emporia"


def runtime_update_command(home: Path | None = None) -> str:
    return f"hermes {_scope(home)}emporia update"


# ── what holds now ─────────────────────────────────────────────────────────────────────────────────────────────

def _read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def plugin_version() -> str:
    try:
        for line in (HERE / "plugin.yaml").read_text(encoding="utf-8").splitlines():
            if line.startswith("version:"):
                return line.split(":", 1)[1].strip().strip("'\"")
    except OSError:
        pass
    return ""


def plugin_pin() -> str:
    return str(_read_json(HERE / "emporia_source.json").get("sha") or "")


def _money_guard():
    try:
        from . import money_guard
        return money_guard
    except ImportError:                                     # loaded by path (the cron check, the dashboard API)
        import importlib.util
        spec = importlib.util.spec_from_file_location("emporia_money_guard", HERE / "money_guard.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod


def _turn_cap():
    try:
        from . import turn_cap
        return turn_cap
    except ImportError:
        import importlib.util
        spec = importlib.util.spec_from_file_location("emporia_turn_cap", HERE / "turn_cap.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod


def hermes_bin() -> str:
    return os.environ.get("EMPORIA_HERMES_BIN", "").strip() or shutil.which("hermes") or ""


def run_hermes(argv: list[str], home: Path | None = None) -> str:
    """Hermes's public CLI for this profile, stdin closed; its stdout. Raises OSError on any trouble."""
    hermes = hermes_bin()
    if not hermes:
        raise OSError("no hermes on PATH")
    env = {k: v for k, v in os.environ.items() if k not in _BYPASS}
    if home is not None:
        env["HERMES_HOME"] = str(home)
    try:
        out = subprocess.run([hermes, *argv], capture_output=True, text=True, timeout=_TIMEOUT_S, check=False,
                             stdin=subprocess.DEVNULL, env=env)
    except subprocess.SubprocessError as e:
        raise OSError(type(e).__name__) from e
    if out.returncode != 0:
        raise OSError(((out.stderr or out.stdout).strip().splitlines() or ["failed"])[-1][-200:])
    return out.stdout


def _peek(what: str, home: Path) -> dict | None:
    """The runtime's read-only `peek WHAT` (it moves nothing), as a dict; None when it cannot be read."""
    try:
        line = (run_hermes(["emporia", "peek", what], home).strip().splitlines() or [""])[-1]
        d = json.loads(line)
    except (OSError, ValueError):
        return None
    return d if isinstance(d, dict) and d.get("success") is True else None


def rounds_on(home: Path) -> bool | None:
    """Whether this profile has a rounds job that is not paused; None when Hermes cannot say."""
    try:
        listing = run_hermes(["cron", "list", "--all"], home)
    except OSError:
        return None
    cur = None
    for line in listing.splitlines():
        m = _ID_LINE.match(line)
        if m:
            cur = m.group(2).strip().lower()
            continue
        n = _NAME_LINE.match(line)
        if n and cur is not None and n.group(1) == ROUNDS_JOB:
            return not cur.startswith("paus")
    return False


def collect(home: Path | None = None, *, probe: bool = True, waited_s: int | None = None,
            has_rounds: bool | None = None, relay_note: bool | None = None) -> list[dict]:
    """The notices that hold now: [{"id", "key", "text"}], each text fixed. `key` says which occurrence it is (a
    version, a day), so a message about it is sent once. `probe=False` skips the two look-ups that start a process
    (`waited_s`, `has_rounds` and `relay_note` may then be passed in by a caller that already has them)."""
    home = home or _home()
    out = []
    now = datetime.now()
    day = now.strftime("%Y-%m-%d")
    name = profile_name(home)
    try:
        stale = _money_guard().runtime_is_stale()
        moved = _money_guard().plugin_moved_to() if stale else ""
    except Exception:  # noqa: BLE001
        stale, moved = False, ""
    if stale:
        out.append({"id": "runtime", "key": plugin_pin()[:12], "text": (
            f"Emporia's plugin moved to {moved or 'a newer release'}, its runtime did not. Run "
            f"`{runtime_update_command(home)}`, then restart the gateway (`hermes gateway restart`).")})
    try:
        waited = _turn_cap().moves_waited(home)
    except Exception:  # noqa: BLE001
        waited = 0
    if waited:
        out.append({"id": "moves", "key": day, "text": (
            f"{waited} game move{'s' if waited != 1 else ''} waited today because the daily turn cap was reached. "
            f"Raise it: `emporia agent settings {name} --set daily_turns=...`.")})
    if probe:
        has_rounds = rounds_on(home) if has_rounds is None else has_rounds
        if has_rounds is False and waited_s is None:
            d = _peek("waiting", home)
            waited_s = d.get("wait_s") if d else None
        if relay_note is None:
            d = _peek("inbox", home)
            relay_note = bool(d and (d.get("by_type") or {}).get(RELAY_NOTE_FRAME))
    if has_rounds is False and isinstance(waited_s, int) and waited_s > WAITING_REPLY_S:
        out.append({"id": "replies", "key": day, "text": (
            f"A reply or message has waited over {WAITING_REPLY_S // 60} minutes in the inbox and no round is "
            f"scheduled to read it. Turn rounds on (`emporia rounds on --agent {name}`) or widen the wake "
            f"(`emporia wake open --agent {name}`).")})
    if relay_note:
        year, week, _ = now.isocalendar()
        out.append({"id": "relay", "key": f"{year}-{week:02d}", "text": (
            f"Your relay says a newer Emporia is out. Update the plugin, then the runtime: "
            f"`{plugin_update_command(home)}`, then `{runtime_update_command(home)}`.")})
    return out


# ── the state and the daily message ───────────────────────────────────────────────────────────────────────────

def state_path(home: Path | None = None) -> Path:
    return (home or _home()) / "plugin-data" / "emporia" / STATE_FILE


def read_state(home: Path | None = None) -> dict:
    s = _read_json(state_path(home))
    s["sent"] = s.get("sent") if isinstance(s.get("sent"), dict) else {}
    return s


def write_state(state: dict, home: Path | None = None) -> None:
    path = state_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(json.dumps(state, indent=1, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)


def daily(home: Path | None = None, *, now: datetime | None = None, notices: list[dict] | None = None) -> str:
    """The one message due now, or "". At most one a day (the state file keeps the day), and only notices whose
    occurrence was not told before; what is told is recorded here, so a second run the same day prints nothing."""
    home = home or _home()
    now = now or datetime.now()
    day = now.strftime("%Y-%m-%d")
    state = read_state(home)
    if state.get("last_sent_day") == day:
        return ""
    found = collect(home) if notices is None else notices
    fresh = [n for n in found if state["sent"].get(n["id"]) != n["key"]]
    if not fresh:
        return ""
    for n in fresh:
        state["sent"][n["id"]] = n["key"]
    state["last_sent_day"] = day
    write_state(state, home)
    return "\n".join(["Emporia" + ("" if profile_name(home) == "default" else f" ({profile_name(home)})") + ":",
                      *(f"- {n['text']}" for n in fresh)])


def choice(home: Path | None = None) -> str:
    """"on", "off", or "" when the person was never asked (setup keeps a recorded choice)."""
    v = read_state(home).get("choice")
    return v if v in ("on", "off") else ""


def set_choice(value: str, home: Path | None = None) -> None:
    state = read_state(home)
    state["choice"] = value
    write_state(state, home)


# ── the job ────────────────────────────────────────────────────────────────────────────────────────────────────

def channels() -> list[str]:
    """Platforms Hermes can deliver to without a chat id: those with a home channel set in this profile's gateway
    config (Hermes's own `gateway.config`; no credential is read here). Sorted; [] when there is none or Hermes
    cannot say."""
    try:
        from gateway.config import load_gateway_config
        cfg = load_gateway_config()
        names = [str(getattr(p, "value", p)) for p, c in cfg.platforms.items()
                 if getattr(c, "home_channel", None) and getattr(c, "enabled", True)]
    except Exception:  # noqa: BLE001
        return []
    return sorted(n for n in names if _PLATFORM.fullmatch(n) and n not in ("local", "emporia"))


def script_path(home: Path | None = None) -> Path:
    return (home or _home()) / "scripts" / SCRIPT


def _write_script(home: Path) -> Path:
    path = script_path(home)
    text = SCRIPT_TEXT.replace("__HERMES__", repr(hermes_bin()))
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists() or path.read_text(encoding="utf-8") != text:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o700)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.chmod(path, 0o700)
    return path


def job_id(home: Path) -> str:
    """The id of this profile's notices job (found by its fixed name, never another), "" when there is none."""
    cur, found = None, ""
    for line in run_hermes(["cron", "list", "--all"], home).splitlines():
        m = _ID_LINE.match(line)
        if m:
            cur = m.group(1)
            continue
        n = _NAME_LINE.match(line)
        if n and cur and n.group(1) == JOB_NAME and not found:
            found = cur
    return found


def turn_on(home: Path | None = None, deliver: str = "") -> str:
    """Create (or bring to this target) the profile's notices job; records the choice. Raises OSError/ValueError
    with a text safe to show."""
    home = home or _home()
    found = channels()
    deliver = (deliver or (found[0] if found else "")).strip().lower()
    if not deliver:
        raise ValueError("no delivery channel: set a home channel in Hermes first (`/sethome` in the chat you want "
                         "them in), or name one: `hermes emporia notices on --deliver telegram`")
    if not _PLATFORM.fullmatch(deliver) or deliver == "local":
        raise ValueError(f"{deliver!r} is not a delivery platform name (for example telegram, discord, signal)")
    _write_script(home)
    base = ["cron"]
    existing = job_id(home)
    flags = ["--deliver", deliver, "--failure-deliver", "local", "--script", SCRIPT, "--no-wrap-response",
             "--catch-up", "off"]
    if existing:
        run_hermes([*base, "edit", existing, "--schedule", SCHEDULE, "--name", JOB_NAME, *flags], home)
        done = f"notices updated: one message a day at most, to {deliver} (job {existing})"
    else:
        out = run_hermes([*base, "create", SCHEDULE, "--name", JOB_NAME, "--no-agent", *flags], home)
        m = re.search(r"Created job:\s*([0-9a-f]+)", out)
        done = f"notices on: one message a day at most, to {deliver} (job {m.group(1) if m else '?'})"
    set_choice("on", home)
    return done


def turn_off(home: Path | None = None) -> str:
    home = home or _home()
    try:
        existing = job_id(home)
        if existing:
            run_hermes(["cron", "remove", existing], home)
    except OSError as e:
        raise OSError(f"hermes did not remove the job ({e}); remove it with `hermes cron list` and `hermes cron remove`") from e
    try:
        script_path(home).unlink()
    except FileNotFoundError:
        pass
    set_choice("off", home)
    return "notices off: the job is removed" if existing else "notices were not on"


def status_line(home: Path | None = None) -> str:
    home = home or _home()
    try:
        existing = job_id(home)
    except OSError as e:
        return f"unknown ({e})"
    if not existing:
        return "off" + ("" if choice(home) == "off" else " (turn on: `hermes emporia notices on`)")
    return f"on (job {existing}): one message a day at most, silent when nothing is new"


# ── `hermes emporia notices …` ─────────────────────────────────────────────────────────────────────────────────

def main(action: str, deliver: str = "", home: Path | None = None) -> int:
    home = home or _home()
    action = (action or "status").lower()
    if action == "check":
        message = daily(home)
        if message:
            print(message)
        return 0
    if action == "channel":
        print(json.dumps({"channels": channels(), "choice": choice(home)}))
        return 0
    if action in ("on", "off"):
        try:
            print(turn_on(home, deliver) if action == "on" else turn_off(home))
        except ValueError as e:
            print(f"  ✗ {e}")
            return 2
        except OSError as e:
            print(f"  ✗ {e}")
            return 1
        return 0
    if action == "status":
        print(f"Notices job : {status_line(home)}")
        found = collect(home)
        if not found:
            print("Notices now : none")
        for n in found:
            print(f"Notice      : {n['text']}")
        return 0
    print(f"hermes emporia notices: no action {action!r} (on, off, status)")
    return 2
