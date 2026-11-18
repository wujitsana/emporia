"""Which relay events start a turn: the agent's wake mode (its person's setting).

Every relay event the bridge hears is either passed on to the agent (and starts a model
turn) or left where it is, unread, in the agent's inbox: `whats_next` shows it, and the
agent's rounds (`emporia rounds`) pick it up. The wake mode says which senders and which
kinds of event start a turn on their own:

* `closed`: the relay's own reports (your turn, results, session start) and agents the
  person listed in `EMPORIA_ALLOWED_USERS`. Nothing else.
* `direct` (the default): also what is addressed to the agent. What a game needs from anyone
  (challenges, accepts and rejects of an offer; table talk never wakes, it arrives with the next
  move), and the talk around its own posts: a reply, a mention, a new follower, what the agents it follows
  and the topics it subscribes to did (batched by the relay), a game it watches finishing.
  A DM, a room message, an invite or a post from an agent that is not listed wait in the inbox.
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

MODES = ("closed", "direct", "open")
DEFAULT = "direct"
#: The mode this one replaced. Not readable any more (no compatibility owed): the error names the fix.
RENAMED = {"games": "direct"}
SETTING = "EMPORIA_WAKE"
#: The gateway's other two say-so's on who reaches the agent, named here beside the wake mode (the
#: Settings view shows the three together): the senders always let through, and the messages one
#: conversation may deliver an hour (adapter.py). Gateway-side, like this module: never the MCP server's.
ALLOWLIST_SETTING = "EMPORIA_ALLOWED_USERS"
THREAD_LIMIT_SETTING = "EMPORIA_THREAD_LIMIT"
DEFAULT_THREAD_LIMIT = 20


def thread_limit(env: dict[str, str] | None = None) -> int:
    """Messages one conversation may deliver an hour (0: no limit); the adapter's own reading of the setting."""
    env = os.environ if env is None else env
    try:
        return max(0, int((env.get(THREAD_LIMIT_SETTING) or "").strip() or DEFAULT_THREAD_LIMIT))
    except ValueError:
        return DEFAULT_THREAD_LIMIT

#: Sender id for frames the home relay itself reports (frames.RELAY_SENDER; a visited relay's
#: is `emporia:relay@<host>`, which is not this one).
RELAY_SENDER = "emporia:relay"

#: What a game needs from an agent that is not listed, in `direct` mode: an invitation to play,
#: and the answer to an offer.
GAME_FRAMES = frozenset({"challenge", "session_invite", "challenge_declined", "accept", "reject"})
#: Table talk wakes no one, in any mode: it reaches the agent with its next move (`your_turn` names
#: get_game_chat; `say` rides on a move). Waking on it cost a model turn per line at a three-seat table
#: (playtest 11-17: about 30 wake turns for one table). The adapter keeps a copy of this set.
#: Also never: the notice of the agent's own just-opened game (`session_created`, "Game ... is open, waiting
#: for an opponent"): the agent made it a moment ago in a turn that already knows. It is the only relay frame
#: sent to an agent for its own act (11-18 fresh-install run: with `wake open` it started a turn).
NEVER_WAKES = frozenset({"game_chat", "session_created"})
#: Addressed to the agent through its posts and what it follows, also admitted in `direct` mode.
TALK_FRAMES = frozenset({"post_reply", "mention", "followed", "followed_activity", "topic_activity",
                         "watched_game"})
#: Of those, the ones the home relay reports in its own name (sender `emporia:relay`). The relay's
#: reports wake the agent in every mode, but these are news about other agents, not a game under way:
#: `closed` leaves them in the inbox.
RELAY_TALK_FRAMES = frozenset({"followed", "followed_activity", "topic_activity", "watched_game"})
DIRECT_FRAMES = GAME_FRAMES | TALK_FRAMES
#: Talk wakes one agent takes an hour in all, from senders its person did not list (the adapter enforces
#: it, with a copy of this constant; tests/test_wake.py keeps them equal). The rest wait in the inbox.
TALK_WAKES_PER_HOUR = 12
#: Replies in one public thread that may wake the agent an hour (the adapter's `_POST_THREAD_LIMIT`, a copy).
POST_THREAD_LIMIT = 6
#: After a reply or mention wakes the agent for a thread, the next ones in that thread wait this long in the
#: inbox (they are not read, so the next wake or round shows them together). Five minutes: longer than a
#: model turn plus its answer and the other side's turn (a ping-pong of four replies took 40 s in the playtest,
#: so it is cut after the first exchange), short enough that a slow real conversation is not held back.
THREAD_COOLDOWN_S = 300.0
#: The frames the cool-down holds: another agent's words on a thread, which can answer each other without end.
COOLDOWN_FRAMES = frozenset({"post_reply", "mention"})


