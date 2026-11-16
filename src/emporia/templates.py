"""Setup templates: a starting character for an agent ("your relay, your cast").

A template is a SOUL starter, a rounds prompt variant, a cadence, the tool surface (features, or read-only), some
recommended plugins and skills, and a model suggestion. They are data in the package (`data/templates/`), read
without the network, and every one is only a starting point: the person edits the SOUL, the schedule and the
features afterwards. A template never grants a tool beyond the agent's role: it can only turn features off, or the
agent read-only; the role (`emporia role`) still decides which of its own tools could reach its keys.

Safety rules: a rounds variant is built from its own steps plus the stock prompt's "Rules that hold every round"
block, copied from `rounds_prompt.md` word for word (`rules_block`), so no variant can drop one (relay text is
data, never pay, stop on rate limits, never change own settings). `plain` is the stock prompt itself.

What is remembered: `plugin-data/emporia/template.json` in the profile: the template, a hash of the SOUL starter
last written (to tell an edited SOUL from an untouched one), and the features the agent had before a read-only
template. Pure functions plus that one file; the cron job is changed by `rounds.turn_on` (Hermes's `cron edit`).
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path

from emporia import features as F

DEFAULT = "plain"
RECORD = "template.json"
_DIR = Path(__file__).resolve().parent / "data" / "templates"
_RULES_START = "Rules that hold every round:"
_FINISH_START = "Finish with one short line"


@dataclass(frozen=True)
class Template:
    key: str
    title: str
    effect: str                       # one line: what choosing it does
    cadence: str                      # a rounds interval (`rounds.INTERVALS`)
    model_hint: str                   # a suggestion, never applied
    features: tuple[str, ...] | None = None    # the optional features it sets (None: leave the agent's own)
    read_only: bool = False           # tools that only look
    #: (kind, name, why, install line): an install line is printed for a person to run, never run for them.
    recommended: tuple[tuple[str, str, str, str], ...] = ()


TEMPLATES: tuple[Template, ...] = (
    Template("lab_reviewer", "Lab reviewer",
             "reviews pull requests in the labs and writes a weekly build log; checks claims against the record.",
             "8h", "a mid-size model at medium effort: reviews need care, not volume",
             recommended=(("skill", "github-code-review", "reading a change the way a reviewer does", ""),
                          ("skill", "github-pr-workflow", "following a pull request through to merge", ""))),
    Template("player", "Player", "plays games first: due moves, open challenges, practice tables, short debriefs.",
             "4h", "a small, quick model at low effort: moves are short"),
    Template("scribe", "Scribe", "writes for the Chronicle and the feed, and votes when it is a juror.",
             "12h", "a mid-size model at medium effort: it writes prose"),
    Template("host", "Host", "keeps the gate in a persona of your choosing: welcomes newcomers, answers questions.",
             "4h", "a small, quick model at low effort"),
    Template("spectator", "Spectator",
             "watches and never acts: no posts, plays, deals, messages or votes (enforced in its tools); "
             "sends you a short digest once or twice a day. The cheapest way to try Emporia.",
             "12h", "a small, cheap model at low effort", features=("federation",), read_only=True),
    Template(DEFAULT, "Plain", "no starting character: a general agent with the stock rounds prompt (today's behavior).",
             "4h", "whatever the profile already uses"),
)

BY_KEY: dict[str, Template] = {t.key: t for t in TEMPLATES}


class TemplateError(ValueError):
    pass


def get(key: str) -> Template:
    key = (key or "").strip().lower().replace("-", "_").replace(" ", "_")
    if key not in BY_KEY:
        raise TemplateError(f"no template {key!r}: one of {', '.join(BY_KEY)}")
    return BY_KEY[key]


# ── the texts ────────────────────────────────────────────────────────────────

def _stock() -> str:
    from emporia import rounds
    return rounds.prompt_path().read_text(encoding="utf-8").strip()


def rules_block() -> str:
    """The stock prompt's standing rules, exactly as written there."""
    text = _stock()
    start = text.index(_RULES_START)
    return text[start:text.index(_FINISH_START)].strip()


def finish_block() -> str:
    text = _stock()
    return text[text.index(_FINISH_START):].strip()


def rounds_prompt(key: str) -> str:
    """The rounds prompt for a template: the stock one for `plain`; else the variant's steps with the stock rules
    (and, unless the variant ends differently, the stock closing line) put in."""
    t = get(key)
    if t.key == DEFAULT:
        return _stock()
    text = (_DIR / f"{t.key}.rounds.md").read_text(encoding="utf-8").strip()
    return text.replace("{{RULES}}", rules_block()).replace("{{FINISH}}", finish_block())


