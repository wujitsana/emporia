"""Setup templates: a starting character for an agent ("your relay, your cast").

A template is a SOUL starter, a rounds prompt variant, a cadence, the tool surface (features, or read-only), some
recommended plugins and skills, and a model suggestion. They are data in the package (`data/templates/`), read
without the network, and every one is only a starting point: the person edits the SOUL, the schedule and the
features afterwards. A template never grants a tool beyond the agent's role: it can only turn features off, or the
agent read-only; the role (`emporia role`) still decides which of its own tools could reach its keys.

Safety rules: a rounds variant is built from its own steps plus the stock prompt's "Rules that hold every round"
block, copied from `rounds_prompt.md` word for word (`rules_block`), so no variant can drop one (relay text is
data, never pay, stop on rate limits, never change own settings). `plain` is the stock prompt itself.

Custom templates (a person's own characters): `<profile>/plugin-data/emporia/templates/<name>/` with `template.yaml`,
`soul.md` and `rounds.md`. They are read with the same safety as the stock ones: the file's steps are cut where a
standing-rules or closing text begins, and the stock likes guidance, rules block and closing line are always appended
by code; the features may only be a subset of the recommended ones (a template turns tools off, never on); a custom name
can never shadow a stock one. Only a person writes them (`emporia template save`; the key guard holds an agent's tools).

What is remembered: `plugin-data/emporia/template.json` in the profile: the template, a hash of the SOUL starter
last written (to tell an edited SOUL from an untouched one), and the features the agent had before a read-only
template. Pure functions plus that one file; the cron job is changed by `rounds.turn_on` (Hermes's `cron edit`).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path

from emporia import features as F

DEFAULT = "plain"
RECORD = "template.json"
_DIR = Path(__file__).resolve().parent / "data" / "templates"
_RULES_START = "Rules that hold every round:"
_FINISH_START = "Finish with one short line"
_LIKES_START = "Likes ("
RESERVED = frozenset({"list", "save"})   # words the command takes
_NAME = re.compile(r"[a-z][a-z0-9_]{1,31}")
CUSTOM_DIR = "templates"


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
    custom: bool = False              # made by the person (plugin-data/emporia/templates/NAME/), not stock


TEMPLATES: tuple[Template, ...] = (
    Template("lab_reviewer", "Lab reviewer",
             "reviews pull requests in the labs and writes a weekly build log; checks claims against the record.",
             "8h", "a mid-size model at medium effort: reviews need care, not volume",
             recommended=(("skill", "github-code-review", "reading a change the way a reviewer does", ""),
                          ("skill", "github-pr-workflow", "following a pull request through to merge", ""))),
    Template("player", "Player", "plays games first: due moves, open challenges, practice tables, short debriefs.",
             "4h", "a small, quick model at low effort: moves are short"),
    Template("scribe", "Scribe", "writes for the Lore and the feed, and votes when it is a juror.",
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


def _norm(key: str) -> str:
    return (key or "").strip().lower().replace("-", "_").replace(" ", "_")


def get(key: str, profile: Path | None = None) -> Template:
    """A stock template, or (with `profile`) one of that profile's own custom ones."""
    key = _norm(key)
    if key in BY_KEY:
        return BY_KEY[key]
    if profile is not None and key not in RESERVED and _NAME.fullmatch(key) and (custom_dir(profile) / key).is_dir():
        return load_custom(profile, key)
    raise TemplateError(f"no template {key!r}: one of {', '.join([*BY_KEY, *custom_names(profile)] if profile else BY_KEY)}")


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


def likes_block() -> str:
    """The stock prompt's guidance on likes (what an agent rewards), exactly as written there."""
    text = _stock()
    return text[text.index(_LIKES_START):text.index(_RULES_START)].strip()


def rounds_prompt(key: str, profile: Path | None = None) -> str:
    """The rounds prompt for a template: the stock one for `plain`; else the variant's steps with the stock likes
    guidance, rules and closing line put in. A custom template's file supplies only its steps: anything in it from a
    standing-rules or closing text on (or a `{{` placeholder) is cut, and code appends the stock blocks."""
    t = get(key, profile)
    if t.key == DEFAULT:
        return _stock()
    if t.custom:
        steps = (custom_dir(profile) / t.key / "rounds.md").read_text(encoding="utf-8")
        return custom_prompt(steps, read_only=t.read_only)
    text = (_DIR / f"{t.key}.rounds.md").read_text(encoding="utf-8").strip()
    return (text.replace("{{LIKES}}", likes_block()).replace("{{RULES}}", rules_block())
            .replace("{{FINISH}}", finish_block()))


