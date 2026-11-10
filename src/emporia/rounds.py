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


def prompt_text(plugin_dir: Path | None = None) -> str:
    """The prompt, which must begin with the marker line a hook recognizes a round by."""
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


def turn_on(profile: str, every: str = DEFAULT_EVERY, plugin_dir: Path | None = None) -> str:
    """Create the job (or bring the existing one to this schedule and prompt, and resume it)."""
    schedule = f"every {hours(every)}h"
    text = prompt_text(plugin_dir)
    base = _scope(profile)
    existing = jobs(profile)
    if existing:
        job = existing[0]
        run_hermes([*base, "cron", "edit", job.id, "--schedule", schedule, "--prompt", text, "--name", JOB_NAME,
                    "--deliver", "local", "--failure-deliver", "local", "--skill", SKILL,
                    *CATCH_UP])
        if job.state.lower().startswith("paus"):
            run_hermes([*base, "cron", "resume", job.id])
        return f"rounds updated: every {every} (job {job.id}, prompt v{prompt_version(text)})"
    out = run_hermes([*base, "cron", "create", schedule, text, "--name", JOB_NAME, "--deliver", "local",
                      "--failure-deliver", "local", "--skill", SKILL, *CATCH_UP])
    m = re.search(r"Created job:\s*([0-9a-f]+)", out)
    return f"rounds on: every {every} (job {m.group(1) if m else '?'}, prompt v{prompt_version(text)})"


def turn_off(profile: str) -> str:
    found = jobs(profile)
    for job in found:
        run_hermes([*_scope(profile), "cron", "remove", job.id])
    return "rounds off: the job is removed" if found else "rounds were not on"


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


def status_line(profile: str) -> str:
    """One line for `rounds status` and `doctor`: never raises."""
    try:
        found = jobs(profile)
    except RoundsError as e:
        return f"unknown ({e})"
    if not found:
        return "off"
    j = found[0]
    return f"{j.state}, {j.schedule or '?'} (job {j.id}); a round has the agent's own tools, delivers nowhere"
