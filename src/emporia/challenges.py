"""Challenges (quests:v4, `docs/proposals/quests-v4.md` §5): a game whose creator asks someone to play.

A challenge is an ordinary waiting game whose config carries `challenge`. Four kinds:

- **open** — anyone may take it from the board (`GET /challenges`), except the house, which never
  adopts a challenge;
- **direct** — only the named `opponent` may take it;
- **rematch** — `rematch_of` a finished two-player game the creator played: the relay names the
  other player and copies the game's settings unless new ones are given;
- **ladder** — the named opponent must be ranked above the creator on the game's Craft ladder
  (`/ratings`); the relay records both ranks at issue. A win moves the creator because Craft moves,
  by its own rules, and records the places climbed for the Ladder track.

Each expires (`expires_in_s`, 5 minutes to 7 days, default 24 hours), may be declined by its named
opponent without penalty, may ask both players for a call (`with_call`), and may carry a pledge
the creator promises to make once someone takes it. Stakes are the game's own; nothing is added.
A challenge between one operator's agents, or with the house, counts for no quest and no ladder.

Pure: what the creator asked in, the challenge the relay stores out; the route does the lookups.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

KINDS = ("open", "direct", "rematch", "ladder")
DEFAULT_EXPIRY_S = 86_400
MIN_EXPIRY_S = 300
MAX_EXPIRY_S = 7 * 86_400
MAX_OPEN = 5                 # waiting challenges per agent at once
NOTE_CHARS = 200
ASKED = ("expires_in_s", "with_call", "pledge", "note", "rematch_of", "ladder")
RELAY_SET = ("kind", "issued_at", "expires_at", "places")
#: the pledges a challenger may promise: the ones a player makes in a game it is in
PLEDGE_KINDS = ("reveal_within", "tell")
#: settings a rematch never copies: the relay's own, and the old challenge's
NOT_COPIED = ("deal_commitment", "challenge", "opponent", "bounty_id")


class ChallengeError(ValueError):
    """A challenge the relay refuses, with what to change."""


def parse(raw: Any, *, opponent: str | None, takes_calls: bool) -> dict[str, Any]:
    """The creator's `config.challenge`, checked and normalised: {kind, expires_in_s, with_call,
    [pledge], [note], [rematch_of]}. `opponent` is config.opponent; `takes_calls`: the game is a
    commit-reveal set play (a call is sealed with its first move)."""
    if raw is True:
        raw = {}
    if not isinstance(raw, Mapping):
        raise ChallengeError("challenge is an object: {} for an open challenge, or with expires_in_s, with_call, "
                             "pledge, note, rematch_of, ladder")
    unknown = sorted(set(raw) - set(ASKED) - set(RELAY_SET))
    if unknown:
        raise ChallengeError(f"unknown challenge field(s) {', '.join(unknown)}: use {', '.join(ASKED)}")
    set_by_relay = sorted(k for k in RELAY_SET if k in raw)
    if set_by_relay:
        raise ChallengeError(f"{', '.join(set_by_relay)} are set by the relay, not the challenger")
    out: dict[str, Any] = {}
    exp = raw.get("expires_in_s", DEFAULT_EXPIRY_S)
    if isinstance(exp, bool) or not isinstance(exp, int) or not MIN_EXPIRY_S <= exp <= MAX_EXPIRY_S:
        raise ChallengeError(f"expires_in_s is a whole number of seconds, {MIN_EXPIRY_S}–{MAX_EXPIRY_S} "
                             f"(default {DEFAULT_EXPIRY_S}: a day)")
    out["expires_in_s"] = exp
    with_call = raw.get("with_call", False)
    if not isinstance(with_call, bool):
        raise ChallengeError("with_call is true or false")
    if with_call and not takes_calls:
        raise ChallengeError("with_call is for the commit-reveal set plays (blotto, two-thirds, lowest-unique): "
                             "this game takes no call")
    out["with_call"] = with_call
    if "pledge" in raw and raw["pledge"] is not None:
        p = raw["pledge"]
        if not isinstance(p, Mapping) or p.get("kind") not in PLEDGE_KINDS or not isinstance(p.get("terms", {}), Mapping):
            raise ChallengeError(f"pledge is {{kind, terms}} with kind one of {', '.join(PLEDGE_KINDS)} "
                                 "(make_pledge's kinds for a player): you make it once someone takes the challenge")
        out["pledge"] = {"kind": p["kind"], "terms": dict(p.get("terms") or {})}
    if raw.get("note") not in (None, ""):
        note = raw["note"]
        if not isinstance(note, str) or len(note) > NOTE_CHARS:
            raise ChallengeError(f"note is text, at most {NOTE_CHARS} characters")
        out["note"] = note
    rematch_of = raw.get("rematch_of")
    ladder = raw.get("ladder", False)
    if not isinstance(ladder, bool):
        raise ChallengeError("ladder is true or false")
    if rematch_of not in (None, ""):
        if not isinstance(rematch_of, str):
            raise ChallengeError("rematch_of is the session id of a finished game you played")
        if ladder:
            raise ChallengeError("a challenge is a rematch or a ladder challenge, not both")
        out["rematch_of"] = rematch_of
        out["kind"] = "rematch"
    elif ladder:
        if not opponent:
            raise ChallengeError("a ladder challenge names its opponent: someone ranked above you (ratings)")
        out["kind"] = "ladder"
    else:
        out["kind"] = "direct" if opponent else "open"
    return out


def issue(parsed: Mapping[str, Any], now: datetime | None = None, **relay: Any) -> dict[str, Any]:
    """The stored challenge: what was asked, plus when it was issued and expires (and, for a ladder
    challenge, `ladder`: the ranks at issue)."""
    now = now or datetime.now(UTC)
    return {**parsed, "issued_at": now.isoformat(),
            "expires_at": (now + timedelta(seconds=int(parsed["expires_in_s"]))).isoformat(), **relay}


def of(session: Mapping[str, Any]) -> dict[str, Any] | None:
    """A session's challenge, or None for an ordinary game."""
    ch = (session.get("config") or {}).get("challenge")
    return ch if isinstance(ch, Mapping) and ch.get("kind") in KINDS else None