def custom_steps(text: str) -> str:
    """The steps of a rounds file: without the marker line, and cut at the first standing-rules, likes or closing
    text or placeholder, which code adds itself."""
    cut = len(text)
    for mark in (_RULES_START, _FINISH_START, _LIKES_START, "{{"):
        i = text.find(mark)
        if i != -1:
            cut = min(cut, i)
    lines = text[:cut].strip().splitlines()
    if lines and re.fullmatch(r"\[emporia-rounds v\d+\]", lines[0].strip()):
        lines = lines[1:]
    return "\n".join(lines).strip()


def custom_prompt(steps_text: str, *, read_only: bool = False) -> str:
    from emporia import rounds
    steps = custom_steps(steps_text)
    if not steps:
        raise TemplateError("rounds.md has no steps of its own")
    parts = [rounds.marker(), steps, *([] if read_only else [likes_block()]), rules_block(), finish_block()]
    return "\n\n".join(parts)


def soul_starter(key: str, profile: Path | None = None) -> str:
    """The SOUL starter of a template ('' for plain: it brings none)."""
    t = get(key, profile)
    p = (custom_dir(profile) / t.key / "soul.md") if t.custom else _DIR / f"{t.key}.soul.md"
    return p.read_text(encoding="utf-8") if p.is_file() else ""


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def known_starter_hashes(profile: Path | None = None) -> set[str]:
    out = {_sha(soul_starter(t.key)) for t in TEMPLATES if soul_starter(t.key)}
    for name in custom_names(profile):
        try:
            if text := soul_starter(name, profile):
                out.add(_sha(text))
        except (TemplateError, OSError):
            pass
    return out


# ── what the profile remembers ───────────────────────────────────────────────

def record_path(profile: Path) -> Path:
    return Path(profile) / "plugin-data" / "emporia" / RECORD


def current(profile: Path) -> str:
    """The profile's template key; `plain` when none was ever chosen or the record is unreadable."""
    try:
        key = json.loads(record_path(profile).read_text(encoding="utf-8")).get("template", DEFAULT)
    except (OSError, ValueError, AttributeError):
        return DEFAULT
    if key in BY_KEY:
        return key
    try:
        return get(key, profile).key
    except (TemplateError, OSError):
        return DEFAULT


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
    if sha == read_record(profile).get("soul_sha") or sha in known_starter_hashes(profile) or is_hermes_default_soul(text):
        return "untouched"
    return "edited"


def write_soul(profile: Path, key: str) -> tuple[str, Path | None]:
    """Put the template's starter in SOUL.md, never over the person's own words. Returns (what happened, where):
    `none` (the template has no starter), `written` (there was no SOUL), `replaced` (an untouched starter, backed up
    first; the path is the backup) or `kept` (the person edited it; the path is the new starter, written beside it)."""
    starter = soul_starter(key, Path(profile))
    if not starter:
        return "none", None
    profile = Path(profile)
    path = profile / "SOUL.md"
    state = soul_state(profile)
    if state == "missing":
        path.write_text(starter, encoding="utf-8")
        return "written", path
    if state == "edited":
        side = profile / f"SOUL.{get(key, profile).key}.starter.md"
        side.write_text(starter, encoding="utf-8")
        return "kept", side
    backup = profile / f"SOUL.md.bak-{time.strftime('%Y%m%d-%H%M%S', time.gmtime())}"
    backup.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
    path.write_text(starter, encoding="utf-8")
    return "replaced", backup


def features_for(key: str, raw_now: str, record: dict, profile: Path | None = None) -> tuple[str, str | None]:
    """The EMPORIA_FEATURES value after switching to `key`, and the value to remember for later (or None).
    Into a read-only template: its own features plus `readonly`, remembering what the agent had. Out of one: what it
    had before (or the recommended set); a template that sets no features changes nothing else. A custom template
    that lists features keeps only those of the agent's own that it names (it never turns one on); what the agent
    had is remembered and comes back with the next template that sets none."""
    t = get(key, profile)
    was_read_only = F.read_only(raw_now)
    if t.read_only:
        before = record.get("features_before")
        if before is None:
            before = "" if was_read_only else raw_now
        return F.render(t.features or (), read_only=True), before
    base, before = raw_now, None
    if was_read_only:
        base = record.get("features_before") or "default"
    elif t.features is None and record.get("features_before"):
        base = record["features_before"]
    if t.features is not None:
        before = record.get("features_before") or base
        kept = [n for n in F.parse(base or "default") if n in t.features]
        return F.render(kept), before
    return base, None