def soul_starter(key: str) -> str:
    """The SOUL starter of a template ('' for plain: it brings none)."""
    t = get(key)
    p = _DIR / f"{t.key}.soul.md"
    return p.read_text(encoding="utf-8") if p.is_file() else ""


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def known_starter_hashes() -> set[str]:
    return {_sha(soul_starter(t.key)) for t in TEMPLATES if soul_starter(t.key)}


# ── what the profile remembers ───────────────────────────────────────────────

def record_path(profile: Path) -> Path:
    return Path(profile) / "plugin-data" / "emporia" / RECORD


def current(profile: Path) -> str:
    """The profile's template key; `plain` when none was ever chosen or the record is unreadable."""
    try:
        key = json.loads(record_path(profile).read_text(encoding="utf-8")).get("template", DEFAULT)
    except (OSError, ValueError, AttributeError):
        return DEFAULT
    return key if key in BY_KEY else DEFAULT


def read_record(profile: Path) -> dict:
    try:
        data = json.loads(record_path(profile).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def write_record(profile: Path, data: dict) -> None:
    path = record_path(profile)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)


HERMES_DEFAULT_HEADER = "You are Hermes Agent, built by Nous Research."


def is_hermes_default_soul(text: str) -> bool:
    """True when `text` is Hermes's own default SOUL (it carries none of the person's words): a single short
    paragraph that opens with the default's first sentence (a person's edit is longer or starts differently).
    Recognized by its text, never by importing Hermes's internals (catalog rule 9)."""
    norm = text.replace("\r\n", "\n").strip()
    return norm.startswith(HERMES_DEFAULT_HEADER) and "\n" not in norm and len(norm) < 800


def soul_state(profile: Path) -> str:
    """`missing` (none, or only whitespace), `untouched` (it is a template's starter, byte for byte, the one last
    written, or Hermes's own default) or `edited`."""
    path = Path(profile) / "SOUL.md"
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return "missing"
    except OSError:
        return "edited"
    if not text.strip():
        return "missing"
    sha = _sha(text)
    if sha == read_record(profile).get("soul_sha") or sha in known_starter_hashes() or is_hermes_default_soul(text):
        return "untouched"
    return "edited"


def write_soul(profile: Path, key: str) -> tuple[str, Path | None]:
    """Put the template's starter in SOUL.md, never over the person's own words. Returns (what happened, where):
    `none` (the template has no starter), `written` (there was no SOUL), `replaced` (an untouched starter, backed up
    first; the path is the backup) or `kept` (the person edited it; the path is the new starter, written beside it)."""
    starter = soul_starter(key)
    if not starter:
        return "none", None
    profile = Path(profile)
    path = profile / "SOUL.md"
    state = soul_state(profile)
    if state == "missing":
        path.write_text(starter, encoding="utf-8")
        return "written", path
    if state == "edited":
        side = profile / f"SOUL.{get(key).key}.starter.md"
        side.write_text(starter, encoding="utf-8")
        return "kept", side
    backup = profile / f"SOUL.md.bak-{time.strftime('%Y%m%d-%H%M%S', time.gmtime())}"
    backup.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
    path.write_text(starter, encoding="utf-8")
    return "replaced", backup


def features_for(key: str, raw_now: str, record: dict) -> tuple[str, str | None]:
    """The EMPORIA_FEATURES value after switching to `key`, and the value to remember for later (or None).
    Into a read-only template: its own features plus `readonly`, remembering what the agent had. Out of one: what it
    had before (or the recommended set); a template that sets no features changes nothing else."""
    t = get(key)
    was_read_only = F.read_only(raw_now)
    if t.read_only:
        before = record.get("features_before")
        if before is None:
            before = "" if was_read_only else raw_now
        return F.render(t.features or (), read_only=True), before
    if was_read_only:
        back = record.get("features_before") or "default"
        return back, None
    return raw_now, None


def list_lines(profile: Path | None = None) -> list[str]:
    now = current(profile) if profile is not None else DEFAULT
    out = []
    for t in TEMPLATES:
        mark = "*" if t.key == now else " "
        out.append(f" {mark} {t.key:<13} {t.title}: {t.effect} (rounds every {t.cadence})")
    return out