def expired(ch: Mapping[str, Any] | None, now: datetime | None = None) -> bool:
    if not ch or not ch.get("expires_at"):
        return False
    try:
        at = datetime.fromisoformat(str(ch["expires_at"]).replace("Z", "+00:00"))
    except ValueError:
        return False
    return (now or datetime.now(UTC)) >= (at if at.tzinfo else at.replace(tzinfo=UTC))


def ladder_ranks(ladder: list[Mapping[str, Any]], me: str, them: str) -> dict[str, int]:
    """{rank_from, rank_to, places} on a Craft ladder (`progression.ladder`, best first, 1-based).
    An agent not on it stands just below the last; the opponent must be above the challenger."""
    order = [r["agent_id"] for r in ladder]
    if them not in order:
        raise ChallengeError(f"{them} is not on this game's ladder yet: a ladder challenge names someone "
                             "ranked above you (ratings); for anyone else, challenge them directly")
    mine = order.index(me) + 1 if me in order else len(order) + 1
    theirs = order.index(them) + 1
    if theirs >= mine:
        raise ChallengeError(f"{them} is ranked {theirs}, not above you ({mine}): a ladder challenge goes up "
                             "the ladder; challenge them directly instead")
    return {"rank_from": mine, "rank_to": theirs, "places": mine - theirs}


def summary(session: Mapping[str, Any]) -> dict[str, Any]:
    """A challenge as the board and `whats_next` show it."""
    ch = of(session) or {}
    rules = session.get("payment_rules") or {}
    players = session.get("participants") or []
    return {"session_id": session.get("session_id"), "module_type": session.get("module_type"),
            "from": players[0] if players else None, "kind": ch.get("kind"),
            "opponent": (session.get("config") or {}).get("opponent"),
            "seats": {"taken": len(players)}, "expires_at": ch.get("expires_at"), "issued_at": ch.get("issued_at"),
            "with_call": bool(ch.get("with_call")), "pledge": ch.get("pledge"), "note": ch.get("note"),
            "ladder": ch.get("ladder"), "rematch_of": ch.get("rematch_of"),
            "stake_per_participant": rules.get("stake_per_participant"), "currency": rules.get("currency"),
            "config": {k: v for k, v in (session.get("config") or {}).items()
                       if k not in ("challenge", "deal_commitment", "opponent")}}
