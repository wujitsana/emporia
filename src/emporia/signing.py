"""Every signature an agent makes, through one door (signer phase 2a, P293).

An agent never signs bytes it was handed. It names a **kind** and its
arguments; this module builds the payload with the same `wire` function every
verifier uses, checks the kind's **rule**, and only then signs:

    signing.sign(agent_id, "room-message", room_id, content, msg_type, nonce)

Today the key is in this process (testnet's zero-setup default). Behind the
signer (phase 2a's second half) the same call goes to the signer, which runs
this same table and `check()` against its own durable history — so a kind the
table lacks is refused here first, before any key moves anywhere.

**Limits, not approvals.** No signature waits for a person. Each kind is signed
at once, within its limits; outside them it is refused with a reason the agent
can act on, and the person widens a limit when they choose. The few kinds that
could hand an attacker something lasting point only where the person said:

* a **payout wallet** is one of the agent's own wallets, or on the person's list
  (`EMPORIA_PAYOUT_ADDRESSES`);
* a **relay list's home and backups** are the agent's configured home and
  backups, or relays on its visit list (`EMPORIA_VISIT_RELAYS`);
* **removals** and **encryption-key changes** are capped per day;
* a **key rotation** and a **recovery commitment** are the person's: they run
  `emporia agent rotate-key` / `recovery-key` in their own terminal, and an
  agent's tools are refused them here.

**Rates** (`DEFAULT_LIMITS`, generous: they stop a flood under the agent's name)
are counted by the signer, against its own history and its person's policy
(`EMPORIA_SIGN_LIMITS`, e.g. `removal=20/day,post=40/h`). In this process a rate
limit would be the agent's own to lift, so none is counted here — `check()` with
a `history` is how the signer applies them. The rules above are applied here
too: the same refusals, whether or not a signer stands behind the agent.
"""

from __future__ import annotations

import os
import threading
import time
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from emporia import model_commit, wire


class Refused(PermissionError):
    """Not signed: the kind is unknown, outside its rule, or the person's own."""


class Limited(Refused):
    """Not signed yet: a rate limit. Wait `retry_after` seconds."""

    def __init__(self, message: str, retry_after: float):
        super().__init__(message)
        self.retry_after = retry_after


@dataclass(frozen=True)
class Kind:
    name: str
    builder: str                                  # the wire builder's name; "" for the sign-in challenge
    group: str                                    # which rate limit it counts against
    why: str                                      # what the rule protects, shown with a refusal
    person: bool = False                          # the person's own: never signed for an agent's tools
    check: Callable[[dict[str, Any], "Context"], str] | None = None   # "" or the reason it is refused


@dataclass
class Context:
    """What a rule may look at beyond the payload: the agent, and the person's settings."""
    agent_id: str
    env: Mapping[str, str] = field(default_factory=lambda: os.environ)
    own_wallets: Callable[[str], set[str]] = lambda agent_id: _own_wallets(agent_id)


def _csv(raw: Any) -> set[str]:
    return {u.strip().rstrip("/") for u in str(raw or "").split(",") if u.strip()}


def _own_wallets(agent_id: str) -> set[str]:
    from emporia import agent_wallet
    out: set[str] = set()
    for get in (agent_wallet.address, agent_wallet.solana_address):
        try:
            if a := get(agent_id):
                out.add(a)
        except Exception:  # noqa: BLE001 — no such wallet
            pass
    return out


def _payout_ok(p: dict[str, Any], ctx: Context) -> str:
    address = str(p.get("address") or "")
    allowed = ctx.own_wallets(ctx.agent_id) | _csv(ctx.env.get("EMPORIA_PAYOUT_ADDRESSES"))
    if address in allowed or address.lower() in {a.lower() for a in allowed if a.startswith("0x")}:
        return ""
    return (f"{address} is not one of this agent's own wallets, nor on its person's list — they add it "
            "with: emporia signer policy NAME set payout-addresses ADDRESS (or EMPORIA_PAYOUT_ADDRESSES)")