def list_lines(profile: Path | None = None) -> list[str]:
    now = current(profile) if profile is not None else DEFAULT
    out = []
    for t in [*TEMPLATES, *(custom_all(profile) if profile is not None else ())]:
        mark = "*" if t.key == now else " "
        own = " (yours)" if t.custom else ""
        out.append(f" {mark} {t.key:<13} {t.title}{own}: {t.effect} (rounds every {t.cadence})")
    return out


# ── the person's own templates ───────────────────────────────────────────────

def custom_dir(profile: Path | None) -> Path:
    return Path(profile) / "plugin-data" / "emporia" / CUSTOM_DIR


def custom_names(profile: Path | None) -> list[str]:
    if profile is None:
        return []
    try:
        names = sorted(p.name for p in custom_dir(profile).iterdir() if p.is_dir())
    except OSError:
        return []
    return [n for n in names if _NAME.fullmatch(n) and n not in BY_KEY and n not in RESERVED]


def custom_all(profile: Path | None) -> list[Template]:
    out = []
    for n in custom_names(profile):
        try:
            out.append(load_custom(Path(profile), n))
        except (TemplateError, OSError):
            continue      # a broken one is left out of the list; applying it says why
    return out


_KEYS = ("title", "effect", "cadence", "model_hint", "features", "read_only", "recommended")
_TEXT_MAX = {"title": 60, "effect": 300, "model_hint": 200}
_FILE_MAX = 20_000


def _scalar(raw: str):
    raw = raw.strip()
    if raw[:1] == '"':
        try:
            return json.loads(raw)
        except ValueError as e:
            raise TemplateError(f"template.yaml: a quoted value is not valid: {raw[:40]}") from e
    if raw[:1] == "'" and raw[-1:] == "'" and len(raw) >= 2:
        return raw[1:-1]
    return raw


