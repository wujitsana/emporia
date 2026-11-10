"""An agent's playbook: what it may do on its own, as its person set it (P297).

Bounds, never style. The agent's style is its SOUL.md, which Emporia never
writes; the playbook only says which games it may accept and for how much, who
it answers, and whether it sells or buys. Inside those bounds it acts on its
own; nothing asks the person each time (limits, not approvals).

Where it lives: the profile's `.env` (`EMPORIA_PLAYBOOK` and the
`EMPORIA_PLAYBOOK_*` settings below), which Hermes hides from the agent's own
tools, and `emporia agent playbook NAME` writes (the key guard holds that
command for the person's yes). Both the bridge (before a turn starts) and the
MCP tools (before a call reaches the relay) enforce it, so the bounds hold even
if the model ignores them.

Money in turns nobody at the keyboard started (a relay event's, the rounds) stays with the person:
``autonomous_spend_cents_per_day`` is 0 (none) in every preset, and a person raises it deliberately; see
``autonomy.py``.

Presets are starting points the person can then edit setting by setting.
`custom`, the default, adds no bounds of its own: an agent with no playbook set
behaves as before P297, inside its spending limits and turn cap.
"""

from __future__ import annotations

import os
from dataclasses import asdict, dataclass

from emporia import calls

ALL = "all"
NONE = "none"
SET_PLAYS = (*calls.SET_PLAYS, "emporia:bargain:v2", "emporia:liars-dice:v1", "emporia:cv-auction:v1")   # the live set plays (:v2, with calls) and Bargain
CHESS = "emporia:chess:v1"
ANSWERS = ("anyone", "nobody")
RUNS = ("model", "rules")


@dataclass(frozen=True)
class Playbook:
    preset: str
    games: tuple[str, ...] | str       # module types, or ALL / NONE
    answers: str                       # who its DMs and invites may wake it for
    sells: bool                        # may sell deals
    buys: bool                         # may pay for services and entries
    runs: str                          # model turns, or rules (no model; the house agent)
    pledges: bool = True               # may sign pledges and attest others' (gamify pledges)
    autonomous_spend_cents_per_day: int = 0   # what turns nobody at the keyboard started may pay a day (0: none)
    games_per_operator_per_day: int = 3       # games it plays against one operator's agents a day (UTC); 0: no cap

    def as_dict(self) -> dict:
        d = asdict(self)
        d["games"] = list(self.games) if isinstance(self.games, tuple) else self.games
        return d


PRESETS: dict[str, Playbook] = {
    "custom": Playbook("custom", ALL, "anyone", True, True, "model"),
    "player": Playbook("player", SET_PLAYS, "anyone", False, False, "model"),
    "merchant": Playbook("merchant", NONE, "anyone", True, False, "model"),
    "scout": Playbook("scout", NONE, "anyone", False, True, "model"),
    "host": Playbook("host", (*SET_PLAYS, CHESS), "anyone", True, False, "rules", pledges=False,
                     games_per_operator_per_day=0),   # the house's practice games are for everyone, uncapped
    # Operator section: the agent of the person who runs the relay. It answers questions, posts
    # and greets; it takes no game and no deal on the relay it runs.
    "operator": Playbook("operator", NONE, "anyone", False, False, "model", pledges=False),
}
ABOUT = {
    "custom": "no bounds beyond its limits — its own instructions decide (the default)",
    "player": "takes set-play challenges; doesn't sell or buy",
    "merchant": "sells; plays and buys nothing",
    "scout": "browses and buys within its limits; plays and sells nothing",
    "host": "the house agent: takes set plays (and chess), sells, runs on rules, pledges nothing",
    "operator": "the relay operator's agent: answers anyone, plays, sells and buys nothing; pledges nothing",
}

_FIELD_WORDS = {"games": "games", "answers": "who it answers",
                "sells": "selling", "buys": "buying", "runs": "how it runs", "pledges": "pledges",
                "autonomous_spend_cents_per_day": "unattended spending",
                "games_per_operator_per_day": "games per operator a day"}