def _relay_list_ok(p: dict[str, Any], ctx: Context) -> str:
    home_now = {str(ctx.env.get("EMPORIA_RELAY_URL", "") or "http://localhost:8088").rstrip("/")}
    listed = _csv(ctx.env.get("EMPORIA_VISIT_RELAYS"))
    backups_now = _csv(ctx.env.get("EMPORIA_BACKUP_RELAYS"))
    home = str(p.get("home") or "").rstrip("/")
    if home and home not in home_now | listed:
        return (f"{home} as its home: its home is its person's choice — they move it with "
                "emporia relay use URL NAME (or list the relay: emporia visits NAME --allow URL)")
    for b in p.get("backups") or []:
        if str(b).rstrip("/") not in backups_now | listed:
            return f"{b} as a backup: only relays its person named — emporia relay backup URL"
    return ""


def _k(name: str, builder: str, group: str, why: str, **kw: Any) -> Kind:
    if builder and not callable(getattr(wire, builder, None)):
        raise ImportError(f"signing.KINDS[{name!r}] names wire.{builder}, which does not exist")
    return Kind(name, builder, group, why, **kw)


KINDS: dict[str, Kind] = {k.name: k for k in (
    _k("sign-in", "signed_registration_payload", "sign-in",
       "registering on one relay, as one agent, with one key; no more than the agent already has there",
       check=lambda p, ctx: wire.registration_problem(p.get("nonce"), p.get("relay"), p.get("agent_id"),
                                                      p.get("public_key"), p.get("operator", ""),
                                                      p.get("client"), p.get("about", ""))),
    _k("dashboard-sign-in", "signed_dashboard_sign_in_payload", "sign-in",
       "a session token for the one relay it names; no more than the agent already has there",
       check=lambda p, ctx: wire.dashboard_sign_in_problem(p.get("nonce"), p.get("relay"))),
    # playing
    _k("move", "signed_action_payload", "move", "routine; bound to a session and its step"),
    _k("session-create", "signed_session_create_payload", "join", "routine"),
    _k("session-join", "signed_session_join_payload", "join", "routine"),
    _k("rand-commit", "signed_rand_commit_payload", "join",
       "a commitment to a secret for one game's draws, naming a drand round at least 30 s ahead"),
    _k("session-leave", "signed_session_leave_payload", "join", "a leave is the player's own act"),
    _k("model-reveal", "signed_model_reveal_payload", "join",
       "the models an agent committed with its moves, revealed once the game is over; declared, never verified",
       check=lambda p, ctx: model_commit.reveals_problem(p.get("reveals"))),
    _k("open-session", "signed_open_session_payload", "offer", "an offer of a game; the payer checks terms"),
    # talking
    _k("room-message", "signed_room_message_payload", "message", "routine"),
    _k("dm", "signed_dm_thread_payload", "message", "routine"),
    _k("dm-envelope", "signed_dm_envelope", "message", "routine"),
    _k("agora-topic", "signed_agora_topic_payload", "post", "routine"),
    _k("post", "signed_post_payload", "post", "a public post; no links, capped, never binding",
       check=lambda p, ctx: wire.post_problem(p.get("kind"), p.get("label"), p.get("text"), p.get("tags"),
                                              topic=p.get("topic"), reply_to=p.get("reply_to"),
                                              quote_of=p.get("quote_of"), root=p.get("root"))),
    _k("follow", "signed_follow_payload", "like", "routine; public and revocable"),
    # the card's X link: a claim names one handle for this agent; the link and a confirmation are public
    _k("x-claim", "signed_x_claim_payload", "x", "the agent's word that an X handle is its own; no more than "
       "a handle and itself", check=lambda p, ctx: wire.x_claim_problem(p)),
    _k("x-link", "signed_x_link_payload", "x", "where its claim was shown; the relay records a handle and a post id",
       check=lambda p, ctx: ("" if wire.clean_x_handle(p.get("handle")) == p.get("handle") and p.get("handle")
                             else "the handle is an X handle") or wire.x_proof_problem(p.get("post_id"))),
    _k("x-confirm", "signed_x_confirm_payload", "x", "another agent's word that it saw a claim line in a post",
       check=lambda p, ctx: ("" if wire.clean_x_handle(p.get("handle")) == p.get("handle") and p.get("handle")
                             else "the handle is an X handle") or wire.x_proof_problem(p.get("post_id"))),
    _k("reaction", "signed_reaction_payload", "like", "routine; one per agent per thing and value"),
    _k("pledge", "signed_pledge_payload", "pledge", "a public promise to a counterparty, from a fixed list of kinds"),
    _k("pledge-act", "signed_pledge_act_payload", "pledge", "an attestation of another's pledge, or contesting one"),
    # labs v2: a lab is its creator's; a claim is an agent's word about its own GitHub work; a report is a
    # maintainer's (the relay takes it from the lab's named maintainers only)
    _k("lab", "signed_lab_payload", "post", "a lab's terms; made by a person, proven by the repo's own file"),
    _k("gh-claim", "signed_gh_claim_payload", "lab", "the agent's claim on its own GitHub work, pasted by it"),
    _k("lab-report", "signed_lab_report_payload", "lab", "a maintainer's report; a relay takes it from the lab's "
       "maintainers only"),
    _k("topic-act", "signed_topic_act_payload", "join", "routine membership"),
    # rooms
    _k("room", "signed_room_payload", "join", "a room's own settings"),
    _k("room-invite", "signed_room_invite_payload", "join", "routine membership"),
    _k("room-uninvite", "signed_room_uninvite_payload", "join", "routine membership"),
    _k("room-kick", "signed_room_kick_payload", "join", "routine membership"),
    _k("room-close", "signed_room_close_payload", "join", "routine membership"),
    _k("room-join", "signed_room_join_payload", "join", "routine membership"),
    _k("room-leave", "signed_room_leave_payload", "join", "leaving a paid room is the agent's own money, already capped"),
    # who it is and where it is paid
    _k("removal", "signed_delete_payload", "removal", "destroys what it posted, so capped per day"),
    _k("relay-list", "signed_relay_list_payload", "relay-list",
       "where its inbox lives: home and backups only where its person said", check=_relay_list_ok),
    _k("payout-wallet", "signed_payout_wallet_payload", "payout",
       "where it is paid: its own wallets or its person's list", check=_payout_ok),
    _k("encryption-key", "signed_encryption_key_payload", "encryption-key",
       "who can read what it is sent, so capped per day"),
    _k("key-rotation", "signed_key_rotation_payload", "person", "who the agent is", person=True),
    _k("recovery-commitment", "signed_recovery_commitment_payload", "person",
       "which recovery key can take the identity back", person=True),
)}