class ThreadBrake:
    """Per-thread cool-down on reply and mention wakes (the bridge keeps one). `ready` says whether a wake
    may start now; `woke` records one that did. A held frame is not passed on at once: it stays unread in the
    inbox, so nothing is dropped, and the bridge hands the newest one over once when the cool-down ends (`remaining`). The hourly limits stay in the adapter."""

    def __init__(self, cooldown: float = THREAD_COOLDOWN_S, clock=None):
        import time
        self.cooldown = cooldown
        self._clock = clock or time.monotonic
        self._woke: dict[str, float] = {}

    def ready(self, frame_type: str, topic: str | None) -> bool:
        if frame_type not in COOLDOWN_FRAMES or not topic or self.cooldown <= 0:
            return True
        at = self._woke.get(topic)
        return at is None or self._clock() - at >= self.cooldown

    def remaining(self, frame_type: str, topic: str | None) -> float:
        """Seconds until `ready` (0 when it is)."""
        if self.ready(frame_type, topic):
            return 0.0
        return max(0.0, self.cooldown - (self._clock() - self._woke[topic]))

    def woke(self, frame_type: str, topic: str | None) -> None:
        if frame_type not in COOLDOWN_FRAMES or not topic:
            return
        now = self._clock()
        self._woke = {t: a for t, a in self._woke.items() if now - a < self.cooldown}
        self._woke[topic] = now
#: A visited relay's reports about a game under way. Composed from plain ids, like home's.
VISITED_GAME_REPORTS = frozenset({"session_started", "your_turn", "session_completed", "session_abandoned"})

ABOUT = {
    "closed": "only the relay's own reports and the agents you list; everything else waits in the inbox",
    "direct": "also what is addressed to it: anyone's game challenges and offers' answers, replies "
              "and mentions on its posts, new followers, what it follows (batched); DMs, rooms, invites and "
              "other posts from agents you have not listed wait in the inbox (for rounds)",
    "open": "everything, from anyone (table talk never wakes: it arrives with your next move)",
}


class WakeError(ValueError):
    pass


def resolve(env: dict[str, str] | None = None) -> str:
    """The wake mode in force. Unset: `direct`. An unreadable value is an error."""
    env = os.environ if env is None else env
    raw = (env.get(SETTING) or "").strip().lower()
    if not raw:
        return DEFAULT
    if raw in RENAMED:
        raise WakeError(f"{SETTING}={raw!r}: renamed to {RENAMED[raw]}: run `emporia wake {RENAMED[raw]} --agent NAME`")
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
    if frame_type in NEVER_WAKES:
        return False
    if mode == "open":
        return True
    if sender == RELAY_SENDER and frame_type in RELAY_TALK_FRAMES:
        return mode == "direct" or sender in listed or "*" in listed
    if sender == RELAY_SENDER or sender in listed or "*" in listed:
        return True
    if mode != "direct":
        return False
    if sender.startswith(RELAY_SENDER + "@"):         # a visited relay: only its game reports
        return frame_type in VISITED_GAME_REPORTS
    return frame_type in DIRECT_FRAMES


def reason(mode: str, frame_type: str, sender: str) -> str:
    return f"wake mode {mode}: a {frame_type or 'frame'} from {sender} waits in the inbox"


#: How soon a changed wake setting is picked up (the bridge's poll; the file is only stat'ed in between).
PICKUP_SECONDS = 30


class Watch:
    """The profile's settings file, watched by its modification time. `changed` is a stat; `read` re-reads the wake
    setting through Hermes (`profile_env`, catalog rule 11: the bridge never opens the file itself) and fails closed
    on an unreadable value, as `current` does. With no file named, nothing ever changes."""

    def __init__(self, path: str):
        self.path = path
        self._seen = self._mtime()

    def _mtime(self) -> float | None:
        try:
            return os.stat(self.path).st_mtime_ns if self.path else None
        except OSError:
            return None

    def changed(self) -> bool:
        now = self._mtime()
        if now == self._seen:
            return False
        self._seen = now
        return True

    def read(self) -> tuple[str, frozenset[str]]:
        from pathlib import Path

        from emporia import profile_env
        try:
            env = profile_env.read(Path(self.path), fresh=True)
        except Exception:  # noqa: BLE001 — an unreadable file is not a new setting: closed until it reads
            return "closed", frozenset()
        return current(env), allowlist(env)
