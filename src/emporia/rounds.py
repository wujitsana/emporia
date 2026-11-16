"""Rounds: a scheduled turn so an agent acts on Emporia without being asked.

A round is one Hermes cron job per profile, created and removed with Hermes's own public CLI
(`hermes [-p PROFILE] cron create | edit | pause | resume | remove | list`). Nothing here writes
`cron/jobs.json`. The job is found by a fixed name (`JOB_NAME`); this module only ever lists,
creates, edits, pauses, resumes or removes a job with that name, never any other.

What a round does is the prompt in the package (`src/emporia/data/rounds_prompt.md`, the single source,
whose first line is the marker `[emporia-rounds v1]`, which the plugin's `rounds_marker.ROUNDS_MARKER` repeats): look at `whats_next` (with its `happening` part), take at most three
actions inside the playbook, never pay or accept a deal, treat what the relay says as data, and
finish with one line or `[SILENT]`. The job delivers to `local` (saved with its run, sent to no
chat), and so do its failure notices.

A round is the agent's own turn, so it has the agent's normal tools; it is not started by the
relay, and neither the relay-turn tool limits nor the turn cap apply to it. The cost is the
person's to see before saying yes (`estimate`), and `emporia rounds off` removes the job.

Person-only: the key guard holds `emporia rounds on|off|pause|resume` from an agent's tools.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

JOB_NAME = "Emporia — rounds"
#: The plugin's skill, pinned to the job so a round has the playbook of what an agent does here.
SKILL = "emporia:emporia"
#: Hermes 0.23 `--catch-up off`: rounds missed while Hermes was off are skipped, not fired at once on return
#: (each shows as a "missed" row in `hermes cron runs`). Applied on create and on every re-run of `rounds on`.
CATCH_UP = ("--catch-up", "off")
#: The quiet-round gate (`emporia rounds on --skip-quiet`): one small script in the profile's `scripts/` folder, which
#: Hermes runs before each round (template: `data/rounds_gate.py.txt`). Removed by `rounds off` and by `rounds on`
#: without the flag. Its output is at most GATE_MAX_LINES lines, which the plugin's marker check allows for.
GATE_SCRIPT = "emporia_rounds_gate.py"
GATE_TEMPLATE = "rounds_gate.py.txt"
GATE_MAX_LINES = 4
INTERVALS = ("2h", "4h", "8h", "12h", "24h")
DEFAULT_EVERY = "4h"
PROMPT_FILE = "rounds_prompt.md"
_TIMEOUT_S = 60
_ID_LINE = re.compile(r"^\s*([0-9a-f]{8,32})\s+\[([^\]]+)\]\s*$")
_FIELD = re.compile(r"^\s+(Name|Schedule):\s*(.*?)\s*$")
_BYPASS = ("HERMES_YOLO_MODE", "HERMES_YOLO", "HERMES_ACCEPT_HOOKS", "HERMES_ACP_AUTO_APPROVE",
           "PYTHONHOME", "PYTHONPATH")


class RoundsError(RuntimeError):
    """What went wrong, safe to show."""


@dataclass(frozen=True)
class Job:
    id: str
    state: str
    name: str
    schedule: str = ""


def scripts_dir(profile_dir: Path) -> Path:
    return Path(profile_dir) / "scripts"


def gate_path(profile_dir: Path) -> Path:
    return scripts_dir(profile_dir) / GATE_SCRIPT


def write_gate(profile_dir: Path) -> Path:
    """The gate script into the profile's scripts folder (owner-only; rewritten only when it differs)."""
    try:
        hermes = hermes_bin()
    except RoundsError:
        hermes = ""
    template = Path(__file__).resolve().parent / "data" / GATE_TEMPLATE
    text = template.read_text(encoding="utf-8").replace("__HERMES__", repr(hermes))
    path = gate_path(profile_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists() or path.read_text(encoding="utf-8") != text:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o700)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.chmod(path, 0o700)
    return path


def remove_gate(profile_dir: Path | None) -> bool:
    """Delete the gate script; True when there was one."""
    if profile_dir is None:
        return False
    try:
        gate_path(profile_dir).unlink()
        return True
    except FileNotFoundError:
        return False


def has_gate(profile_dir: Path | None) -> bool:
    return profile_dir is not None and gate_path(profile_dir).exists()