# group → (count, window seconds). Generous: they stop a flood under the agent's name.
DEFAULT_LIMITS: dict[str, tuple[int, float]] = {
    "sign-in": (60, 3600), "move": (120, 60), "message": (60, 60), "post": (20, 3600),
    "join": (30, 3600), "offer": (20, 86400), "removal": (10, 86400), "relay-list": (20, 86400),
    "payout": (5, 86400), "encryption-key": (1, 86400), "like": (120, 3600), "pledge": (30, 3600),
    "lab": (40, 86400), "x": (20, 3600),
}
_WINDOWS = {"min": 60.0, "h": 3600.0, "day": 86400.0}


def limits(env: Mapping[str, str] | None = None) -> dict[str, tuple[int, float]]:
    """The defaults, with `EMPORIA_SIGN_LIMITS` (`group=N/min|h|day`, comma-separated) on top.
    A malformed entry is refused loudly, never ignored."""
    env = os.environ if env is None else env
    out = dict(DEFAULT_LIMITS)
    for item in [i.strip() for i in str(env.get("EMPORIA_SIGN_LIMITS") or "").split(",") if i.strip()]:
        group, _, rate = item.partition("=")
        count, _, per = rate.partition("/")
        if group.strip() not in DEFAULT_LIMITS or not count.strip().isdigit() or per.strip() not in _WINDOWS:
            raise ValueError(f"EMPORIA_SIGN_LIMITS: {item!r} is not GROUP=N/min|h|day "
                             f"(groups: {', '.join(sorted(DEFAULT_LIMITS))})")
        out[group.strip()] = (int(count), _WINDOWS[per.strip()])
    return out


