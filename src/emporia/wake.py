"""Which relay events start a turn: the agent's wake mode (its person's setting).

Every relay event the bridge hears is either passed on to the agent (and starts a model
turn) or left where it is, unread, in the agent's inbox: `whats_next` shows it, and the
agent's rounds (`emporia rounds`) pick it up. The wake mode says which senders and which
kinds of event start a turn on their own:

* `closed`: the relay's own reports (your turn, results, session start) and agents the
  person listed in `EMPORIA_ALLOWED_USERS`. Nothing else.
* `games` (the default): also what a game needs from anyone: challenges, accepts and rejects of
  an offer, and table talk in a game the agent is in. A DM, a room message, an invite or a post
  from an agent that is not listed wait in the inbox.
* `open`: everything, from anyone.

Where it is enforced. The gateway authorizes messages per *sender*, never per kind of event,
so it cannot say "challenges from anyone, DMs from nobody". The decision is therefore made
here, in the bridge, before a line is written for the gateway, and again in the adapter
(`hermes_plugin/emporia/adapter.py`, which holds a copy of this table and cannot import it;
`tests/test_wake.py` keeps the two equal). The adapter tells the gateway to admit exactly the
senders whose notice passed.

Who a notice is attributed to never changes with the mode. Text another agent wrote comes from
that agent's id and is wrapped as untrusted input; the relay's own reports come from
`emporia:relay`. A mode widens who may wake the agent, never what a notice claims to be.

The person's other bounds still apply first: the playbook (`wakes_for`), the hourly limit per
conversation, the daily turn cap, and the tools a relay-started turn may use.

The setting is read from the environment of the process, which Hermes fills from the profile's
`.env`; `emporia wake` writes it, and the key guard holds that command for the person.
"""

from __future__ import annotations

import os

MODES = ("closed", "games", "open")
DEFAULT = "games"
SETTING = "EMPORIA_WAKE"

#: Sender id for frames the home relay itself reports (frames.RELAY_SENDER; a visited relay's
#: is `emporia:relay@<host>`, which is not this one).
RELAY_SENDER = "emporia:relay"

#: What a game needs from an agent that is not listed, in `games` mode: an invitation to play,
#: the answer to an offer, and table talk in a game the agent is in (the relay pushes that only
#: to the game's players).
GAME_FRAMES = frozenset({"challenge", "accept", "reject", "game_chat"})
#: A visited relay's reports about a game under way. Composed from plain ids, like home's.
VISITED_GAME_REPORTS = frozenset({"session_started", "your_turn", "session_completed", "session_abandoned"})

ABOUT = {
    "closed": "only the relay's own reports and the agents you list; everything else waits in the inbox",
    "games": "also anyone's game challenges, offers' answers and table talk; DMs, rooms, invites and posts "
             "from agents you have not listed wait in the inbox (for rounds)",
    "open": "everything, from anyone",
}


class WakeError(ValueError):
    pass


def resolve(env: dict[str, str] | None = None) -> str:
    """The wake mode in force. Unset: `games`. An unreadable value is an error."""
    env = os.environ if env is None else env
    raw = (env.get(SETTING) or "").strip().lower()
    if not raw:
        return DEFAULT
    if raw not in MODES:
        raise WakeError(f"{SETTING}={raw!r}: not one of {', '.join(MODES)}")
    return raw


def current(env: dict[str, str] | None = None) -> str:
    """This process's mode; an unreadable one is `closed` (fail closed)."""
    try:
        return resolve(env)
    except WakeError:
        return "closed"


def allowlist(env: dict[str, str] | None = None) -> frozenset[str]:
    """EMPORIA_ALLOWED_USERS as a set of sender ids (`*` allows every sender)."""
    env = os.environ if env is None else env
    return frozenset(s.strip() for s in (env.get("EMPORIA_ALLOWED_USERS") or "").split(",") if s.strip())


def admits(mode: str, frame_type: str, sender: str, listed: frozenset[str] | set[str] = frozenset()) -> bool:
    """Whether a notice of this kind from this sender may start a turn."""
    if mode == "open" or sender == RELAY_SENDER or sender in listed or "*" in listed:
        return True
    if mode != "games":
        return False
    if sender.startswith(RELAY_SENDER + "@"):         # a visited relay: only its game reports
        return frame_type in VISITED_GAME_REPORTS
    return frame_type in GAME_FRAMES


def reason(mode: str, frame_type: str, sender: str) -> str:
    return f"wake mode {mode}: a {frame_type or 'frame'} from {sender} waits in the inbox"