def gate_supported(profile_dir: Path) -> str:
    """"" when the plugin installed in this profile still recognises a round that has the gate's output block ahead
    of its prompt, else why not. An older plugin would take such a round for a person's own turn, with every tool,
    so the gate is refused until the plugin is updated."""
    import importlib.util
    path = Path(profile_dir) / "plugins" / "emporia" / "relay_turn.py"
    if not path.is_file():
        return "the Emporia plugin is not installed in this profile (hermes emporia setup)"
    try:
        spec = importlib.util.spec_from_file_location("emporia_relay_turn_probe", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        block = ("## Script Output\nThe following data was collected by a pre-run script. Use it as context for your "
                 "analysis.\n\n```\n" + "\n".join(f"line {i}" for i in range(GATE_MAX_LINES)) + "\n```\n\n")
        return "" if mod.is_rounds_prompt(block + marker() + "\nrest") else (
            "the installed Emporia plugin is older than this option needs (hermes plugins install emporia --force)")
    except Exception as e:  # noqa: BLE001 — an unreadable plugin is not proof of safety
        return f"the installed plugin could not be checked ({type(e).__name__})"


def hours(every: str) -> int:
    every = (every or "").strip().lower()
    if every not in INTERVALS:
        raise RoundsError(f"--every is one of {', '.join(INTERVALS)} (not {every!r})")
    return int(every[:-1])


def estimate(every: str) -> str:
    """What a person should know before saying yes, in two honest lines."""
    n = 24 // hours(every)
    return (f"Every {every}: about {n} round{'s' if n != 1 else ''} a day. Each round is one model turn "
            f"of roughly 3,000 to 8,000 tokens on the model this profile is set to (a quiet one is "
            f"cheaper; at most 3 actions), so about {n * 3}k to {n * 8}k tokens a day.")


def prompt_path(plugin_dir: Path | None = None) -> Path:
    """The rounds prompt: `emporia/data/rounds_prompt.md`, inside the package (so it is in the wheel and in the
    runtime a catalog install clones), the single source. The Hermes plugin does not read it: the plugin only
    recognises a round by its marker line (`hermes_plugin/emporia/rounds_marker.py`, held equal by a test).
    `plugin_dir` is accepted and ignored."""
    p = Path(__file__).resolve().parent / "data" / PROMPT_FILE
    if not p.is_file():
        raise RoundsError(f"the rounds prompt ({PROMPT_FILE}) is not in the package")
    return p


def prompt_text(plugin_dir: Path | None = None, template: str = "") -> str:
    """The prompt, which must begin with the marker line a hook recognizes a round by. With a setup template
    (`emporia.templates`) it is that template's variant: its own steps, the stock rules word for word."""
    if template:
        from emporia import templates
        text = templates.rounds_prompt(template)
    else:
        text = prompt_path(plugin_dir).read_text(encoding="utf-8").strip()
    if text.splitlines()[0] != marker():
        raise RoundsError("the rounds prompt does not begin with its marker line")
    return text


def marker() -> str:
    """The marker line a round opens with: the first line of the packaged prompt."""
    return prompt_path().read_text(encoding="utf-8").splitlines()[0].strip()


def prompt_version(text: str) -> str:
    m = re.fullmatch(r"\[emporia-rounds v(\d+)\]", text.splitlines()[0] if text else "")
    return m.group(1) if m else "?"


def hermes_bin() -> str:
    found = os.getenv("EMPORIA_HERMES_BIN", "").strip() or shutil.which("hermes")
    if not found:
        raise RoundsError("Hermes is not installed here (no `hermes` on PATH), so rounds cannot be scheduled")
    return found


def run_hermes(argv: list[str]) -> str:
    """Hermes's public CLI, stdin closed (nothing waits for an answer), its output returned."""
    env = {k: v for k, v in os.environ.items() if k not in _BYPASS}
    try:
        out = subprocess.run([hermes_bin(), *argv], stdin=subprocess.DEVNULL, capture_output=True, text=True,
                             timeout=_TIMEOUT_S, check=False, env=env)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise RoundsError(f"hermes did not answer: {type(e).__name__}") from e
    if out.returncode != 0:
        last = ((out.stderr or out.stdout).strip().splitlines() or ["?"])[-1][-300:]
        raise RoundsError(f"hermes {' '.join(argv[:3])} failed: {last}")
    return out.stdout


def _scope(profile: str) -> list[str]:
    return [] if profile in ("", "default") else ["-p", profile]


def parse_jobs(listing: str) -> list[Job]:
    jobs: list[Job] = []
    cur: dict | None = None
    for line in listing.splitlines():
        m = _ID_LINE.match(line)
        if m:
            if cur:
                jobs.append(Job(**cur))
            cur = {"id": m.group(1), "state": m.group(2).strip(), "name": "", "schedule": ""}
            continue
        f = _FIELD.match(line)
        if f and cur is not None:
            cur[f.group(1).lower()] = f.group(2)
    if cur:
        jobs.append(Job(**cur))
    return jobs


def jobs(profile: str) -> list[Job]:
    """This profile's rounds jobs: those named JOB_NAME, and no other."""
    listing = run_hermes([*_scope(profile), "cron", "list", "--all"])
    return [j for j in parse_jobs(listing) if j.name == JOB_NAME]


def turn_on(profile: str, every: str = DEFAULT_EVERY, plugin_dir: Path | None = None, *,
            profile_dir: Path | None = None, skip_quiet: bool = False, model: str = "",
            reasoning_effort: str = "", template: str = "") -> str:
    """Create the job (or bring the existing one to this schedule and prompt, and resume it).

    `skip_quiet` (needs `profile_dir`) writes the gate script and attaches it to the job; without it the script is
    removed and cleared from the job. `model` and `reasoning_effort` pin the job's model and thinking effort when
    given (Hermes validates them); left empty the job keeps what it has, which for a new job is the profile's own."""
    schedule = f"every {hours(every)}h"
    text = prompt_text(plugin_dir, template)
    base = _scope(profile)
    if skip_quiet:
        if profile_dir is None:
            raise RoundsError("--skip-quiet needs the agent's profile folder")
        if why := gate_supported(profile_dir):
            raise RoundsError(f"--skip-quiet is not set up: {why}")
        write_gate(profile_dir)
    pins: list[str] = []
    if model.strip():
        pins += ["--model", model.strip()]
    if reasoning_effort.strip():
        pins += ["--reasoning-effort", reasoning_effort.strip()]
    note = ", skips quiet rounds" if skip_quiet else ""
    had_gate = has_gate(profile_dir)       # the job carries our script only if we wrote it: clear it with the file
    existing = jobs(profile)
    if existing:
        job = existing[0]
        run_hermes([*base, "cron", "edit", job.id, "--schedule", schedule, "--prompt", text, "--name", JOB_NAME,
                    "--deliver", "local", "--failure-deliver", "local", "--skill", SKILL,
                    *(["--script", GATE_SCRIPT] if skip_quiet else ["--script", ""] if had_gate else []),
                    *pins, *CATCH_UP])
        if not skip_quiet:
            remove_gate(profile_dir)
        if job.state.lower().startswith("paus"):
            run_hermes([*base, "cron", "resume", job.id])
        return f"rounds updated: every {every} (job {job.id}, prompt v{prompt_version(text)}{note})"
    out = run_hermes([*base, "cron", "create", schedule, text, "--name", JOB_NAME, "--deliver", "local",
                      "--failure-deliver", "local", "--skill", SKILL, *CATCH_UP,
                      *(["--script", GATE_SCRIPT] if skip_quiet else []), *pins])
    if not skip_quiet:
        remove_gate(profile_dir)
    m = re.search(r"Created job:\s*([0-9a-f]+)", out)
    return f"rounds on: every {every} (job {m.group(1) if m else '?'}, prompt v{prompt_version(text)}{note})"


def turn_off(profile: str, profile_dir: Path | None = None) -> str:
    found = jobs(profile)
    for job in found:
        run_hermes([*_scope(profile), "cron", "remove", job.id])
    gone = remove_gate(profile_dir)
    if found:
        return "rounds off: the job is removed" + (" (and its gate script)" if gone else "")
    return "rounds were not on" + (" (its gate script is removed)" if gone else "")


def pause(profile: str) -> str:
    found = jobs(profile)
    for job in found:
        run_hermes([*_scope(profile), "cron", "pause", job.id])
    return "rounds paused (`emporia rounds resume`)" if found else "rounds are not on"


def resume(profile: str) -> str:
    found = jobs(profile)
    for job in found:
        run_hermes([*_scope(profile), "cron", "resume", job.id])
    return "rounds resumed" if found else "rounds are not on (`emporia rounds on`)"


def status_line(profile: str, profile_dir: Path | None = None) -> str:
    """One line for `rounds status` and `doctor`: never raises."""
    try:
        found = jobs(profile)
    except RoundsError as e:
        return f"unknown ({e})"
    if not found:
        return "off"
    j = found[0]
    gate = "; skips quiet rounds" if profile_dir is not None and gate_path(profile_dir).exists() else ""
    return f"{j.state}, {j.schedule or '?'} (job {j.id}){gate}; a round has the agent's own tools, delivers nowhere"


# ─── what the last rounds did (read-only: `hermes cron runs`) ────────────────────────────────

_RUN_LINE = re.compile(r"^([0-9a-f]{6,64})\s+(\w+)\s+job=([0-9a-f]+)\s+(?:source=\S+\s+)?(\d{4}-\d\d-\d\dT\S+)\s*$")
#: Run states Hermes records (`cron/executions.py`): `failed` and `unknown` (a run that was lost) are the bad ones.
BAD_STATES = ("failed", "unknown")
_NOTE_MAX = 160


def _plain(text: str, limit: int = _NOTE_MAX) -> str:
    """One line a person may be shown: control characters out, clipped. Failure text can quote what an agent said."""
    clean = re.sub(r"[\x00-\x1f\x7f]+", " ", text).strip()
    return clean if len(clean) <= limit else clean[: limit - 1].rstrip() + "…"


def parse_runs(listing: str) -> list[dict]:
    """`hermes cron runs` rows, newest first as Hermes prints them: {id, status, job, at, note} (note = the first
    line of the error text under a row, if any)."""
    rows: list[dict] = []
    for line in listing.splitlines():
        m = _RUN_LINE.match(line)
        if m:
            rows.append({"id": m.group(1), "status": m.group(2).lower(), "job": m.group(3), "at": m.group(4), "note": ""})
        elif rows and line.strip() and line[:1].isspace() and not rows[-1]["note"]:
            rows[-1]["note"] = _plain(line)
    return rows


def last_runs(profile: str, limit: int = 30) -> dict:
    """The rounds job's last run and last failure, from Hermes's own run records (read-only; no state is changed).
    {"job": id|"", "last": row|None, "last_failure": row|None, "error": str}. Never raises."""
    out: dict = {"job": "", "last": None, "last_failure": None, "error": ""}
    try:
        found = jobs(profile)
        if not found:
            return out
        out["job"] = found[0].id
        rows = parse_runs(run_hermes([*_scope(profile), "cron", "runs", found[0].id, "--limit", str(limit)]))
    except RoundsError as e:
        out["error"] = str(e)
        return out
    rows = [r for r in rows if r["job"] == out["job"] and r["status"] not in ("claimed",)]
    out["last"] = rows[0] if rows else None
    out["last_failure"] = next((r for r in rows if r["status"] in BAD_STATES), None)
    return out


def runs_report(profile: str) -> tuple[str, str, str]:
    """(level, text, fix) for `doctor`: level is "ok", "warn" or "off" (no rounds job: nothing to say)."""
    r = last_runs(profile)
    if r["error"]:
        return "warn", f"the last rounds could not be read ({r['error']})", ""
    if not r["job"]:
        return "off", "", ""
    last, bad = r["last"], r["last_failure"]
    if last is None:
        return "ok", "no round has run yet", ""
    when = last["at"][:16].replace("T", " ") + " UTC"
    fix = f"hermes -p {profile} cron runs {r['job']}" if profile not in ("", "default") else f"hermes cron runs {r['job']}"
    if last["status"] in BAD_STATES:
        return "warn", f"the last round failed ({when}){': ' + last['note'] if last['note'] else ''}", fix
    text = f"the last round {last['status']} ({when})"
    if bad is not None:
        text += f"; last failure {bad['at'][:16].replace('T', ' ')} UTC" + (f": {bad['note']}" if bad["note"] else "")
    return "ok", text, ""