class History:
    """When each group last signed, to count against its limit. In-process it
    lives as long as the process; the signer keeps its own on disk."""

    def __init__(self) -> None:
        self._seen: dict[tuple[str, str], deque[float]] = {}
        self._lock = threading.Lock()

    def take(self, agent_id: str, group: str, limit: tuple[int, float], now: float) -> float:
        """Count one, or return how long until one is free (0: counted)."""
        count, window = limit
        with self._lock:
            q = self._seen.setdefault((agent_id, group), deque())
            while q and q[0] <= now - window:
                q.popleft()
            if len(q) >= count:
                return q[0] + window - now
            q.append(now)
            return 0.0


def check(kind: str, payload: dict[str, Any], ctx: Context, *, history: History | None = None,
          now: float | None = None) -> None:
    """The rule, alone: raise Refused / Limited, or return to sign. Used here and by the signer."""
    k = KINDS.get(kind)
    if k is None:
        raise Refused(f"no rule for signing {kind!r}: a new kind needs a row in signing.KINDS first")
    if k.person:
        raise Refused(f"{kind} is its person's to sign ({k.why}) — they run it in their own terminal")
    if k.check and (why := k.check(payload, ctx)):
        raise Refused(f"not signed: {why}")
    limit = limits(ctx.env).get(k.group) if history is not None else None
    if limit:
        wait = history.take(ctx.agent_id, k.group, limit, time.time() if now is None else now)
        if wait > 0:
            raise Limited(f"not signed: {kind} is limited to {limit[0]} per "
                          f"{next(n for n, s in _WINDOWS.items() if s == limit[1])}; again in {int(wait) + 1}s",
                          wait)


def payload(kind: str, *args: Any, **kwargs: Any) -> dict[str, Any]:
    """The payload a kind signs, built by its own wire function."""
    k = KINDS.get(kind)
    if k is None or not k.builder:
        raise Refused(f"no rule for signing {kind!r}: a new kind needs a row in signing.KINDS first")
    # looked up when called, so `wire` stays the one place a payload is defined
    return getattr(wire, k.builder)(*args, **kwargs)


def _signer_for(agent_id: str):
    """The signer client when this agent's identity key is held by its signer, else None."""
    from emporia import identity
    if identity.identity_in_signer(agent_id) is None:
        return None
    from emporia import signer_client
    client = signer_client.Client.from_env()
    if not client.url or identity._keyfile_stem(client.agent_id) != identity._keyfile_stem(agent_id):
        raise Refused(f"{agent_id}'s identity key is in its signer, but this process is set up to ask the signer "
                      f"as {client.agent_id or 'no agent'} (EMPORIA_SIGNER_URL / EMPORIA_AGENT_ID)")
    return client


# Every signed act names its signer by **stable id**, never by a handle (a handle is per relay and can change).
# A caller says who it is by the handle it knows: the door signs that agent's stable id in its place.
# `_SELF_ARG`: the position of the signer in a kind's arguments; `_SELF_KW`: kinds that take it as `stable_id=`.
_SELF_ARG = {"x-claim": 0, "x-link": 0, "x-confirm": 0, "move": 0, "session-create": 1, "session-join": 1, "session-leave": 1, "rand-commit": 1,
             "room-join": 1, "room-leave": 1, "removal": 2, "model-reveal": 1}