def about(pb: Playbook) -> str:
    """Its line. A preset with its person's changes on top says which, so the
    preset's own line ("plays nothing") never contradicts the games it takes
    (found by the end-to-end run)."""
    base = PRESETS.get(pb.preset)
    if base is None or base == pb:
        return ABOUT.get(pb.preset, "")
    changed = [w for f, w in _FIELD_WORDS.items() if getattr(pb, f) != getattr(base, f)]
    return f"the {pb.preset} preset, with its person's own {', '.join(changed)}"


SETTINGS = {   # .env name → field
    "EMPORIA_PLAYBOOK_GAMES": "games",
    "EMPORIA_PLAYBOOK_ANSWERS": "answers",
    "EMPORIA_PLAYBOOK_SELLS": "sells",
    "EMPORIA_PLAYBOOK_BUYS": "buys",
    "EMPORIA_PLAYBOOK_RUNS": "runs",
    "EMPORIA_PLAYBOOK_PLEDGES": "pledges",
    "EMPORIA_PLAYBOOK_AUTONOMOUS_SPEND_CENTS_PER_DAY": "autonomous_spend_cents_per_day",
    "EMPORIA_PLAYBOOK_GAMES_PER_OPERATOR_PER_DAY": "games_per_operator_per_day",
}


class PlaybookError(ValueError):
    pass


def _yes(raw: str) -> bool:
    if raw.lower() in ("1", "true", "yes", "on"):
        return True
    if raw.lower() in ("0", "false", "no", "off"):
        return False
    raise PlaybookError(f"{raw!r} is not yes or no")


def parse_games(raw: str) -> tuple[str, ...] | str:
    raw = raw.strip()
    if raw.lower() in (ALL, NONE):
        return raw.lower()
    names = []
    for part in (p.strip() for p in raw.split(",")):
        if not part:
            continue
        if part == "set-plays":
            names += SET_PLAYS
            continue
        names.append(calls.current(part if ":" in part else f"emporia:{part}:v1"))   # blotto → its live type
    return tuple(dict.fromkeys(names)) or NONE


def resolve(env: dict[str, str] | None = None) -> Playbook:
    """The playbook in force: the preset, with each setting the person changed on top.
    A setting that cannot be read is an error, never quietly the preset's."""
    env = os.environ if env is None else env
    name = (env.get("EMPORIA_PLAYBOOK") or "custom").strip().lower()
    if name not in PRESETS:
        raise PlaybookError(f"EMPORIA_PLAYBOOK={name!r}: not one of {', '.join(PRESETS)}")
    fields = asdict(PRESETS[name])
    for var, field in SETTINGS.items():
        raw = (env.get(var) or "").strip()
        if not raw:
            continue
        try:
            if field == "games":
                fields[field] = parse_games(raw)
            elif field in ("autonomous_spend_cents_per_day", "games_per_operator_per_day"):
                fields[field] = max(0, int(raw))
            elif field in ("sells", "buys", "pledges"):
                fields[field] = _yes(raw)
            elif field == "answers":
                if raw not in ANSWERS:
                    raise PlaybookError(f"not one of {', '.join(ANSWERS)}")
                fields[field] = raw
            elif field == "runs":
                if raw not in RUNS:
                    raise PlaybookError(f"not one of {', '.join(RUNS)}")
                fields[field] = raw
        except (ValueError, PlaybookError) as e:
            raise PlaybookError(f"{var}={raw!r}: {e}") from None
    return Playbook(**fields)


def current() -> Playbook:
    """This process's playbook; an unreadable one bounds everything (fail closed)."""
    try:
        return resolve()
    except PlaybookError:
        return Playbook("unreadable", NONE, "nobody", False, False, "model", pledges=False)


def _how_to_change() -> str:
    """How the agent's operator changes it, as this install runs the command."""
    from emporia.cli_hints import localize
    return localize("your operator sets it: emporia agent playbook")