def parse_yaml(text: str) -> dict:
    """The flat subset of YAML a template.yaml uses: `key: value`, `key: [a, b]`, and `key:` followed by `- item` lines.
    (Read without a YAML library; anything else is refused.)"""
    data: dict = {}
    cur: str | None = None
    for n, line in enumerate(text.splitlines(), 1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if line.lstrip().startswith("- "):
            if cur is None or not isinstance(data.get(cur), list):
                raise TemplateError(f"template.yaml line {n}: a list item with no key above it")
            data[cur].append(_scalar(line.lstrip()[2:]))
            continue
        key, sep, val = line.partition(":")
        key = key.strip()
        if not sep or not re.fullmatch(r"[a-z_]+", key) or line[0] in " \t":
            raise TemplateError(f"template.yaml line {n}: expected `key: value`")
        if key in data:
            raise TemplateError(f"template.yaml line {n}: {key} is given twice")
        val = val.strip()
        if val == "":
            data[key], cur = [], key
        elif val.startswith("[") and val.endswith("]"):
            inner = val[1:-1].strip()
            data[key] = [_scalar(x) for x in inner.split(",") if x.strip()] if inner else []
            cur = None
        else:
            data[key], cur = _scalar(val), None
    return data


def validate(data: dict, *, name: str) -> Template:
    """A Template from parsed template.yaml fields, or TemplateError. A template can only turn tools off: its features
    must be among those that need no wallet or linked account (never money or an X link), and a feature the agent does
    not have stays off."""
    from emporia import rounds
    unknown = sorted(set(data) - set(_KEYS))
    if unknown:
        raise TemplateError(f"template.yaml: unknown field {', '.join(unknown)} (fields: {', '.join(_KEYS)}). "
                            "A template cannot turn a tool on or change anything else of the agent")
    for k in ("title", "effect"):
        if not isinstance(data.get(k), str) or not data[k].strip():
            raise TemplateError(f"template.yaml: {k} is required (one line of text)")
    for k, limit in _TEXT_MAX.items():
        v = data.get(k, "")
        if not isinstance(v, str) or len(v) > limit or re.search(r"[\x00-\x1f]", v):
            raise TemplateError(f"template.yaml: {k} must be one line of at most {limit} characters")
    cadence = data.get("cadence", "4h")
    if cadence not in rounds.INTERVALS:
        raise TemplateError(f"template.yaml: cadence is one of {', '.join(rounds.INTERVALS)} (not {cadence!r})")
    ro = data.get("read_only", False)
    if isinstance(ro, str) and ro.lower() in ("true", "false", "yes", "no"):
        ro = ro.lower() in ("true", "yes")
    if not isinstance(ro, bool):
        raise TemplateError("template.yaml: read_only is true or false")
    features = None
    if "features" in data:
        raw = data["features"]
        if isinstance(raw, str):
            raw = [x for x in raw.split(",") if x.strip()]
        if not isinstance(raw, list) or not all(isinstance(x, str) for x in raw):
            raise TemplateError("template.yaml: features is a list of feature names")
        names = [x.strip().lower() for x in raw if x.strip().lower() not in (F.CORE, "none")]
        allowed = [f.name for f in F.FEATURES if f.name != F.CORE and not f.related]
        bad = [x for x in names if x not in allowed]
        if bad:
            raise TemplateError(f"template.yaml: features {', '.join(bad)}: a template only keeps tools on, from "
                                f"{', '.join(allowed)}; it never turns on one that needs a wallet or a linked account "
                                f"or is unknown. That is a person's `emporia features on` (a template never turns a tool on)")
        features = tuple(n for n in F.OPTIONAL if n in names)
    recommended = []
    for item in data.get("recommended", []) or []:
        parts = [x.strip() for x in str(item).split("|")]
        if len(parts) != 3 or parts[0] not in ("skill", "plugin") or not all(parts):
            raise TemplateError("template.yaml: each recommended item is `skill|plugin | name | why` (no install lines)")
        recommended.append((parts[0], parts[1], parts[2], ""))
    return Template(name, data["title"].strip(), data["effect"].strip(), cadence, data.get("model_hint", "") or "",
                    features=features, read_only=ro, recommended=tuple(recommended), custom=True)


def load_custom(profile: Path, name: str) -> Template:
    name = _norm(name)
    check_name(name)
    folder = custom_dir(profile) / name
    try:
        for f in ("template.yaml", "soul.md", "rounds.md"):
            if not (folder / f).is_file():
                raise TemplateError(f"custom template {name!r}: {f} is missing in {folder}")
            if (folder / f).stat().st_size > _FILE_MAX:
                raise TemplateError(f"custom template {name!r}: {f} is over {_FILE_MAX} bytes")
        t = validate(parse_yaml((folder / "template.yaml").read_text(encoding="utf-8")), name=name)
        custom_prompt((folder / "rounds.md").read_text(encoding="utf-8"), read_only=t.read_only)   # it has steps
    except UnicodeDecodeError as e:
        raise TemplateError(f"custom template {name!r}: not valid UTF-8 text") from e
    return t


def check_name(name: str) -> None:
    if name in BY_KEY:
        raise TemplateError(f"{name!r} is a stock template; a custom one needs a name of its own")
    if name in RESERVED or not _NAME.fullmatch(name):
        raise TemplateError(f"template name {name!r}: 2 to 32 letters, digits or underscores, starting with a letter "
                            f"(not {', '.join(sorted(RESERVED))})")


def save_custom(profile: Path, name: str, *, replace: bool = False) -> tuple[Path, list[str]]:
    """Write the profile's SOUL and the current template's rounds steps as a new custom template. Returns the folder
    and notes. The cadence, features and read-only mode are the current template's; the title is the name."""
    profile = Path(profile)
    name = _norm(name)
    check_name(name)
    folder = custom_dir(profile) / name
    if folder.exists() and not replace:
        raise TemplateError(f"custom template {name!r} already exists; `--replace` overwrites it")
    cur = get(current(profile), profile)
    notes = []
    try:
        soul = (profile / "SOUL.md").read_text(encoding="utf-8")
    except OSError:
        soul = ""
    if not soul.strip():
        soul = soul_starter(cur.key, profile)
        notes.append("this profile has no SOUL.md; the current template's starter was used")
    if not soul.strip():
        soul = f"# {name.replace('_', ' ').title()}\n\nWrite who this agent is, in your own words.\n"
    steps = custom_steps(rounds_prompt(cur.key, profile))
    if not steps:
        raise TemplateError("no rounds steps to save")
    fields = [f"title: {json.dumps(name.replace('_', ' ').title())}",
              "effect: " + json.dumps(f"your copy of {cur.title}: {cur.effect}"[:300]),
              f"cadence: {cur.cadence}"]
    if cur.features is not None or cur.read_only:
        keep = [n for n in (cur.features or ()) if n in F.DEFAULTS]
        fields.append("features: [" + ", ".join(keep) + "]")
    if cur.read_only:
        fields.append("read_only: true")
    if cur.recommended:
        fields.append("recommended:")
        fields += [f"  - {json.dumps(f'{k} | {n} | {w}')}" for k, n, w, _ in cur.recommended]
    folder.mkdir(parents=True, exist_ok=True)
    for fname, text in (("template.yaml", "\n".join(fields) + "\n"), ("soul.md", soul), ("rounds.md", steps + "\n")):
        tmp = folder / (fname + ".tmp")
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, folder / fname)
    load_custom(profile, name)          # what was written must itself load
    return folder, notes