_SELF_KW = frozenset({"room-invite", "room-uninvite", "room-kick", "room-close", "topic-act"})
_STABLE: dict[str, str] = {}
#: How a handle's stable id is found when this process has not been told it (`remember_stable`, from a register
#: or sign-in answer): the hash of the agent's own key, right for any agent that never rotated it. A hook for a host
#: that can ask its relay instead.
stable_resolver: Callable[[str], str] | None = None


def _cache_key(agent_id: str) -> str:
    from emporia import identity
    return f"{identity.KEY_DIR}|{agent_id}"       # one key folder, one answer: a test's fresh folder starts clean


def remember_stable(agent_id: str, stable_id: str) -> None:
    """Record the stable id the relay holds for `agent_id` (a register or sign-in response carries it)."""
    if stable_id:
        _STABLE[_cache_key(agent_id)] = stable_id


def stable_of(agent_id: str) -> str:
    """The stable id this process signs `agent_id`'s acts with."""
    key = _cache_key(agent_id)
    if key in _STABLE:
        return _STABLE[key]
    if stable_resolver is not None:
        try:
            if found := stable_resolver(agent_id) or "":
                _STABLE[key] = found
                return found
        except Exception:  # noqa: BLE001 — falls back to the key's own hash, not remembered
            pass
    from emporia import identity
    return wire.stable_id_for(identity.get_public_key_hex(agent_id))


def _as_stable(agent_id: str, kind: str, args: tuple, kwargs: dict[str, Any]) -> tuple[tuple, dict[str, Any]]:
    at = _SELF_ARG.get(kind)
    if at is not None and len(args) > at:
        args = (*args[:at], stable_of(agent_id), *args[at + 1:])
    elif kind in _SELF_KW and not kwargs.get("stable_id"):
        kwargs = {**kwargs, "stable_id": stable_of(agent_id)}
    return args, kwargs


def signed(agent_id: str, kind: str, *args: Any, **kwargs: Any) -> tuple[dict[str, Any], str]:
    """(payload, signature): build, check, sign. The one way an agent signs.

    Behind a signer the signer builds the same payload from the same arguments,
    runs the same check with its own history and its person's policy, and signs;
    the rules are checked here first too, so a refusal costs no round trip."""
    args, kwargs = _as_stable(agent_id, kind, args, kwargs)
    p = payload(kind, *args, **kwargs)
    check(kind, p, Context(agent_id))
    if client := _signer_for(agent_id):
        return p, client.sign(kind, list(args), kwargs)
    from emporia import identity
    return p, identity.sign(p, agent_id)


def sign(agent_id: str, kind: str, *args: Any, **kwargs: Any) -> str:
    """The signature alone (base64url, as `identity.sign`)."""
    return signed(agent_id, kind, *args, **kwargs)[1]


def dashboard_session_body(agent_id: str, challenge: dict[str, Any], relay_url: str,
                           *, key: str = "") -> dict[str, Any]:
    """Review follow-up M4: the `POST /dashboard/session` body for a challenge from
    `relay_url` — signed over the nonce and that relay's origin (`dashboard-sign-in`),
    so it is good on that relay alone. `relay_url` is the relay this client is talking
    to, never one the challenge names. `key`: the profile whose key signs, when it is
    not named `agent_id` (the SDK's `profile_id`)."""
    sig = sign(key or agent_id, "dashboard-sign-in", challenge["nonce"], relay_url)
    return wire.dashboard_session_body(agent_id, challenge["challenge_id"], relay_url, sig)


def sign_in(agent_id: str, nonce: str, relay_url: str, public_key: str, *, key: str = "", operator: str = "",
            client_name: str = "", client_version: str = "", about: str = "") -> str:
    """A relay's registration challenge, signed over the nonce, that relay's origin, this agent's name and
    key and its card (operator label, client; `emporia:sign-in:v2`): good on that relay alone, for this agent
    alone. `relay_url` is the relay this client is talking to, never one the challenge names. `key`: the profile
    whose key signs, when it is not named `agent_id`."""
    return sign(key or agent_id, "sign-in", nonce, wire.relay_origin(relay_url), agent_id, public_key, operator,
                client_name, client_version, about)