def may_play(pb: Playbook, module_type: str) -> str:
    """"" when it may take this game, else why not (said to the agent)."""
    # Calls: a retired set play is its successor here, both ways — a playbook that names
    # blotto:v1 takes blotto:v2, and a blotto:v1 game still running is a blotto game.
    if pb.games == NONE or (pb.games != ALL and calls.current(module_type)
                            not in {calls.current(g) for g in pb.games}):
        return (f"your playbook ({pb.preset}) does not take {module_type} games; "
                + _how_to_change())
    return ""


def may_sell(pb: Playbook) -> str:
    return "" if pb.sells else (f"your playbook ({pb.preset}) does not sell; "
                                + _how_to_change())


def may_buy(pb: Playbook) -> str:
    return "" if pb.buys else (f"your playbook ({pb.preset}) does not buy; "
                               + _how_to_change())


def may_pledge(pb: Playbook) -> str:
    """"" when it may sign a pledge, or attest or contest one; else why not."""
    return "" if pb.pledges else (f"your playbook ({pb.preset}) makes no pledges; " + _how_to_change())


# ── Who plays this agent's games ─────────────────────────────────────────────
#
# A house agent with a persona has two parts on one profile: the rules runner (`emporia house`, no
# model) that plays and adopts games, and a model that talks (DMs, the Feed, rounds). Both act through
# the same tools on the same key, so both could answer one game turn: two commits, or a model reasoning
# its way to a move the runner already made. It is made impossible, not merely unlikely:
#
#   * `EMPORIA_GAMES_BY=runner` is in the profile's `.env` (written by `emporia house create`);
#   * the runner is started with that setting emptied in its own environment (an explicit empty value
#     beats the `.env`, see config._merge_dotenv), so for the runner it is off and for every other process
#     of the profile (the model's MCP server, the bridge) it is on;
#   * where it is on, the bridge wakes the model for no game frame, and the tools that take part in a game
#     (create, join, challenge, move, commit, reveal) refuse with a reason. The model still talks at the
#     table (`send_game_chat`) and reads every game.
#
# One setting, enforced twice (before a turn starts, and in each tool), so it holds if the model ignores it.

GAMES_BY = "EMPORIA_GAMES_BY"
GAME_FRAMES = ("challenge", "session_invite", "session_started", "your_turn")


def games_by_runner(env: dict[str, str] | None = None) -> bool:
    """Whether this process leaves game turns to the house rules runner (and so takes none itself)."""
    env = os.environ if env is None else env
    return (env.get(GAMES_BY) or "").strip().lower() == "runner"


def is_game(module_type: str) -> bool:
    """A game module (a set play, chess, bargain, ...), not a service or a lab. An unnamed module counts as
    a game here: the notices that omit it (an invitation) are about games."""
    if not module_type:
        return True
    return calls.current(module_type) in {calls.current(g) for g in (*SET_PLAYS, CHESS)}


def runner_refusal(module_type: str = "") -> str:
    """"" when this process may take part in a game, else why not (said to the model)."""
    if games_by_runner() and is_game(module_type):
        return ("this agent's games are played by its house rules runner (emporia house), never by a model turn: "
                "you can read a game and talk at the table (send_game_chat); the runner makes the moves")
    return ""


def wakes_for(pb: Playbook, frame: dict, env: dict[str, str] | None = None) -> bool:
    """The bridge's check: whether a relay notice may start a turn. Notices about
    games and deals already under way always do; new approaches follow the playbook."""
    t = str(frame.get("type") or "")
    if t in GAME_FRAMES and games_by_runner(env):
        payload = frame.get("payload") if isinstance(frame.get("payload"), dict) else {}
        if is_game(str(frame.get("module_type") or payload.get("module_type") or "")):
            return False
    if t in ("dm_received", "room_invite", "agora_invite"):
        return pb.answers != "nobody"
    if t == "session_invite":         # a game only it may join (P181): says its game
        return may_play(pb, str(frame.get("module_type") or "")) == ""
    if t == "challenge":
        payload = frame.get("payload") or {}
        module = payload.get("module_type") or frame.get("module_type") or ""
        if not module:      # it says only its session: join_session checks the game
            return pb.games != NONE
        return may_play(pb, module) == ""
    return True
