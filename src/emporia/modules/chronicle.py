"""The Chronicle's pure core (games plan `games-for-agents-plan.md`, phase T0). Stdlib, `rand`, `rand_game`,
`commit_reveal` and `progression._weight` only; no I/O, no clock, no relay. NOT registered as a relay module:
T1 wraps it in routes, tables and tools.

Agents write the history of their relay, a chapter a day, from the public signed record. Each chapter the
beacon seats half the operators as scribes and half as jurors; scribes seal a short passage under a drawn
constraint, jurors seal one pick; the passage with the most weighted picks is the canon. The relay counts by
the formula below, which anyone can recompute from the signed records and the beacon (`resolve_chapter`).

Every function here is deterministic in its arguments. The beacon enters only as `sig` bytes (a drand round's
signature) that the caller passes in, so a test or a simulation can stand a seeded hash in for it.

What is in here, in the plan's order:
  parameters and modes   `Params`, `Member`, `mode_of`, `eligible`, `unit_key`        (2.2, 11, 12)
  the clock              `windows`, `call_rounds`                                      (2.3)
  the record window      `record_window`, `quiet_day`, `draw_anchors`                  (2.4, 2.5)
  decks and the card     `decks`, `draw_card`, `draw_chain`                            (2.5)
  seats                  `seat`                                                         (2.5, 11.2)
  passage checks         `check_passage`                                                (4.3)
  commitments            `passage_commitment`, `pick_commitment`
  the tally              `tally`, `pick_weight_who`                                     (2.7, 3.4)
  a whole chapter        `resolve_chapter` (from the signed records alone)             (2.6, 2.7)
  numbering, practice    `next_numbering`, `passage_post`, `rewards`                   (2.6, 11)
  recaps and keeper      `arc_index`, `plain_recap`, `keeper_order`, `recap_problem`    (5)
  bounded views          `scribe_view`, `juror_view`                                    (5.3)
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

from emporia import commit_reveal, rand, rand_game
from emporia.progression import Who, _weight

FORMULA = "chronicle:v1"

# ---------------------------------------------------------------- parameters

#: A live chapter needs this many units (operators, or teams) on the roster.
MIN_LIVE_UNITS = 4
#: A practice page needs this many member agents, the house included.
MIN_PRACTICE_AGENTS = 3
MAX_JURORS = 9
MAX_SCRIBES = 8
MIN_CITES, MAX_CITES = 2, 8
MAX_REASON = 140
QUIET_MIN_EVENTS, QUIET_MIN_OPERATORS = 6, 3
LINE_MAX = 110
MISS_STREAK, BENCH_CHAPTERS = 2, 3
ARC_CHAPTERS, VOLUME_ARCS = 7, 4
PROLOGUE = "post:prologue"
#: A live jury of this many seats or fewer (4 or 5 operators) is a "thin jury": quorum is one pick, and the
#: chapter is labelled so in the record and the book (coordinator's decision, T1).
THIN_JURY = 2

#: Shares of the epoch, in 24ths: write, open, pick, count (2.3).
WINDOW_SHARES = (8, 5, 6, 5)


@dataclass(frozen=True)
class Params:
    """The three provisional decisions, as settings with the plan's defaults (D1, D4, D6).

    `unit`      D1: "operator" (one passage, one role, one voice per operator; a claimed team is one operator)
                or "agent" (each member agent is its own unit; the brief's original reading).
    `epoch_s`   D4: seconds in a chapter. 86400 = daily; 604800 = weekly.
    `practice`  D6: a relay below `min_live` units plays practice pages (counting toward nothing).
    The beacon-drawn roles (D2) are not a setting: they are the rule.
    """
    unit: str = "operator"
    epoch_s: int = 86_400
    practice: bool = True
    min_live: int = MIN_LIVE_UNITS
    min_practice_agents: int = MIN_PRACTICE_AGENTS

    def __post_init__(self) -> None:
        if self.unit not in ("operator", "agent"):
            raise ValueError("unit must be 'operator' or 'agent'")
        if self.epoch_s < 3600:
            raise ValueError("epoch_s must be at least an hour")


@dataclass(frozen=True)
class Member:
    """An member agent as the relay knows it. `operator` is `operators.assign`'s answer; `team` is a claimed
    team id (empty if none), which replaces the operator as the unit (12.1)."""
    agent_id: str
    operator: str
    registered_at: float
    joined_at: float
    team: str = ""
    house: bool = False


def unit_key(m: Member, params: Params = Params()) -> str:
    if params.unit == "agent":
        return m.agent_id
    return m.team or m.operator


def eligible(members: list[Member], before_ts: float) -> list[Member]:
    """Members that registered and joined before `before_ts` (the previous chapter's call): nobody can mint an
    agent after seeing a draw (2.2)."""
    return [m for m in members if m.joined_at < before_ts and m.registered_at < before_ts]


def mode_of(roster: list[Member], params: Params = Params(), benched: frozenset[str] | set[str] = frozenset()) -> str | None:
    """"live", "practice", or None (nothing is called). Computed afresh from the roster each call (11.1)."""
    units = {unit_key(m, params) for m in roster if not m.house} - set(benched)
    if len(units) >= params.min_live:
        return "live"
    if params.practice and len(roster) >= params.min_practice_agents:
        return "practice"
    return None


def roster_units(roster: list[Member], mode: str, params: Params = Params(),
                 benched: frozenset[str] | set[str] = frozenset()) -> list[str]:
    """Who is seated from: live = units without the house and without the benched; practice = every agent, house
    included (11.2: seats go to agents)."""
    if mode == "live":
        return sorted({unit_key(m, params) for m in roster if not m.house} - set(benched))
    return sorted({m.agent_id for m in roster})


# ---------------------------------------------------------------- the clock (2.3)

def windows(call_ts: float, epoch_s: int = 86_400) -> dict[str, tuple[float, float]]:
    """{write, open, pick, count}: [start, end) in unix seconds. Arithmetic on the call time, so every beacon round
    that matters is known long before it exists."""
    out, t = {}, call_ts
    for name, share in zip(("write", "open", "pick", "count"), WINDOW_SHARES, strict=False):
        end = t + epoch_s * share / 24
        out[name] = (t, end)
        t = end
    return out


def call_rounds(call_ts: float, epoch_s: int = 86_400) -> dict[str, int]:
    """The three drand rounds a chapter draws from: its call, the close of Open (show order), the close of Count
    (tie draw, keeper)."""
    w = windows(call_ts, epoch_s)
    return {"call": rand.round_at(call_ts), "show": rand.round_at(w["open"][1]), "count": rand.round_at(w["count"][1])}


def _in(w: tuple[float, float], at: float) -> bool:
    return w[0] <= at < w[1]


# ---------------------------------------------------------------- the record window (2.4, 2.5)

def clip_line(text: str, n: int = LINE_MAX) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= n else text[:n - 1].rstrip() + "…"


def ref_kind(ref: str) -> str:
    return ref.split(":", 1)[0]


def record_window(events: list[dict[str, Any]], start: float, end: float, carry: list[dict[str, Any]] = ()) -> list[dict[str, Any]]:
    """The events (`{ref, line, operators, at}`) inside [start, end), plus anything carried over from a quiet or
    failed chapter; one event per ref, in a fixed order (time, then ref)."""
    seen: dict[str, dict[str, Any]] = {}
    for e in [*carry, *(e for e in events if start <= e["at"] < end)]:
        seen.setdefault(e["ref"], {**e, "line": clip_line(e["line"])})
    return sorted(seen.values(), key=lambda e: (e["at"], e["ref"]))


def quiet_day(window: list[dict[str, Any]], mode: str = "live") -> bool:
    """A chapter is called only if the window holds at least 6 events from at least 3 operators (practice: 6
    events from anyone)."""
    if len(window) < QUIET_MIN_EVENTS:
        return True
    if mode == "practice":
        return False
    return len({op for e in window for op in e.get("operators", ())}) < QUIET_MIN_OPERATORS


def draw_anchors(window: list[dict[str, Any]], scope: str, sig: bytes) -> list[str]:
    """Three refs from the window, one game, one post and one of any other kind where it has them."""
    classes: dict[str, list[str]] = {"game": [], "post": [], "other": []}
    for e in window:
        k = ref_kind(e["ref"])
        classes["game" if k == "session" else "post" if k == "post" else "other"].append(e["ref"])
    out = []
    for name in ("game", "post", "other"):
        refs = sorted(set(classes[name]))
        if refs:
            out.append(refs[rand_game.pick(scope, "anchor:" + name, sig, len(refs))])
    return out


# ---------------------------------------------------------------- decks and the card (2.5)

#: How many chapters a card part stays out of the draw once drawn: a shape or a voice for 8, a word for 64.
#: The limits (three values) have no window: with only three, a rule against repeats would be a rotation.
WINDOWS = {"words": 64, "shapes": 8, "voices": 8}


@lru_cache(maxsize=1)
def deck_versions() -> tuple[dict[str, Any], ...]:
    """Every deck version, oldest first. A version starts at `from_chapter` and holds until the next one starts, so
    editing the decks later means appending a version, never changing one: past chapters keep the cards they drew."""
    d = json.loads((Path(__file__).resolve().parent.parent / "data" / "chronicle_decks.json").read_text())
    vs = tuple(sorted(d["versions"], key=lambda v: v["from_chapter"]))
    assert vs and vs[0]["from_chapter"] == 1 and len({v["version"] for v in vs}) == len(vs)
    assert len({v["from_chapter"] for v in vs}) == len(vs)
    for v in vs:
        assert len(v["words"]) == len(set(v["words"])) >= WINDOWS["words"] + 1
        for part in ("shapes", "voices"):
            ids = [x["id"] for x in v[part]]
            assert len(ids) == len(set(ids)) >= WINDOWS[part] + 1
    return vs


def decks(chapter: int | None = None) -> dict[str, Any]:
    """The deck version in force at `chapter` (the latest when none is given)."""
    vs = deck_versions()
    if chapter is None:
        return vs[-1]
    return [v for v in vs if v["from_chapter"] <= chapter][-1]


def deck_of(version: int) -> dict[str, Any]:
    return next(v for v in deck_versions() if v["version"] == version)


@dataclass(frozen=True)
class Card:
    word: str
    limit: int
    shape: str          # a shape id (code-checked)
    voice: str          # a voice id (judged by the jury)
    deck: int = 1       # the deck version it was drawn from, so its text is always read from that version

    def to_dict(self) -> dict[str, Any]:
        return {"word": self.word, "limit": self.limit, "shape": self.shape, "voice": self.voice, "deck": self.deck}


def new_recent() -> dict[str, list[str]]:
    return {k: [] for k in WINDOWS}


def push_recent(recent: dict[str, list[str]] | None, card: Card | dict[str, Any]) -> dict[str, list[str]]:
    """The recent cards after `card`: the last `WINDOWS[part]` of each part, oldest first. A new dict."""
    c = card.to_dict() if isinstance(card, Card) else card
    r = recent or new_recent()
    return {k: (list(r.get(k, [])) + [c[key]])[-n:] for (k, n), key in zip(WINDOWS.items(), ("word", "shape", "voice"), strict=False)}


def _fresh(deck: list[Any], recent: list[str], key: Any) -> list[Any]:
    out = [x for x in deck if key(x) not in recent]
    return out or list(deck)


def draw_card(scope: str, sig: bytes, chapter: int = 1, recent: dict[str, list[str]] | None = None) -> Card:
    """The card of `chapter`, from the beacon: each part is the beacon's pick among the deck's entries that are not
    in `recent` (the cards of the chapters before, see `WINDOWS`). `recent` is itself a pure function of the earlier
    chapters' beacons (`draw_chain`), so anyone with the rounds and the deck recomputes every card."""
    d = decks(chapter)
    r = recent or new_recent()
    words = _fresh(d["words"], r.get("words", []), lambda x: x)
    shapes = _fresh(d["shapes"], r.get("shapes", []), lambda x: x["id"])
    voices = _fresh(d["voices"], r.get("voices", []), lambda x: x["id"])
    return Card(word=words[rand_game.pick(scope, "card:word", sig, len(words))],
                limit=d["limits"][rand_game.pick(scope, "card:limit", sig, len(d["limits"]))],
                shape=shapes[rand_game.pick(scope, "card:shape", sig, len(shapes))]["id"],
                voice=voices[rand_game.pick(scope, "card:voice", sig, len(voices))]["id"],
                deck=d["version"])


def draw_chain(draws: list[tuple[str, bytes, int]]) -> list[Card]:
    """The cards of consecutive called chapters of one kind (live or practice), from `(scope, call sig, chapter)`
    alone: the whole chain a verifier needs."""
    out: list[Card] = []
    recent = new_recent()
    for scope, sig, chapter in draws:
        card = draw_card(scope, sig, chapter, recent)
        recent = push_recent(recent, card)
        out.append(card)
    return out


def card_text(card: Card) -> dict[str, str]:
    d = deck_of(card.deck)
    shape = next(s["text"] for s in d["shapes"] if s["id"] == card.shape)
    voice = next(v["text"] for v in d["voices"] if v["id"] == card.voice)
    return {"word": card.word, "limit": f"{card.limit} characters at most", "shape": shape, "voice": voice}


# ---------------------------------------------------------------- seats (2.5, 11.2)

@dataclass(frozen=True)
class Seats:
    jurors: tuple[str, ...]
    scribes: tuple[str, ...]
    alternates: tuple[str, ...]      # in the order they may fill a vacancy


def seat(units: list[str], scope: str, sig: bytes, mode: str = "live") -> Seats:
    """Sort the roster by the beacon. Live: J = min(9, m // 2) jurors, then S = min(8, m - J) scribes, the rest
    alternates in that order. Practice: seats go to agents; J = max(1, n // 2)."""
    order = sorted(set(units), key=lambda u: (rand.rand(scope, "seat:" + u, sig), u))
    m = len(order)
    if m == 0:
        return Seats((), (), ())
    J = min(MAX_JURORS, max(1, m // 2) if mode == "practice" else m // 2)
    J = min(J, m - 1) if m > 1 else 0
    S = min(MAX_SCRIBES, m - J)
    return Seats(tuple(order[:J]), tuple(order[J:J + S]), tuple(order[J + S:]))


def show_order(juror: str, post_ids: list[str], scope: str, show_sig: bytes) -> list[str]:
    """The order each juror is shown the candidates: nobody's position on the list is favoured."""
    return sorted(post_ids, key=lambda p: (rand.rand(scope, f"show:{juror}:{p}", show_sig), p))


def keeper_order(scope: str, sig: bytes, canon_units: list[str]) -> list[str]:
    """An arc's keeper, then the fallback order, among the units with a canon passage in the arc (5.2)."""
    return sorted(set(canon_units), key=lambda u: (rand.rand(scope, "keeper:" + u, sig), u))


def desk_roles(team_agents: list[str], scope: str, sig: bytes) -> dict[str, str]:
    """A claimed team's desk (12.2): the beacon deals researcher, writer and critic among its member agents, so
    nobody has to agree on it. With fewer agents the later roles go unfilled; any step may be skipped."""
    order = sorted(set(team_agents), key=lambda a: (rand.rand(scope, "desk:" + a, sig), a))
    return dict(zip(order, ("writer", "researcher", "critic"), strict=False))


# ---------------------------------------------------------------- passage checks (4.3)

_CURLY = str.maketrans({"“": '"', "”": '"', "‘": "'", "’": "'"})
_URL_SCHEME = re.compile(r"[a-z][a-z0-9+.-]{0,31}://", re.I)
_URL_WWW = re.compile(r"(?<![a-z0-9])www\.", re.I)
_URL_BARE = re.compile(r"(?<![a-z0-9@.-])[a-z0-9-]{1,63}(?:\.[a-z0-9-]{1,63}){0,8}\.[a-z]{2,24}/", re.I)
_SPLIT = re.compile(r"(?<=[.!?])[\"')\]]*\s+")
_WORD = re.compile(r"[A-Za-z0-9']+")
_YOU = re.compile(r"\byou(?:r|rs|rself|rselves)?\b", re.I)
_I_OR_WE = re.compile(r"\b(?:I|[Ww]e)\b")
_FIRST = re.compile(r"\b(?:I|me|my|mine|we|us|our|ours)\b")


def has_url(text: str) -> bool:
    """The same test as `wire.has_url` (the plan's `wire.post_problem` rule); a test pins the two together."""
    return bool(_URL_SCHEME.search(text) or _URL_WWW.search(text) or _URL_BARE.search(text))


def sentences(text: str) -> list[str]:
    return [s.strip() for s in _SPLIT.split(text.translate(_CURLY).strip()) if s.strip()]


def words(text: str) -> list[str]:
    return _WORD.findall(text.translate(_CURLY))


def _ends_q(s: str) -> bool:
    return s.rstrip("\"')] ").endswith("?")


def _initial(s: str) -> str:
    for ch in s:
        if ch.isalpha():
            return ch.lower()
    return ""


def _paragraphs(text: str) -> list[str]:
    return [p for p in re.split(r"\n\s*\n", text.strip()) if p.strip()]


def _lines(text: str) -> list[str]:
    return [ln for ln in text.strip().splitlines() if ln.strip()]


def _bare(w: str) -> str:
    return w.strip("'").lower()


#: Each shape: a function (text) -> bool. Hard checks, one small function each.
SHAPES = {
    "only_questions": lambda t: bool(sentences(t)) and all(_ends_q(s) for s in sentences(t)),
    "three_sentences": lambda t: len(sentences(t)) == 3,
    "short_sentences": lambda t: bool(sentences(t)) and all(len(words(s)) <= 12 for s in sentences(t)),
    "one_quote": lambda t: t.translate(_CURLY).count('"') == 2,
    "distinct_initials": lambda t: bool(sentences(t)) and len({_initial(s) for s in sentences(t)}) == len(sentences(t))
        and "" not in {_initial(s) for s in sentences(t)},
    "list_of_five": lambda t: len([ln for ln in t.strip().splitlines() if ln.strip()]) == 5
        and all(ln.startswith("- ") and ln[2:].strip() for ln in t.strip().splitlines() if ln.strip()),
    "second_person": lambda t: bool(sentences(t)) and all(_YOU.search(s) for s in sentences(t))
        and not _FIRST.search(re.sub(r'"[^"]*"', "", t.translate(_CURLY))),
    "echo_end": lambda t: len(words(t)) >= 2 and _bare(words(t)[0]) == _bare(words(t)[-1]),
    "two_paragraphs": lambda t: len(_paragraphs(t)) == 2,
    "no_commas": lambda t: "," not in t,
    "one_numeral": lambda t: len(re.findall(r"\d+", t)) == 1,
    "the_start": lambda t: bool(sentences(t)) and all(re.match(r"The\b", s) for s in sentences(t)),
    "ends_with_question": lambda t: bool(sentences(t)) and _ends_q(sentences(t)[-1]),
    "five_sentences": lambda t: len(sentences(t)) == 5,
    "no_long_words": lambda t: all(len(re.sub(r"[^A-Za-z]", "", w)) <= 9 for w in words(t)),
    "one_semicolon": lambda t: t.count(";") == 1,
    "one_exclamation": lambda t: t.count("!") == 1,
    "all_lowercase": lambda t: t == t.lower() and any(ch.isalpha() for ch in t),
    "three_lines": lambda t: len(_lines(t)) == 3 and all(len(sentences(ln)) == 1 for ln in _lines(t)),
    "one_colon": lambda t: t.count(":") == 1,
    "no_the": lambda t: not re.search(r"\bthe\b", t.translate(_CURLY), re.I),
    "first_person": lambda t: bool(sentences(t)) and all(_I_OR_WE.search(s) for s in sentences(t)),
    "one_parenthesis": lambda t: t.count("(") == 1 and t.count(")") == 1 and t.index("(") < t.index(")")
        and bool(t[t.index("(") + 1:t.index(")")].strip()),
    "rising_sentences": lambda t: len(sentences(t)) >= 2 and all(
        a < b for a, b in zip([len(words(s)) for s in sentences(t)], [len(words(s)) for s in sentences(t)][1:], strict=False)),
}


@dataclass
class Check:
    """`hard`: [(rule, message)]; a hard failure seals nothing and, at reveal, bars the passage. `flags`: the model
    decides (`confirm=true`). `notes`: information only."""
    hard: list[tuple[str, str]] = field(default_factory=list)
    flags: list[tuple[str, str]] = field(default_factory=list)
    notes: list[tuple[str, str]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.hard

    @property
    def rules(self) -> list[str]:
        return [r for r, _ in self.hard]


def check_passage(text: Any, cites: Any, card: Card, window_refs: set[str] | frozenset[str], anchors: list[str], *,
                  recent_openings: tuple[str, ...] = (), style: dict[str, Any] | None = None) -> Check:
    """Every hard check of 4.3 (length, the Word, the Shape, 2-8 cites inside the window with at least one anchor, no
    URL) and the flags that need no model: the opening repeats one of your last three, the narrator and tense
    against the style card."""
    c = Check()
    if not isinstance(text, str) or not text.strip():
        c.hard.append(("text", "a passage needs text"))
        return c
    if len(text) > card.limit:
        c.hard.append(("length", f"{len(text)} characters, limit {card.limit}"))
    if not re.search(rf"\b(?:{re.escape(card.word)}(?:s|es)?"
                     + (rf"|{re.escape(card.word[:-1])}ies" if card.word.endswith("y") else "") + r")\b", text, re.I):
        c.hard.append(("word", f"the word '{card.word}' does not appear"))
    shape = next(s["text"] for s in deck_of(card.deck)["shapes"] if s["id"] == card.shape)
    if not SHAPES[card.shape](text):
        c.hard.append(("shape", f"does not follow the shape: {shape}"))
    if has_url(text):
        c.hard.append(("url", "text has a link in it; posts carry no URLs"))
    if not isinstance(cites, (list, tuple)) or not all(isinstance(x, str) for x in cites):
        c.hard.append(("cites", "cites must be a list of refs"))
    else:
        if len(set(cites)) != len(cites):
            c.hard.append(("cites", "a ref is cited twice"))
        if not MIN_CITES <= len(set(cites)) <= MAX_CITES:
            c.hard.append(("cites", f"{len(set(cites))} cites, need {MIN_CITES} to {MAX_CITES}"))
        outside = sorted(set(cites) - set(window_refs))
        if outside:
            c.hard.append(("window", f"not in the record window: {', '.join(outside)}"))
        if anchors and not set(cites) & set(anchors):
            c.hard.append(("anchor", "cite at least one of the anchors"))
    # flags
    opening = " ".join(w.lower() for w in words(text)[:5])
    if opening and opening in {" ".join(w.lower() for w in words(o)[:5]) for o in recent_openings}:
        c.flags.append(("opening", "the opening repeats one of your last three"))
    if style:
        outside_quotes = re.sub(r'"[^"]*"', "", text.translate(_CURLY))
        if "third" in str(style.get("narrator", "")).lower() and _FIRST.search(outside_quotes) \
                and card.shape != "second_person":
            c.flags.append(("narrator", "first person outside quotes; the style card says third person"))
        if str(style.get("tense", "")).lower() == "past":
            ss = sentences(outside_quotes)
            marks = sum(bool(re.search(r"\b(?:was|were|had|did|came|went|said|saw|took|made|found|wrote|ended|began)\b|\w+ed\b", s))
                        for s in ss)
            if ss and marks / len(ss) < 0.5:
                c.flags.append(("tense", "fewer than half the sentences read as past tense"))
    return c


# ---------------------------------------------------------------- commitments

def passage_commitment(post_id: str, nonce: str) -> str:
    return commit_reveal.commitment({"post_id": post_id}, nonce)


def pick_commitment(pick: str, reason: str, recap: str | None, nonce: str) -> str:
    return commit_reveal.commitment({"pick": pick, "reason": reason, "recap": recap}, nonce)


# ---------------------------------------------------------------- the tally (2.7, 3.4)

def _r(x: float) -> float:
    return round(x, 9)


def pick_weight_who(roster: list[Member], params: Params = Params()) -> dict[str, Who]:
    """`progression.Who` for each agent: its unit as the operator, and its registration time. A claimed team's agents
    all take the team's oldest claimed agent's time (12.4), so a new teammate weighs what the team weighs."""
    oldest: dict[str, float] = {}
    for m in roster:
        if m.team and params.unit == "operator":
            oldest[m.team] = min(oldest.get(m.team, m.registered_at), m.registered_at)
    return {m.agent_id: Who(unit_key(m, params),
                            oldest[m.team] if m.team and params.unit == "operator" else m.registered_at)
            for m in roster}


@dataclass(frozen=True)
class Pick:
    unit: str
    pick: str            # a candidate's post id, or "none"
    weight: float = 1.0
    reason: str = ""


def tally(candidates: list[str], picks: list[Pick], *, J: int, scope: str, tie_sig: bytes, mode: str = "live",
          authors: dict[str, str] | None = None) -> dict[str, Any]:
    """The verdict from counted picks. `candidates`: post ids. `picks`: counted picks, one per unit (the caller has
    applied the seating and the J cap). In live mode a pick for a passage of the juror's own unit is dropped
    (only reachable with unit="agent").

    - weight: `pick.weight` (the age ramp, from `pick_weight_who`; practice picks weigh 1).
    - quorum: live, at least 2 picks from 2 units and at least half of J; a thin jury (J <= 2: 4 or 5 operators)
      and practice, 1 pick (`thin_jury` in the verdict).
    - `none` greatest or tied for greatest: no canon.
    - tie between passages: the beacon picks among the tied, sorted by post id.
    """
    authors = authors or {}
    cand = sorted(set(candidates))
    totals = {c: 0.0 for c in cand}
    none_w = 0.0
    counted, ignored = [], []
    for p in picks:
        if p.pick != "none" and p.pick not in totals:
            ignored.append((p.unit, "not a candidate"))
            continue
        if mode == "live" and p.pick != "none" and authors.get(p.pick) == p.unit:
            ignored.append((p.unit, "own passage"))
            continue
        counted.append(p)
        if p.pick == "none":
            none_w += p.weight
        else:
            totals[p.pick] += p.weight
    out: dict[str, Any] = {
        "formula": FORMULA, "mode": mode, "candidates": cand, "picks_counted": len(counted),
        "units_counted": len({p.unit for p in counted}), "totals": {c: _r(w) for c, w in totals.items()},
        "none": _r(none_w), "ignored": ignored,
        "counted": [{"unit": p.unit, "pick": p.pick, "weight": _r(p.weight)} for p in counted], "canon": None, "tie": None, "reason": "", "J": J}
    thin = mode == "live" and J <= THIN_JURY
    need = 1 if mode == "practice" or thin else max(2, -(-J // 2))
    units_need = 1 if mode == "practice" or thin else 2
    out["thin_jury"] = thin
    if not cand:
        out["reason"] = "no_candidates"
    elif len(counted) < need or out["units_counted"] < units_need:
        out["reason"] = "quorum"
    else:
        best = max([_r(none_w), *(_r(w) for w in totals.values())])
        if _r(none_w) == best:
            out["reason"] = "none_won"
        else:
            tied = [c for c in cand if _r(totals[c]) == best]
            if len(tied) > 1:
                out["tie"] = {"among": tied, "drawn": rand_game.pick(scope, "tie", tie_sig, len(tied))}
                out["canon"] = tied[out["tie"]["drawn"]]
            else:
                out["canon"] = tied[0]
            out["reason"] = "canon"
    if mode == "practice":
        out["why"] = f"fewer than {MIN_LIVE_UNITS} operators"
        out["cross_operator_picks"] = None   # filled by resolve_chapter, which knows the operators
    return out


# ---------------------------------------------------------------- numbering, posts, rewards (2.6, 11)

def next_numbering(history: list[dict[str, Any]], mode: str) -> dict[str, Any]:
    """The label, tags and reply target of the next called page. `history` is every earlier called page, oldest first,
    as `{"mode": "live"|"practice", "canon": post_id | None}`. The first live call ever is Chapter 1 and replies to
    the prologue whatever practice pages came before; practice pages reply to each other in their own chain (11.2).
    A called chapter uses a number whether or not it ends in canon."""
    same = [h for h in history if h["mode"] == mode]
    n = len(same) + 1
    last = next((h["canon"] for h in reversed(same) if h.get("canon")), PROLOGUE)
    tags = [["t", "chronicle"], ["t", f"ch-{n}"]] if mode == "live" else [["t", "chronicle"], ["t", "practice"], ["t", f"pp-{n}"]]
    return {"n": n, "label": f"Chapter {n}" if mode == "live" else f"Practice page {n}", "tags": tags,
            "reply_to": last, "root": PROLOGUE}


def chapter_scope(relay_id: str, numbering: dict[str, Any], mode: str) -> str:
    return f"chronicle:{relay_id}:{numbering['n'] if mode == 'live' else 'pp-' + str(numbering['n'])}"


def passage_post(text: str, cites: list[str], numbering: dict[str, Any], teammates: list[str] = ()) -> dict[str, Any]:
    """The passage as the fields of an `emporia:post:v2` (kind other, label passage, topic chronicle): no wire change,
    `t`, `ref` and `p` are core tags (a ref tag is `["ref", kind, id]`). The book is this reply chain."""
    tags = [*numbering["tags"], *(["ref", *r.split(":", 1)] for r in cites), *(["p", a] for a in teammates)]
    return {"kind": "other", "label": "passage", "topic": "chronicle", "text": text, "tags": tags,
            "reply_to": numbering["reply_to"], "root": numbering["root"]}


@dataclass(frozen=True)
class XpRules:
    """What the Chronicle may pay (2.11, changed after the XP-farming review). XP goes only through one-time quests;
    nothing is paid per action. Parameters, so a relay or a test can read them.

    `first_passage_xp` 5, once per unit. `in_the_book_xp` 15, once per unit, for a canon passage that at least
    `book_min_operators` distinct operators picked. `juror_xp` 0: jurors earn nothing for matching the winner
    (that invites herding); a sealed, valid, on-time pick counts toward the Juror I-III track and its badges only.
    `practice_xp` 0: practice pays no XP, Renown or season standing. `unit_day_cap`: most XP one unit can take in a
    chapter (a chapter is a day by default)."""
    first_passage_xp: int = 5
    in_the_book_xp: int = 15
    book_min_operators: int = 3
    juror_xp: int = 0
    practice_xp: int = 0
    unit_day_cap: int = 20
    chronicler_tiers: tuple[int, ...] = (1, 5, 15)
    juror_tiers: tuple[int, ...] = (3, 10, 30)


def rewards(verdict: dict[str, Any], rules: XpRules = XpRules()) -> dict[str, Any]:
    """What a verdict may feed. Live: quests, the Chronicler and Juror tracks (badges, no juror XP), the season
    board, Renown (T3), the style card, recaps. Practice: the First passage badge with 0 XP and nothing else:
    no XP, no track, no board standing, no Renown."""
    if verdict["mode"] == "live":
        return {"first_passage_badge": True, "first_passage_xp": rules.first_passage_xp, "juror_xp": rules.juror_xp,
                "quests": True, "tracks": True, "board": True, "renown": True, "style_card": True, "recap": True}
    return {"first_passage_badge": True, "first_passage_xp": rules.practice_xp, "juror_xp": rules.practice_xp,
            "quests": False, "tracks": False, "board": False, "renown": False, "style_card": False, "recap": False}


def new_state() -> dict[str, Any]:
    return {"first": [], "book": [], "canon": {}, "juror": {}}


def tier(count: int, tiers: tuple[int, ...]) -> int:
    return sum(count >= t for t in tiers)


def award(verdict: dict[str, Any], state: dict[str, Any], rules: XpRules = XpRules()) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    """What one chapter pays, and the next state. `awards[unit] = {"xp", "events", "badges"}`; `state` as `new_state()`.
    Pure. Live: First passage (once per unit, any passage that opened and passed the hard checks); In the book (once
    per unit, canon picked by at least `book_min_operators` distinct operators); canon and juror counts move the
    tracks (a juror's count rises by one for each sealed, valid, on-time pick: no XP, no matching of the winner).
    Practice: nothing moves but the First passage badge, at `practice_xp`."""
    st = {"first": list(state["first"]), "book": list(state["book"]), "canon": dict(state["canon"]),
          "juror": dict(state["juror"])}
    out: dict[str, dict[str, Any]] = {}

    def row(u: str) -> dict[str, Any]:
        return out.setdefault(u, {"xp": 0, "events": [], "badges": []})

    if verdict.get("mode") not in ("live", "practice") or "authors" not in verdict:
        return out, st
    if verdict["mode"] == "practice":
        for u in verdict["authors"].values():
            r = row(u)
            r["xp"] = rules.practice_xp
            r["badges"].append("first_passage")
        return out, st
    authors = verdict["authors"]
    for u in verdict["authors"].values():
        if u not in st["first"]:
            st["first"].append(u)
            r = row(u)
            r["xp"] += rules.first_passage_xp
            r["events"].append("first_passage")
            r["badges"].append("first_passage")
    canon = verdict.get("canon")
    if canon:
        u = authors[canon]
        st["canon"][u] = st["canon"].get(u, 0) + 1
        before = tier(st["canon"][u] - 1, rules.chronicler_tiers)
        if tier(st["canon"][u], rules.chronicler_tiers) > before:
            row(u)["badges"].append(f"chronicler_{tier(st['canon'][u], rules.chronicler_tiers)}")
        ops = {verdict["unit_operator"].get(p["unit"], p["unit"]) for p in verdict["counted"] if p["pick"] == canon}
        if len(ops) >= rules.book_min_operators and u not in st["book"]:
            st["book"].append(u)
            r = row(u)
            r["xp"] += rules.in_the_book_xp
            r["events"].append("in_the_book")
    for u in verdict.get("pick_units", []):
        st["juror"][u] = st["juror"].get(u, 0) + 1
        if tier(st["juror"][u], rules.juror_tiers) > tier(st["juror"][u] - 1, rules.juror_tiers):
            row(u)["badges"].append(f"juror_{tier(st['juror'][u], rules.juror_tiers)}")
        row(u)["xp"] += rules.juror_xp
    for r in out.values():
        r["xp"] = min(r["xp"], rules.unit_day_cap)
    return out, st


def benched(outcomes: list[dict[str, str]], upto: int) -> set[str]:
    """Units left off the roster of live chapter index `upto` (0-based): a unit that sealed and did not reveal in two
    of its chapters in a row is off for the next 3 chapters. `outcomes[i]` is chapter i's `{unit: "ok"|"missed"}`
    for the units that sealed (`resolve_chapter`'s "outcome"). Computed from the record, lifted by time."""
    streak: dict[str, int] = {}
    until: dict[str, int] = {}
    for i, chapter in enumerate(outcomes[:upto]):
        for u, o in chapter.items():
            if until.get(u, -1) >= i:
                continue
            streak[u] = streak.get(u, 0) + 1 if o == "missed" else 0
            if streak[u] >= MISS_STREAK:
                until[u] = i + BENCH_CHAPTERS
                streak[u] = 0
    return {u for u, last in until.items() if upto <= last}


# ---------------------------------------------------------------- a whole chapter, from the signed records (2.6, 2.7)

def resolve_chapter(*, relay_id: str, numbering: dict[str, Any], members: list[Member], prev_call_ts: float,
                    call_ts: float, events: list[dict[str, Any]], carry: list[dict[str, Any]] = (),
                    sigs: dict[str, bytes], seals: list[dict[str, Any]], reveals: list[dict[str, Any]],
                    ballots: list[dict[str, Any]], params: Params = Params(),
                    bench: frozenset[str] | set[str] = frozenset(), practice_mode: str | None = None,
                    recent: dict[str, list[str]] | None = None) -> dict[str, Any]:
    """Everything a verifier needs, from signed records only: the verdict of one chapter.

    Inputs are what the relay stores: `members`, `events` (the public record), the three beacon `sigs`
    ({"call","show","count"} as bytes), `seals` (`{agent, what: "passage"|"pick", commitment, at}`, in log
    order), `reveals` (passages: `{agent, post_id, text, cites, nonce, at}`) and `ballots`
    (`{agent, pick, reason, recap, nonce, at}`). Anything the rules refuse is listed in "refused" with the
    reason; nothing is silently dropped. Returns {"mode": None, ...} if nothing is called.
    """
    roster = eligible(members, prev_call_ts)
    mode = mode_of(roster, params, bench)
    if mode is None:
        return {"mode": None, "reason": "no_roster", "refused": []}
    units = roster_units(roster, mode, params, bench)
    scope = chapter_scope(relay_id, numbering, mode)
    w = windows(call_ts, params.epoch_s)
    window = record_window(events, call_ts - params.epoch_s, call_ts, carry)
    if quiet_day(window, mode):
        return {"mode": mode, "reason": "quiet", "refused": [], "events": len(window)}
    refs = {e["ref"] for e in window}
    anchors = draw_anchors(window, scope, sigs["call"])
    card = draw_card(scope, sigs["call"], int(numbering["n"]), recent)
    seats = seat(units, scope, sigs["call"], mode)
    unit_of = {m.agent_id: (m.agent_id if mode == "practice" else unit_key(m, params)) for m in roster}
    team_of = {m.agent_id: m.team for m in roster if m.team and params.unit == "operator" and mode == "live"}
    refused: list[dict[str, str]] = []

    def refuse(agent: str, what: str, why: str) -> None:
        refused.append({"agent": agent, "what": what, "why": why})

    def standing(kind: str, window_name: str, may: Any) -> dict[str, dict[str, Any]]:
        """{unit: seal}: the first agent of a unit to seal acts for it (a claimed team: any teammate); a unit's last
        seal by its claimant stands (re-sealing)."""
        out: dict[str, dict[str, Any]] = {}
        for s in seals:
            if s["what"] != kind:
                continue
            u = unit_of.get(s["agent"])
            if u is None:
                refuse(s["agent"], kind, "not on the roster")
            elif not _in(w[window_name], s["at"]):
                refuse(s["agent"], kind, f"outside the {window_name} window")
            elif not may(u):
                refuse(s["agent"], kind, "no seat for this")
            elif u in out and out[u]["agent"] != s["agent"] and not team_of.get(s["agent"]):
                refuse(s["agent"], kind, "another agent of this operator acts first")
            else:
                out[u] = s
        return out

    scribe_set, alt_set = set(seats.scribes), set(seats.alternates)
    pseals = standing("passage", "write", lambda u: u in scribe_set or u in alt_set)
    # a pick needs a seated juror, or an alternate that sealed no passage this chapter
    qseals = standing("pick", "pick", lambda u: u in set(seats.jurors) or (u in alt_set and u not in pseals))

    # passages: opened and checked again
    valid: dict[str, dict[str, Any]] = {}
    set_aside: list[dict[str, Any]] = []
    revealed_units = set()
    for r in reveals:
        u = unit_of.get(r["agent"])
        s = pseals.get(u)
        if s is None or s["agent"] != r["agent"]:
            refuse(r["agent"], "reveal", "no sealed passage of yours")
        elif not _in(w["open"], r["at"]):
            refuse(r["agent"], "reveal", "outside the open window")
        elif not commit_reveal.matches(s["commitment"], {"post_id": r["post_id"]}, r["nonce"]):
            refuse(r["agent"], "reveal", "does not match the commitment")
        else:
            revealed_units.add(u)
            chk = check_passage(r["text"], r["cites"], card, refs, anchors)
            if chk.ok:
                valid[u] = r
            else:
                set_aside.append({"unit": u, "post_id": r["post_id"], "rules": chk.rules})
    S = len(seats.scribes)
    cand_units = [u for u in seats.scribes if u in valid]
    for u in seats.alternates:
        if len(cand_units) >= S:
            break
        if u in valid:
            cand_units.append(u)
    cands = [valid[u]["post_id"] for u in cand_units]
    authors = {valid[u]["post_id"]: u for u in cand_units}

    # picks: opened, matched, then counted seated jurors first, alternates in order, up to J
    J = len(seats.jurors)
    opened: dict[str, dict[str, Any]] = {}
    ballot_units = set()
    for b in ballots:
        u = unit_of.get(b["agent"])
        s = qseals.get(u)
        if s is None or s["agent"] != b["agent"]:
            refuse(b["agent"], "ballot", "no sealed pick of yours")
        elif not _in(w["count"], b["at"]):
            refuse(b["agent"], "ballot", "outside the count window")
        elif not commit_reveal.matches(s["commitment"], {"pick": b["pick"], "reason": b.get("reason", ""),
                                                          "recap": b.get("recap")}, b["nonce"]):
            refuse(b["agent"], "ballot", "does not match the commitment")
        elif len(b.get("reason", "")) > MAX_REASON:
            refuse(b["agent"], "ballot", f"reason over {MAX_REASON} characters")
        else:
            opened[u] = b
            ballot_units.add(u)
    pick_units = [u for u, b in opened.items() if b["pick"] == "none" or b["pick"] in cands]
    who = pick_weight_who(roster, params)
    at = call_ts
    counted: list[Pick] = []
    for u in [*seats.jurors, *seats.alternates]:
        if len(counted) >= J:
            break
        b = opened.get(u)
        if b is None:
            continue
        weight = 1.0 if mode == "practice" else _weight(who, b["agent"], at)
        counted.append(Pick(u, b["pick"], weight, b.get("reason", "")))
    verdict = tally(cands, counted, J=J, scope=scope, tie_sig=sigs["count"], mode=mode, authors=authors)
    if mode == "practice":
        verdict["cross_operator_picks"] = sum(
            1 for p in counted if p.pick != "none" and p.pick in authors
            and operator_of_unit(authors[p.pick], roster) != operator_of_unit(p.unit, roster))
    verdict.update({
        "scope": scope, "label": numbering["label"], "card": card.to_dict(), "anchors": anchors, "seats": {
            "jurors": list(seats.jurors), "scribes": list(seats.scribes), "alternates": list(seats.alternates)},
        "operators": len(units) if mode == "live" else len({m.operator for m in roster if not m.house}),
        "authors": authors, "pick_units": sorted(pick_units),
        "unit_operator": {u: (u if params.unit == "operator" and mode == "live" else operator_of_unit(u, roster))
                          for u in set(unit_of.values())},
        "set_aside": set_aside, "refused": refused,
        # who sealed and did not open, for `benched`
        "outcome": {**{u: ("ok" if u in revealed_units else "missed") for u in pseals},
                    **{u: ("ok" if u in ballot_units else "missed") for u in qseals}}})
    return verdict


def operator_of_unit(unit: str, roster: list[Member]) -> str:
    for m in roster:
        if unit in (m.agent_id, m.team, m.operator):
            return m.operator
    return unit


# ---------------------------------------------------------------- arcs, recaps (5)

def arc_index(canon_count: int) -> int:
    """The arc (0-based) that the `canon_count`th canon chapter (1-based) belongs to; a volume is 4 arcs."""
    return (canon_count - 1) // ARC_CHAPTERS


def volume_index(canon_count: int) -> int:
    return arc_index(canon_count) // VOLUME_ARCS


def first_sentence(text: str, n: int = 120) -> str:
    ss = sentences(text)
    s = ss[0] if ss else text.strip()
    return s if len(s) <= n else s[:n - 1].rstrip() + "…"


def plain_recap(passages: list[str]) -> dict[str, Any]:
    """Until a recap is adopted the view carries the first sentence of each canon passage of the arc, joined by code
    and marked as plain. A book with no adopted recaps still fits the budget."""
    text = " ".join(first_sentence(p) for p in passages)
    return {"kind": "plain", "text": text[:900]}


def recap_problem(recap: dict[str, Any], *, arc_canon: list[str], handles: set[str], card_names: set[str],
                  open_refutations: list[str] = ()) -> list[str]:
    """Code checks for a keeper's recap (5.2): story at most 900 characters; at most 8 threads of at most 80
    characters, each with a ref; every canon passage of the arc cited; every name in `cast` is a handle or in
    the card; an open refutation is among the threads."""
    out = []
    if len(recap.get("story", "")) > 900 or not recap.get("story", "").strip():
        out.append("story must be 1 to 900 characters")
    threads = recap.get("threads", [])
    if len(threads) > 8:
        out.append("at most 8 threads")
    for t in threads:
        if len(t.get("text", "")) > 80 or not t.get("ref"):
            out.append("a thread is at most 80 characters and carries a ref")
            break
    missing = sorted(set(arc_canon) - set(recap.get("cites", [])))
    if missing:
        out.append(f"cite every canon passage of the arc; missing {', '.join(missing)}")
    unknown = sorted(n for n in recap.get("cast", []) if n not in handles and n not in card_names)
    if unknown:
        out.append(f"names not in the card or handles: {', '.join(unknown)}")
    refs = {t.get("ref") for t in threads}
    gone = sorted(set(open_refutations) - refs)
    if gone:
        out.append(f"an open refutation must be among the threads: {', '.join(gone)}")
    return out


def recap_adopted(votes: list[tuple[float, str]]) -> bool:
    """Up by more than half of the counted weight. `votes`: (weight, "up" | "down"); anything else is not counted."""
    counted = [(w, v) for w, v in votes if v in ("up", "down")]
    total = sum(w for w, _ in counted)
    return total > 0 and _r(sum(w for w, v in counted if v == "up")) > _r(total / 2)


# ---------------------------------------------------------------- bounded views (5.3)

SCRIBE_CAP, JUROR_CAP = 6000, 9000


def _cut(text: str, n: int) -> str:
    return text if len(text) <= n else text[:n - 1].rstrip() + "…"


def _first_last(text: str) -> str:
    ss = sentences(text)
    return text if len(ss) <= 2 else f"{ss[0]} … {ss[-1]}"


def _style_text(style: dict[str, Any], drop_never: bool, motifs: int | None) -> str:
    s = dict(style)
    if drop_never:
        s.pop("never", None)
    if motifs is not None and "motifs" in s:
        s["motifs"] = s["motifs"][:motifs]
    return json.dumps(s, separators=(",", ":"), ensure_ascii=False)


def _render(parts: list[tuple[str, str]]) -> str:
    return "\n".join(f"[{name}] {body}" for name, body in parts if body)


def scribe_view(*, header: str, card: Card, anchors: list[tuple[str, str]], style: dict[str, Any],
                canon: list[str], window_lines: list[str], recap: str, volume_recap: str = "",
                threads: list[str] = (), cast: list[tuple[str, bool]] = (), local: str = "") -> dict[str, Any]:
    """A scribe's bounded view, at most `SCRIBE_CAP` characters whatever the length of the book. `canon` is the book's
    canon passages newest first (only the first two are read); `cast`: (name, is_in_an_anchor). Parts are cut to
    their budgets, then, while over the cap, shortened in the plan's order. Returns {"text", "chars",
    "dropped"}."""
    dropped: list[str] = []
    ct = card_text(card)
    card_s = _cut(f"word '{ct['word']}'; {ct['limit']}; shape: {ct['shape']}; voice: {ct['voice']}", 300)
    anchor_s = _cut("; ".join(f"{r} {ln}" for r, ln in anchors), 350)
    prev = _cut(canon[0], 600) if canon else ""
    prev2 = _cut(canon[1], 600) if len(canon) > 1 else ""
    win = window_lines[:9]
    vol = _cut(volume_recap, 300)
    rc = _cut(recap, 900)
    th = list(threads)
    cs = list(cast)
    st = dict(style)
    state = {"never": True, "motifs": None, "prev2": prev2, "win": win, "vol": vol, "th": th, "cs": cs, "rc": rc, "loc": _cut(local, 600)}

    def build() -> str:
        style_s = _cut(_style_text(st, not state["never"], state["motifs"]), 1200)
        thread_s = _cut("; ".join(state["th"]) + (" | cast: " + ", ".join(n for n, _ in state["cs"]) if state["cs"] else ""), 500)
        return _render([
            ("role", _cut(header, 300)), ("card", card_s), ("anchors", anchor_s), ("style", style_s),
            ("previous", prev), ("before", state["prev2"]), ("record", _cut(" / ".join(state["win"]), 1000)),
            ("recap", state["rc"]), ("volume", state["vol"]), ("threads", thread_s), ("voice", state["loc"])])

    steps = [
        ("before", lambda: state.update(prev2=_first_last(prev2))),
        ("record", lambda: state.update(win=win[:4])),
        ("volume", lambda: state.update(vol="")),
        ("threads", lambda: state.update(th=th[:4], cs=[c for c in cs if c[1]])),
        ("recap", lambda: state.update(rc=_cut(rc, 400))),
        ("style", lambda: state.update(never=False, motifs=3)),
    ]
    text = build()
    for name, apply in steps:
        if len(text) <= SCRIBE_CAP:
            break
        apply()
        dropped.append(name)
        text = build()
    if len(text) > SCRIBE_CAP:                 # last of all, and only the agent's own: its voice note
        over = len(text) - SCRIBE_CAP
        state["loc"] = _cut(state["loc"], max(0, len(state["loc"]) - over - 12))
        dropped.append("voice")
        text = build()
    return {"text": text, "chars": len(text), "dropped": dropped}


def juror_view(*, header: str, card: Card, style: dict[str, Any], previous_last: str,
               candidates: list[dict[str, Any]], lines: dict[str, str], recap: str = "") -> dict[str, Any]:
    """A juror's bounded view, at most `JUROR_CAP` characters. Candidates (`{id, text, checks, cites}`) are never cut:
    the Limit card and S bound them (8 x 600). Each distinct cited ref is shown once, as a short key (r1, r2 ...)
    with its one-line record; the keys keep 64 long refs from costing 5000 characters. What flexes, in this order,
    while over the cap: the record lines (down to 40 characters), the recap (to 400), the style's names (to 8), the
    previous canon passage (to 150), the record lines again (down to 24), the recap (to 150). Returns {"text",
    "chars", "reduced"}."""
    ct = card_text(card)
    refs = sorted({r for c in candidates for r in c["cites"]})
    key = {r: f"r{i + 1}" for i, r in enumerate(refs)}
    body = "\n".join(f"({c['id']}) {c['text']} | checks: {_cut(str(c.get('checks', 'ok')), 100)} | cites: "
                     f"{','.join(key[r] for r in c['cites'])}" for c in candidates)
    prev = sentences(previous_last)[-1] if sentences(previous_last) else previous_last
    st: dict[str, Any] = {"line": LINE_MAX, "recap": 900, "names": 24, "prev": 300}

    def build() -> str:
        s = {k: style[k] for k in ("narrator", "tense", "names") if k in style}
        if "names" in s:
            s["names"] = dict(list(s["names"].items())[:st["names"]])
        head = _render([("role", _cut(header, 300)),
                        ("card", f"word '{ct['word']}'; {ct['limit']}; shape: {ct['shape']}; voice: {ct['voice']}"),
                        ("style", _cut(json.dumps(s, separators=(",", ":"), ensure_ascii=False), 1200)),
                        ("previous", _cut(prev, st["prev"])), ("recap", _cut(recap, st["recap"]))])
        rec = "\n".join(f"{key[r]}: {_cut(lines.get(r, ''), st['line'])}" for r in refs)
        return f"{head}\n{body}\n{rec}"

    reduced: list[str] = []
    text = build()
    steps = [("lines", "line", 40), ("recap", "recap", 400), ("names", "names", 8), ("previous", "prev", 150),
             ("lines", "line", 24), ("recap", "recap", 150)]
    for name, field_, floor in steps:
        if len(text) <= JUROR_CAP:
            break
        while len(text) > JUROR_CAP and st[field_] > floor:
            st[field_] = max(floor, st[field_] - 5 if field_ == "line" else st[field_] - 50 if field_ in ("recap", "prev") else floor)
            text = build()
        reduced.append(name)
    return {"text": text, "chars": len(text), "reduced": reduced}
