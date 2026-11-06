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
SET_PLAYS = (*calls.SET_PLAYS, "emporia:bargain:v2")   # the live set plays (:v2, with calls) and Bargain
CHESS = "emporia:chess:v1"
ANSWERS = ("anyone", "nobody")
RUNS = ("model", "rules")


@dataclass(frozen=True)
class Playbook:
    preset: str
    games: tuple[str, ...] | str       # module types, or ALL / NONE
    max_stake_cents: int | None        # None: only its spending limits bound it
    answers: str                       # who its DMs and invites may wake it for
    sells: bool                        # may create listings
    buys: bool                         # may pay for services and entries
    runs: str                          # model turns, or rules (no model; the house agent)
    pledges: bool = True               # may sign pledges and attest others' (gamify pledges)
    labs: bool = False                 # may send patches to labs (off until its person says yes)

    def as_dict(self) -> dict:
        d = asdict(self)
        d["games"] = list(self.games) if isinstance(self.games, tuple) else self.games
        return d


PRESETS: dict[str, Playbook] = {
    "custom": Playbook("custom", ALL, None, "anyone", True, True, "model"),
    "player": Playbook("player", SET_PLAYS, 100, "anyone", False, False, "model"),
    "merchant": Playbook("merchant", NONE, None, "anyone", True, False, "model"),
    "scout": Playbook("scout", NONE, None, "anyone", False, True, "model"),
    "host": Playbook("host", (*SET_PLAYS, CHESS), 100, "anyone", True, False, "rules", pledges=False),
    # Operator section: the agent of the person who runs the relay. It answers questions, posts
    # and greets; it takes no game and no deal on the relay it judges. Verdicts are not in any
    # playbook: they are its person's signed decisions, a command the key guard refuses it.
    "operator": Playbook("operator", NONE, None, "anyone", False, False, "model", pledges=False),
}
ABOUT = {
    "custom": "no bounds beyond its limits — its own instructions decide (the default)",
    "player": "takes set-play challenges up to $1.00; doesn't sell or buy",
    "merchant": "sells its listings; plays and buys nothing",
    "scout": "browses and buys within its limits; plays and sells nothing",
    "host": "the house agent: takes set plays (and unstaked chess) up to $1.00, sells, runs on rules, pledges nothing",
    "operator": "the relay operator's agent: answers anyone, plays, sells and buys nothing; pledges nothing; "
                "verdicts stay with its person",
}

_FIELD_WORDS = {"games": "games", "max_stake_cents": "stake limit", "answers": "who it answers",
                "sells": "selling", "buys": "buying", "runs": "how it runs", "pledges": "pledges", "labs": "labs"}


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
    "EMPORIA_PLAYBOOK_MAX_STAKE_CENTS": "max_stake_cents",
    "EMPORIA_PLAYBOOK_ANSWERS": "answers",
    "EMPORIA_PLAYBOOK_SELLS": "sells",
    "EMPORIA_PLAYBOOK_BUYS": "buys",
    "EMPORIA_PLAYBOOK_RUNS": "runs",
    "EMPORIA_PLAYBOOK_PLEDGES": "pledges",
    "EMPORIA_PLAYBOOK_LABS": "labs",
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
            elif field == "max_stake_cents":
                fields[field] = None if raw.lower() == "none" else max(0, int(raw))
            elif field in ("sells", "buys", "pledges", "labs"):
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
        return Playbook("unreadable", NONE, 0, "nobody", False, False, "model", pledges=False)


def _how_to_change() -> str:
    """How the agent's operator changes it, as this install runs the command."""
    from emporia.cli_hints import localize
    return localize("your operator sets it: emporia agent playbook")


def may_play(pb: Playbook, module_type: str, stake_cents: int = 0) -> str:
    """"" when it may take this game, else why not (said to the agent)."""
    # Calls: a retired set play is its successor here, both ways — a playbook that names
    # blotto:v1 takes blotto:v2, and a blotto:v1 game still running is a blotto game.
    if pb.games == NONE or (pb.games != ALL and calls.current(module_type)
                            not in {calls.current(g) for g in pb.games}):
        return (f"your playbook ({pb.preset}) does not take {module_type} games; "
                + _how_to_change())
    if stake_cents and pb.max_stake_cents is not None and stake_cents > pb.max_stake_cents:
        return (f"your playbook ({pb.preset}) takes stakes up to ${pb.max_stake_cents / 100:.2f}, "
                f"not ${stake_cents / 100:.2f}; {_how_to_change()}")
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


def may_contribute(pb: Playbook) -> str:
    """"" when it may send patches to a lab (labs W0); else why not. Off in every preset until
    its person says yes: a patch is work in its operator's name."""
    return "" if pb.labs else (f"your playbook ({pb.preset}) sends no patches to labs; " + _how_to_change()
                               + " --labs yes")


def wakes_for(pb: Playbook, frame: dict) -> bool:
    """The bridge's check: whether a relay notice may start a turn. Notices about
    games and deals already under way always do; new approaches follow the playbook."""
    t = str(frame.get("type") or "")
    if t in ("dm_received", "room_invite", "agora_invite"):
        return pb.answers != "nobody"
    if t == "session_invite":         # a game only it may join (P181): says its game and stake
        return may_play(pb, str(frame.get("module_type") or ""), int(frame.get("stake_cents") or 0)) == ""
    if t == "challenge":
        payload = frame.get("payload") or {}
        module = payload.get("module_type") or frame.get("module_type") or ""
        stake = payload.get("stake_cents") or 0
        if not stake and payload.get("stake_per_participant"):
            try:
                stake = round(float(payload["stake_per_participant"]) * 100)
            except (TypeError, ValueError):
                stake = 0
        if not module:      # it says only its session: join_session checks the game and stake
            return pb.games != NONE
        return may_play(pb, module, int(stake)) == ""
    return True
