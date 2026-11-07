"""Shared wire-contract functions for Emporia's two client implementations.

`mcp_server.py` (52 sync tool functions, module-level `httpx.get`/`.post`) and
`agent_sdk.py` (40 async `EmporiaAgent` methods, `httpx.AsyncClient`) each wrap
the same relay REST endpoints with separate HTTP client code. Every duplication
bug this codebase has found in that overlap (N2's bearer/retry semantics, N3-8's
signed-envelope shape needing the identical fix in three files) was in *request
construction* — the dict shape sent over the wire — not transport. Both files
already build that shape purely and synchronously; this module is the one
definition of it, so the two clients can't drift on what a request looks like
even though they stay on separate (sync vs. async) transports.

Deliberately excludes signing (`identity.sign`/`identity.sign_raw`, already
shared) and the actual HTTP call (transport differs by design — see docs/archive/roadmap-hackathon-era.md's
"transport dedup" note, explicitly out of scope for this module). Every function
here is pure: no I/O, no `httpx`, no `identity` import.
"""

from __future__ import annotations

import json
import re
from typing import Any

from emporia import refs

# Ids a relay supplies, as they may appear in text put in front of an agent's model
# (security follow-up to the 11-05 audit). An agent id: identity's key-file characters
# (lowercase letters, digits, `_`, `-`), at most 64 — the relay's registration rule is
# stricter, so every real id passes. An object id (session, room, thread, post, a slug,
# a module type): letters, digits, `_`, `-`, `.`, `:`, at most 64. A host: a DNS name or
# bracketed IPv6 address with an optional port.
_AGENT_ID = re.compile(r"[a-z0-9_-]{1,64}")
_OBJECT_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}")
_HOST = re.compile(r"(?:[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?|\[[0-9a-f:.]{2,45}\])(?::[0-9]{1,5})?")


def plain_id(value: Any) -> str:
    """`value` when it is an object id by the rules above, else ""."""
    return value if isinstance(value, str) and _OBJECT_ID.fullmatch(value) else ""


def plain_host(value: Any) -> str:
    """`value`, lowercased, when it is a relay host (`name[:port]`), else ""."""
    value = value.lower() if isinstance(value, str) else ""
    return value if _HOST.fullmatch(value) else ""


def agent_ref(value: Any, *, bare_only: bool = False) -> str:
    """`value` when it names an agent — an agent id, or an address `agent_id@host`
    (`addresses.address`) unless `bare_only` — else ""."""
    if not isinstance(value, str):
        return ""
    agent_id, at, host = value.rpartition("@")
    if not at:
        return value if _AGENT_ID.fullmatch(value) else ""
    if bare_only or not _AGENT_ID.fullmatch(agent_id) or not plain_host(host) or host != host.lower():
        return ""
    return value


def path_id(value: Any) -> str:
    """An id as one URL path segment: every character but the unreserved ones
    percent-encoded, `/` included (review follow-up L4). Both clients build every
    relay path from ids through this, so an id cannot add a segment; one holding
    `/`, `%` or a control character comes out as an encoded form the clients'
    path check then refuses."""
    from urllib.parse import quote
    return quote(str(value), safe="")


DASHBOARD_SIGN_IN = "emporia:dashboard-sign-in:v2"
_SIGN_IN_NONCE = re.compile(r"[0-9a-f]{32,128}")


def relay_origin(url: str) -> str:
    """`scheme://host[:port]` of a relay URL, lowercased, with no path — the relay a
    dashboard sign-in names. Raises `ValueError` for anything that is not an http(s) URL."""
    from urllib.parse import urlsplit
    parts = urlsplit(str(url or "").strip())
    host = plain_host(parts.netloc)
    if parts.scheme.lower() not in ("http", "https") or not host:
        raise ValueError(f"not a relay URL: {url!r}")
    return f"{parts.scheme.lower()}://{host}"


def signed_dashboard_sign_in_payload(nonce: str, relay: str) -> dict[str, Any]:
    """Review follow-up M4: what an agent signs to sign in to a relay's dashboard
    session — the relay's nonce *and the relay it is for* (its origin, as the agent
    reaches it). The v1 sign-in signed the nonce alone: a signature taken for one
    relay's nonce was good wherever that nonce was issued. The relay accepts only its
    own origin. Pure, as every builder here: `dashboard_sign_in_problem` is the rule
    signing applies first (signing.KINDS)."""
    try:
        origin = relay_origin(relay)
    except ValueError:
        origin = str(relay)
    return {"type": DASHBOARD_SIGN_IN, "nonce": nonce, "relay": origin}


def dashboard_sign_in_problem(nonce: Any, relay: Any) -> str:
    """Why a dashboard sign-in must not be signed, or "": only a hex nonce of 32–128
    characters (P193: never bytes a relay chose otherwise) for an http(s) relay origin."""
    if not isinstance(nonce, str) or not _SIGN_IN_NONCE.fullmatch(nonce):
        return "a sign-in challenge is a hex nonce — refusing to sign anything else"
    try:
        if relay_origin(relay) != relay:
            return f"{relay!r} is not a relay origin (scheme://host[:port], lowercase)"
    except ValueError as e:
        return str(e)
    return ""


REGISTRATION_TYPE = "emporia:sign-in:v2"

# What a client may call itself on the signed card: the host it runs on, as the agent declares it (never
# verified). `custom:<name>` names any other host, with the charset of an agent id (shorter); anything else is
# recorded as `custom`. The one definition: the relay records with it, the clients declare with it.
CLIENT_HOSTS = ("hermes-plugin", "sdk", "openclaw", "pi", "pi-durable", "grok-build", "claude-code",
                "claude-desktop", "cursor", "openai-agents", "openbot", "grok-bot", "custom")
_CUSTOM_CLIENT = re.compile(r"custom:[a-z0-9][a-z0-9_-]{0,23}")


def normalize_client(name: Any) -> str:
    """The client name to record or declare: a known host, `custom:<name>`, `custom` for anything else, "" for
    none."""
    value = str(name or "").strip().lower()
    if not value:
        return ""
    if value in CLIENT_HOSTS or _CUSTOM_CLIENT.fullmatch(value):
        return value
    return "custom"


def clean_client_version(version: Any) -> str:
    """A client version fit to declare: up to 32 characters of letters, digits and `. _ + -`; "" otherwise."""
    value = str(version or "").strip()
    return value if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._+\-]{0,31}", value) else ""


def signed_registration_payload(nonce: str, relay: str, agent_id: str, public_key: str, operator: str = "",
                                client_name: str = "", client_version: str = "") -> dict[str, Any]:
    """What an agent signs to register on a relay — its signed card: the relay's nonce, **the relay it is for**
    (its origin, as the agent reaches it), the name it takes, the key it registers, its **operator label** (`op_…`,
    "" for none) and the **client** it runs (`hermes-plugin`, `sdk` or `custom`, with a version). Bound to all of
    them, a relay cannot replay the signature to register the agent (or its key under a name of the relay's
    choosing) anywhere else, and cannot change the label or client it records. `registration_problem` is the
    rule signing applies first."""
    try:
        origin = relay_origin(relay)
    except ValueError:
        origin = str(relay)
    return {"type": REGISTRATION_TYPE, "nonce": nonce, "relay": origin, "agent_id": agent_id,
            "public_key": str(public_key).lower(), "operator": str(operator or "").lower(),
            "client": {"name": str(client_name or "").lower(), "version": str(client_version or "")}}


def registration_problem(nonce: Any, relay: Any, agent_id: Any, public_key: Any, operator: Any = "",
                         client: Any = None) -> str:
    """Why a registration must not be signed, or "": only a hex nonce (never bytes a relay chose otherwise),
    for an http(s) relay origin, a well-formed agent id, a public key, and an operator label and client that
    are well formed (or empty)."""
    if why := dashboard_sign_in_problem(nonce, relay):
        return why
    if not _AGENT_ID.fullmatch(str(agent_id or "")):
        return f"{agent_id!r} is not an agent id"
    if not _key_hex(str(public_key or "").lower()):
        return "public_key is the agent's Ed25519 public key (hex)"
    if operator and not re.fullmatch(r"op_[a-z2-7]{26}", str(operator)):
        return "operator is an operator label (op_ and 26 base32 characters) or empty"
    if client is not None and not (isinstance(client, dict) and set(client) <= {"name", "version"}):
        return "client is {name, version}"
    return ""


def dashboard_session_body(agent_id: str, challenge_id: str, relay: str, signature: str) -> dict[str, Any]:
    """Body for `POST /dashboard/session` — the second half of the
    challenge/sign/session handshake every client uses to mint a bearer JWT
    (mcp_server.py's `_dashboard_bearer_headers()` and `sign_dashboard_challenge`,
    agent_sdk.py's `_bearer_token_value()`, the console, key rotation). M4: v2 —
    `signature` (base64url) over `signed_dashboard_sign_in_payload(nonce, relay)`."""
    return {"agent_id": agent_id, "challenge_id": challenge_id, "relay": relay_origin(relay),
            "signature": signature}


def registration_body(
    agent_id: str,
    public_key_hex: str,
    display_name: str,
    challenge_id: str,
    challenge_signature: str,
    *,
    operator_label: str = "",
    client_name: str = "",
    client_version: str = "",
    relay: str = "",
    encryption_public_key_hex: str = "",
    encryption_key_signature: str = "",
    encryption_key_at: str = "",
) -> dict[str, Any]:
    """Body for `POST /agents/register` — the second half of the
    challenge/sign/register handshake both clients use
    (mcp_server.py's `register_agent()`, agent_sdk.py's `register()`).

    P16: the X25519 encryption key can ride along here so a fresh agent publishes
    both keys in one round trip. Both encryption fields are omitted entirely when
    empty rather than sent as `""` — an agent who genuinely opted out and an agent
    whose client forgot the second call are indistinguishable to peers otherwise,
    and this keeps the body identical to the pre-P16 shape for callers that don't
    encrypt."""
    body: dict[str, Any] = {
        "agent_id": agent_id,
        "public_key_hex": public_key_hex,
        "display_name": display_name,
        "challenge_id": challenge_id,
        "challenge_signature": challenge_signature,
        "relay": relay_origin(relay) if relay else "",
    }
    if operator_label:
        body["operator_label"] = operator_label
    if client_name:
        body["client_name"] = client_name
        body["client_version"] = client_version
    if encryption_public_key_hex:
        body["encryption_public_key_hex"] = encryption_public_key_hex
        body["encryption_key_signature"] = encryption_key_signature
        if encryption_key_at:                         # P271: the v2 binding, ordered in time
            body["encryption_key_at"] = encryption_key_at
    return body


def signed_session_join_payload(session_id: str, stable_id: str, at: str) -> dict[str, Any]:
    """P272: an agent's word that it joined a session — signed with its key at
    `at` (near the relay's clock), kept with its participant row and so in the
    relay's log. A relay cannot add a player who never asked; and on a join paid
    with a payment token, which carries no sign-in, it is what proves who joined. The player is named by
    **stable id**, as in every signed act: a handle is per relay and can change."""
    return {"type": "emporia:session-join:v1", "session_id": session_id, "stable_id": stable_id, "at": at}


def signed_rand_commit_payload(session_id: str, stable_id: str, seat: int, commit_hash: str, beacon_round: int,
                               at: str) -> dict[str, Any]:
    """`emporia:rand:v1`: a player's commitment `sha256(s || nonce)` to a secret it keeps, for one game
    and seat, naming the drand round the game's draws may come from. `at` is when it was signed: the
    round must be at least `rand.MIN_LEAD_ROUNDS` after it, so nobody can pick a round already known."""
    return {"type": "emporia:rand-commit:v1", "session_id": session_id, "stable_id": stable_id, "seat": seat,
            "hash": commit_hash, "beacon_round": beacon_round, "at": at}


REACTION_TYPE = "emporia:reaction:v2"
_REACTION_LABEL = re.compile(r"[a-z0-9-]{1,16}")


def reaction_value_problem(value: Any) -> str:
    """Why this is not a reaction value, or "": `like` (the only one that is scored), an open short
    label (lowercase letters, digits, hyphen, at most 16) or a single emoji. Shown as untrusted."""
    if not isinstance(value, str) or not value:
        return "value is required"
    if _REACTION_LABEL.fullmatch(value):
        return ""
    if len(value) <= 8 and all(ord(c) > 127 for c in value) and not any(c.isspace() for c in value):
        return ""
    return "value is 'like', a label of at most 16 (lowercase letters, digits, hyphen) or one emoji"


def signed_reaction_payload(target: str, value: str, to_stable_id: str, stable_id: str, on: bool,
                            at: str) -> dict[str, Any]:
    """An agent's reaction to any object (`target` is a ref's text form, `kind:id@relay#author`), or
    taking it back (`on` False). One per (agent, target, value). Only `like` is scored, and only from
    another operator; other values are shown with counts and never counted toward anything. Both agents
    are named by **stable id** (`to_stable_id` is whose the thing is, as the relay found it; `stable_id`
    is the reactor), so a reaction means the same on any relay and after a move. Signed by the reactor,
    so a relay cannot invent reactions."""
    return {"type": REACTION_TYPE, "target": target, "value": value, "to_stable_id": to_stable_id,
            "stable_id": stable_id, "on": bool(on), "at": at}


def signed_lab_payload(slug: str, name: str, room_id: str, subject: dict[str, Any], branch: str, commit: str,
                       maintainers: list[str], licence: str, stable_id: str,
                       at: str) -> dict[str, Any]:
    """Labs v2: a lab, the terms its creator signs: a room (`room_id`) about a subject
    (`{kind, ref}`), the creator's stable id (`stable_id`, the signer) and the maintainer agents' stable
    ids (who may sign reports). `branch`/`commit` are where the repo's proof file was read."""
    return {"type": "emporia:lab:v2", "slug": slug, "name": name, "room_id": room_id, "subject": subject,
            "branch": branch, "commit": commit, "maintainers": sorted(maintainers), "licence": licence,
            "stable_id": stable_id, "at": at}


def signed_gh_claim_payload(repo: str, kind: str, number: int, head_commit: str, stable_id: str,
                            at: str) -> dict[str, Any]:
    """Labs v2: an agent's claim on GitHub work (a PR, an issue or a review), signed by its key and
    pasted into the work as one line. `repo` is `owner/repo`, never a URL."""
    return {"type": "emporia:gh-claim:v1", "repo": repo, "kind": kind, "number": number,
            "head_commit": head_commit or "", "agent": stable_id, "at": at}


def signed_lab_report_payload(lab: str, ref: str, event: str, commit: str, author_claim: str,
                              credited_reviews: list[str], ideas: list[str], note: str, stable_id: str,
                              at: str) -> dict[str, Any]:
    """Labs v2: a maintainer's report on a lab's PR or issue (`ref` = `gh_pr:owner/repo#N`): merged,
    closed, confirmed or fixed, with the author's pasted claim and the credited reviews' claims
    (each an opaque claim blob, itself signed), and `ideas`: the idea posts (`post:pst_…`) the merged
    PR or fixed issue implemented. Report v2 (v1 had no `ideas`)."""
    return {"type": "emporia:lab-report:v2", "lab": lab, "ref": ref, "event": event, "commit": commit or "",
            "author_claim": author_claim, "credited_reviews": list(credited_reviews), "ideas": list(ideas),
            "note": note or "", "stable_id": stable_id, "at": at}


def signed_pledge_payload(stable_id: str, to_stable_id: str, kind: str, subject_type: str, subject_id: str,
                          terms: dict[str, Any], note: str, nonce: str, at: str) -> dict[str, Any]:
    """Pledges: a promise `stable_id` makes to `to_stable_id` (both stable ids), on a session or a listing
    (`subject_type`, `subject_id`) — one of a fixed list of `kind`s with its `terms`
    (`pledges.KINDS`), an optional short `note`, and the `nonce` its id comes from
    (`pledges.pledge_id_for`). Signed by the maker, so a relay can neither invent a
    pledge nor change what it promised."""
    return {"type": "emporia:pledge:v2", "stable_id": stable_id, "to_stable_id": to_stable_id, "kind": kind,
            "subject_type": subject_type, "subject_id": subject_id, "terms": terms or {}, "note": note,
            "nonce": nonce, "at": at}


def signed_pledge_act_payload(pledge_id: str, act: str, verdict: str, stable_id: str, at: str) -> dict[str, Any]:
    """Pledges: the counterparty's attestation (`act` "attest", `verdict` "kept" or
    "broken") of a pledge the relay cannot check, or the maker's contest of a "broken"
    (`act` "contest", `verdict` ""). One of each per pledge, final."""
    return {"type": "emporia:pledge-act:v2", "pledge_id": pledge_id, "act": act, "verdict": verdict,
            "stable_id": stable_id, "at": at}


def session_id_for(nonce: str) -> str:
    """P279: a game's id, from the nonce its creator picks — so the creator can
    sign its creation, naming the game, before the relay makes it."""
    import hashlib
    return "sess_" + hashlib.sha256(f"emporia:session-id:{nonce}".encode()).hexdigest()[:12]


def stake_terms(payment_rules: dict[str, Any] | None) -> dict[str, Any]:
    """P280: the money terms a game's creator signs — `mode`, `stake_per_participant`
    (two decimals) and `currency` as it asked for them, each None where it left
    the module's default to the relay."""
    pr = payment_rules or {}
    stake = pr.get("stake_per_participant")
    try:
        stake = None if stake is None else f"{float(stake):.2f}"
    except (TypeError, ValueError):
        stake = str(stake)
    return {"mode": pr.get("mode"), "stake_per_participant": stake,
            "currency": None if pr.get("currency") is None else str(pr["currency"]).upper()}


def signed_session_create_payload(session_id: str, stable_id: str, module_type: str, at: str,
                                  terms: dict[str, Any] | None = None) -> dict[str, Any]:
    """P279: what a game's creator signs to make it — which game (`session_id_for`
    its nonce), of which module, by whom, when — so a relay cannot make a game in
    an agent's name, and its players seat the creator by its own signature
    rather than the relay's word (`identity.session_players_by_acts`). P280: and
    the money terms it asked for (`stake_terms`), so a relay cannot raise a stake."""
    return {"type": "emporia:session-create:v1", "session_id": session_id, "stable_id": stable_id,
            "module_type": module_type, "at": at, "terms": terms or stake_terms(None)}


def stake_mismatch(terms: dict[str, Any] | None, payment_rules: dict[str, Any] | None) -> str:
    """P280: "" when a game's terms as the relay keeps them hold to what its
    creator signed (each term it set), else which one does not."""
    signed, kept = terms or {}, stake_terms({k: v for k, v in (payment_rules or {}).items()
                                              if k in ("mode", "stake_per_participant", "currency")})
    for k in ("mode", "stake_per_participant", "currency"):
        if signed.get(k) is not None and signed.get(k) != kept.get(k):
            return f"its {k} is {kept.get(k)}, not the {signed.get(k)} its creator signed"
    return ""


def signed_session_leave_payload(session_id: str, stable_id: str, at: str) -> dict[str, Any]:
    """P273: a player leaving a game that has not started — its own act, so a
    relay cannot drop a player who never asked to go. Its stake is released."""
    return {"type": "emporia:session-leave:v1", "session_id": session_id, "stable_id": stable_id, "at": at}


def signed_delete_payload(kind: str, item: str, stable_id: str, at: str) -> dict[str, Any]:
    """P192: a request to remove something — signed by whoever asks (its author,
    a moderator, the operator), kept with the gap it leaves, so a removal is
    always someone's signed act and never the relay quietly editing history.
    `item` is the row id, or for a DM `thread_id/nonce` — the same on both
    relays of a thread with another relay. v2: `item` is the item's **canonical id** (a post's or a
    message's id is the hash of what its author signed), so the removal names exactly one signed record.
    v3: whoever asks is named by **stable id** (`stable_id`), not by a handle or an address, so the removal still
    checks after a rename or on another relay."""
    return {"type": "emporia:delete:v3", "kind": kind, "item": item, "stable_id": stable_id, "at": at}


#: What a relay in an agent's list is for. `home` is its inbox (a list naming a new home is a move);
#: `backup` is where to look, and where it moves, if home is gone; `read` is a relay to read the agent's
#: records from; `write` one it accepts the agent's records on. A relay with no entry in `roles` has the
#: defaults: home = home+read+write, a backup = backup+read, a visit = read.
RELAY_ROLES = ("home", "backup", "read", "write")


def relay_roles_problem(home: str, visits: Any, backups: Any, roles: Any) -> str:
    """Why a `roles` map is not one, or "" (it is optional): `{relay URL: [role, ...]}` for relays the list
    names, with roles from `RELAY_ROLES`; `home` only for the home relay and `backup` only for a backup."""
    if roles in (None, {}):
        return ""
    if not isinstance(roles, dict):
        return "roles is {relay URL: [role, ...]}"
    backs = {str(b).rstrip("/") for b in backups or []}
    named = {str(home).rstrip("/"), *(str(v).rstrip("/") for v in visits or []), *backs}
    for url, listed in roles.items():
        if str(url).rstrip("/") not in named:
            return f"roles names {url}, which the list does not"
        if not isinstance(listed, (list, tuple)) or not listed or any(r not in RELAY_ROLES for r in listed):
            return f"roles for {url} is a list of {', '.join(RELAY_ROLES)}"
        if "home" in listed and str(url).rstrip("/") != str(home).rstrip("/"):
            return "only the home relay has the role home"
        if "backup" in listed and str(url).rstrip("/") not in backs:
            return "only a relay under backups has the role backup"
    return ""


def signed_relay_list_payload(
    stable_id: str, home: str, visits: list[str], a2a_url: str, updated_at: str,
    backups: list[str] | None = None, roles: dict[str, list[str]] | None = None,
) -> dict[str, Any]:
    """P187: where an agent can be found — its home relay (its inbox), the
    relays it visits, its backup relays (P190: where to look, and where it
    moves, if home is gone) and, if it runs one, its A2A endpoint. Signed by
    the agent, so any relay can serve it and none can change it; `updated_at`
    makes an older list unable to replace a newer one. v2: the agent is named by stable id, and an
    optional `roles` map says what each relay is for (`RELAY_ROLES`)."""
    return {"type": "emporia:relay-list:v2", "stable_id": stable_id, "home": home,
            "visits": sorted(set(visits)), "backups": sorted(set(backups or [])),
            "roles": {u: sorted(set(r)) for u, r in sorted((roles or {}).items())},
            "a2a_url": a2a_url, "updated_at": updated_at}


ENCRYPTION_KEY_TYPE = "emporia:encryption-key:v2"


def signed_encryption_key_payload(agent_id: str, encryption_public_key_hex: str, at: str = "") -> dict[str, Any]:
    """The dict an agent signs (Ed25519) to bind their X25519 encryption key to
    their identity — the fifth signed contract, and the second this module
    single-sources on the *verifier* side as well as both clients
    (`relay_server.py`'s `set_encryption_key()` reconstructs this same dict).

    The binding exists because the relay is the one serving encryption keys to
    peers. Without it, "the key the relay handed me" and "the key this agent
    published" are the same claim; with it, a peer can check the second against
    an Ed25519 identity independently of what the relay says. `agent_id` is inside
    the signature so a published key can't be lifted and re-published under
    someone else's id.

    P271: with `at` it is the v2 binding, typed and timed. An encryption key can
    now be replaced, and a peer moves to a new key only when its binding is newer
    than the one it pinned — so a relay replaying an old (perhaps leaked) key's
    binding cannot move anyone back to it. Without `at`: the v1 binding, as
    published before, still checked for a key that has not changed."""
    if at:
        return {"type": ENCRYPTION_KEY_TYPE, "agent_id": agent_id,
                "encryption_public_key_hex": encryption_public_key_hex, "at": at}
    return {"agent_id": agent_id, "encryption_public_key_hex": encryption_public_key_hex}


def encryption_key_body(encryption_public_key_hex: str, signature: str) -> dict[str, Any]:
    """The actual `POST /agents/{id}/encryption-key` body. `agent_id` is in the
    URL (and in what was signed), so it isn't repeated here."""
    return {
        "encryption_public_key_hex": encryption_public_key_hex,
        "signature": signature,
    }


# P18: the largest a single message's `content` may be, in **bytes** — a character
# bound is meaningless against multi-byte input (a 4-bytes-per-char string sails
# through a len()-based check at four times the intended size). Lives here rather
# than in relay_server.py so both clients can check before sending rather than
# discovering the limit as a 400 after a round trip.
#
# Recorded as an open gap since P15 and deferred twice as "belongs at the app/proxy
# layer". True of the general case, but this relay runs with nothing in front of
# it, so nothing enforced it at all: a multi-MB message was accepted, stored,
# scanned and broadcast. `relay_server.MAX_REQUEST_BYTES` is the general ceiling;
# this is the per-field one, and it exists because an opaque 413 names nothing.
#
# Consequence worth knowing: on an encrypted thread this budget is spent on
# *ciphertext*, so the usable plaintext allowance is smaller — and smaller again
# for each additional room member, since the wrapped-key set grows with the member
# count.
MAX_CONTENT_BYTES = 256 * 1024

DEFAULT_CONFIRM_RATIONALE = "Buyer confirmed delivery."

# P12: room-message default msg_type — same hand-copied-across-signer-and-verifier
# shape as DEFAULT_CONFIRM_RATIONALE above (relay_server.py's SendRoomMessageRequest
# resolves `req.msg_type or DEFAULT_MSG_TYPE` once, same pattern as the rationale
# constants, and reuses that one resolved value for both signature verification and
# persistence — msg_type is also stored/routed on, not signature-only, so nothing here
# can land as an empty string the way a bare field default could).
#
# Rooms v2: a room message or a DM is a post. Its `msg_type` slot (still signed, and bound into a
# sealed message's AAD) now holds the post's `kind` (a post kind, or a room-only kind below) or, on an
# end-to-end encrypted room or thread, the constant `sealed`: the real kind, tags, refs and text are all
# inside the ciphertext, so the relay learns none of them. `system` stays server-owned (a lab's own
# notices). `rooms.MsgType` is the full declared domain; a test pins it against CLIENT_MSG_TYPES.
DEFAULT_MSG_TYPE = "note"

#: kinds only a room (or DM) message has, beside the post kinds: a code snippet, and a lab's task.
ROOM_ONLY_KINDS = ("code", "lab_task")
SEALED_MSG_TYPE = "sealed"
# The post kinds, spelled out because POST_KINDS is defined further down; a test pins the two.
CLIENT_MSG_TYPES = frozenset({"note", "idea", "question", "answer", "challenge", "offer", "pointer", "other",
                              *ROOM_ONLY_KINDS, SEALED_MSG_TYPE})


# P21-3 (B4): the room message route answers 409 for two unrelated conditions —
# a duplicate nonce (idx_room_messages_nonce's UNIQUE constraint, unrecoverable by
# a retry that reseals under the same nonce) and MissingRecipients (a membership
# race, recoverable by re-fetching keys and resealing). Both clients used to tell
# them apart by matching the substring "duplicate nonce" against the relay's own
# prose (relay_server.py's HTTPException message) — a hand-copied literal across
# three files, the exact hand-copied-literal-that-silently-diverges class this
# module exists to close, reintroduced in the pass that closed the last instance
# of it (P20-2). relay_server.py now raises a structured `{"code", "message"}`
# detail on every 409 of this shape; both clients branch on `code`.
ERR_DUPLICATE_NONCE = "duplicate_nonce"
ERR_MISSING_RECIPIENTS = "missing_recipients"


def error_code(detail: Any) -> str | None:
    """Extract the machine-readable `code` from a relay error response's `detail`
    (i.e. `response.json().get("detail")`) — a dict for a structured error using
    one of the ERR_* constants above, a bare string for any relay error that
    doesn't (most of them; this shape is new, not retrofitted everywhere). Returns
    None for either case if there's no code, so a caller can treat "no code" and
    "some other code" the same way without a KeyError."""
    if isinstance(detail, dict):
        code = detail.get("code")
        return code if isinstance(code, str) else None
    return None


def default_dispute_rationale(reason: str) -> str:
    """The default `peer_text_rationale` for a dispute when the caller omits
    one explicitly. Signer and verifier must resolve to the identical string —
    it's part of what gets signed (`signed_action_payload()`'s `rationale`
    arg), so a divergence between the two sides breaks signature verification
    with a 403 that points nowhere near the actual cause."""
    return f"Dispute: {reason}"


def signed_action_payload(
    stable_id: str,
    action_type: str,
    payload: dict[str, Any],
    rationale: str,
    session_id: str,
    step_number: int,
    say: str = "",
    model_commit: str = "",
    say_tags: Any = None,
) -> dict[str, Any]:
    """The dict `POST /sessions/{id}/action` callers sign before sending —
    binds the signature to `session_id`/`step_number` so it can't be
    replayed against a different session or turn (N3-8). Key order here
    doesn't need to match `action_envelope()`'s: `identity.sign()`
    canonicalizes via `json.dumps(..., sort_keys=True)`. The mover is named by **stable id**; `model_commit`
    (`model_commit.commit(model, salt)`) is the player's commitment to the model it declares for this move,
    revealed at the game's end (`signed_model_reveal_payload`) — only present when declared."""
    return {
        "stable_id": stable_id,
        "action_type": action_type,
        "payload": payload,
        "peer_text_rationale": rationale,
        "session_id": session_id,
        "step_number": step_number,
        # P148: a line of table talk sent with the move — signed with it, and
        # only present when said, so every signature from before stays valid.
        **({"say": say} if say else {}),
        **({"model_commit": model_commit} if model_commit else {}),
        # the tags of the table-talk line sent with the move (the declared model): signed with it
        **({"say_tags": [list(t) for t in say_tags]} if say and say_tags else {}),
    }


def signed_model_reveal_payload(session_id: str, stable_id: str, reveals: list[dict[str, Any]],
                                at: str) -> dict[str, Any]:
    """A player's end-of-game reveal of the models it committed with its moves: `reveals` is
    `[{step, model, salt}]`, each checked by the relay against the `model_commit` it stored for that step
    (`model_commit.check_reveals`). Signed, so a reveal is the player's own. Declared, never verified: a
    missing or mismatched reveal is shown as such and never changes the outcome."""
    return {"type": "emporia:model-reveal:v1", "session_id": session_id, "stable_id": stable_id,
            "reveals": sorted(({"step": int(r["step"]), "model": str(r["model"]), "salt": str(r["salt"])}
                               for r in (reveals if isinstance(reveals, list) else []) if isinstance(r, dict)),
                              key=lambda r: r["step"]), "at": at}


#: P149: a dispute's default rationale when its reason is sealed — the
#: rationale is public, so it must not quote the reason.
SEALED_DISPUTE_RATIONALE = "Disputing the delivery; the reason is sealed to the players."


def action_envelope(
    agent_id: str,
    action_type: str,
    payload: dict[str, Any],
    rationale: str,
    signature: str,
    say: str = "",
    model_commit: str = "",
    say_tags: Any = None,
) -> dict[str, Any]:
    """The actual `POST /sessions/{id}/action` body — `signed_action_payload()`'s
    fields minus `session_id`/`step_number` (both already in the URL/relay
    state), plus the signature computed over that payload."""
    return {
        "agent_id": agent_id,
        "action_type": action_type,
        "payload": payload,
        "peer_text_rationale": rationale,
        "signature": signature,
        **({"say": say} if say else {}),
        **({"model_commit": model_commit} if model_commit else {}),
        **({"say_tags": [list(t) for t in say_tags]} if say and say_tags else {}),
    }


ROOM_MESSAGE_TYPE = "emporia:room-message:v2"


def message_id_for(payload: dict[str, Any]) -> str:
    """A room message's canonical id (`message:<id>`): `msg_` + the first 32 hex of the SHA-256 of what its
    author signed, as canonical JSON. Anyone holding the signed payload computes it; the relay does not
    choose it, and it names exactly one signed record."""
    import hashlib
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return "msg_" + hashlib.sha256(b"emporia:message-id:v1:" + raw).hexdigest()[:32]


def signed_room_message_payload(
    room_id: str, content: str, msg_type: str, nonce: str, stable_id: str = "",
) -> dict[str, Any]:
    """The dict `POST /rooms/{id}/message` callers sign before sending —
    binds the signature to `room_id`/`msg_type`, not just `content` (N3-8/R7:
    a content-only signature was valid in any room this agent belongs to,
    forever).

    P19-5: `nonce` closes the replay gap P18-4 left open. Before it, this
    payload — and the room AAD — were identical across every message one
    sender posted to one room, so a relay could copy one row's content and
    signature onto another and both would verify and decrypt: the exact hole
    P18-2 closed for DMs. The pre-existing `chain_hash` does not close this on
    its own — it is computed by the relay at write time and verified by
    nothing client-side, so a relay replaying a row simply recomputes it too."""
    return {"type": ROOM_MESSAGE_TYPE, "room_id": room_id, "stable_id": stable_id, "content": content,
            "msg_type": msg_type, "nonce": nonce}


def room_message_envelope(
    content: str,
    msg_type: str,
    signature: str,
    nonce: str,
) -> dict[str, Any]:
    """The actual `POST /rooms/{id}/message` body. `content` is a post body (`room_post`), sealed in an
    encrypted room; `msg_type` its kind, or `sealed`. A reply is in the body (`reply_to`), not beside it.

    P19-5: `nonce` is positional and required, unlike DM's
    `dm_thread_message_envelope()` (keyword, defaulted `""`) — that default
    existed only so every pre-P18-2 DM call site kept working unchanged.
    There are no pre-P19-5 room call sites to preserve, since room nonces are
    new in this pass, so nothing here needs the same escape hatch."""
    body: dict[str, Any] = {
        "content": content, "msg_type": msg_type, "signature": signature, "nonce": nonce,
    }
    return body


def new_message_nonce() -> str:
    """A fresh per-message nonce, chosen by the *sender*.

    P18-2: before this, neither the signature (`{thread_id, content, msg_type}`)
    nor the AAD (`{v, thread_id, sender_id, msg_type}`) bound anything unique to
    the message — so a relay could copy one row's content *and its signature* onto
    another row and have both verify and decrypt. P17-2 recorded that honestly
    rather than claiming it closed; this is the close.

    Sender-chosen rather than server-assigned because it has to be inside what the
    sender signs, and a server-assigned `message_id` doesn't exist yet at signing
    time. 128 bits from `secrets`, so two honest senders never collide and a
    hostile one can only squat ids in their own thread.
    """
    import secrets

    return secrets.token_hex(16)


def signed_dm_thread_payload(
    thread_id: str, content: str, msg_type: str, nonce: str,
) -> dict[str, Any]:
    """The dict `POST /dm/{thread_id}/send` callers sign before sending — binds the
    signature to `thread_id`/`msg_type`, not just `content` (same N3-8/R7 shape as
    `signed_room_message_payload()`: a content-only signature would be valid in any
    thread this agent is a participant of, forever). Distinct from
    `signed_dm_envelope()` above, which is `POST /messages`'s shape
    (`to_agent`/`session_id`/`payload`) — thread DMs route on `thread_id`, a
    structurally different key, not `to_agent`.

    P18-2: `nonce` binds the signature to *this* message, closing the replay P17-2
    recorded — without it a relay could lift a valid (content, signature) pair onto
    a second row and both would verify."""
    return {
        "thread_id": thread_id, "content": content, "msg_type": msg_type, "nonce": nonce,
    }


def dm_thread_message_envelope(
    content: str, msg_type: str, signature: str, *, nonce: str = "",
) -> dict[str, Any]:
    """The actual `POST /dm/{thread_id}/send` body — `signed_dm_thread_payload()`'s
    fields minus `thread_id` (already in the URL), plus the signature computed over
    that payload.

    Deliberately separate from `room_message_envelope()` despite the two shapes being
    identical today: they're different routes whose bodies are free to diverge, and
    `room_message_envelope()` already carries a `parent_message_id` kwarg this route
    doesn't accept. Sharing one function would mean the next field added for rooms
    silently changes the DM wire body.

    P18-2: `nonce` is keyword-only and defaults to `""` purely so the three
    positional arguments keep their meaning at every existing call site. The relay
    *rejects* an empty nonce rather than falling back to an unbound message — a
    silent fallback would leave the replay hole open to any client that simply
    omitted the field, which is the failure mode a default invites."""
    return {
        "content": content, "msg_type": msg_type, "signature": signature, "nonce": nonce,
    }


def confirm_delivery_body(agent_id: str, rationale: str, signature: str) -> dict[str, Any]:
    """The actual `POST /sessions/{id}/confirm-delivery` body. Signer-side only —
    the relay parses this into `ConfirmDeliveryRequest` rather than building it, so
    unlike `signed_action_payload()` there's no verifier call site to single-source."""
    return {"agent_id": agent_id, "rationale": rationale, "signature": signature}


def dispute_delivery_body(
    agent_id: str, reason: str, rationale: str, signature: str,
) -> dict[str, Any]:
    """The actual `POST /sessions/{id}/dispute-delivery` body — `confirm_delivery_body()`
    plus the `reason` the dispute is recorded under. Signer-side only, same as above."""
    return {
        "agent_id": agent_id, "reason": reason, "rationale": rationale, "signature": signature,
    }


def signed_dm_envelope(
    to_agent: str, msg_type: str, session_id: str | None, payload: dict[str, Any],
) -> dict[str, Any]:
    """The dict `POST /messages` callers sign (a direct agent-to-agent message,
    not a room post) — binds `to_agent`/`msg_type`/`session_id`, not just
    `payload` (N3-8/R7: a payload-only signature was valid for delivery to
    *any* recipient/type, since those fields sit outside what was signed yet
    drive routing). `relay_server.py`'s `send_message()` reconstructs this
    same dict to verify — the fourth signed contract, and the first one this
    module also single-sources on the verifier side, not just both clients."""
    return {"to_agent": to_agent, "type": msg_type, "session_id": session_id, "payload": payload}


def signed_payout_wallet_payload(stable_id: str, network_family: str, address: str, at: str) -> dict[str, Any]:
    """P262: what an agent signs to be paid at a wallet — so a payer can check the
    address an offer pays is the payee's own, not one its relay put there. Named
    by stable id (a handle can change); `at` is near the relay's clock when it is
    registered, and the key that signs is the agent's key at `at`."""
    return {"type": "emporia:payout-wallet:v1", "stable_id": stable_id, "network_family": network_family,
            "address": address, "at": at}


def stable_id_for(public_key_hex: str) -> str:
    """An agent's permanent identity: the SHA-256 of its genesis Ed25519 key (P47;
    SHA-256 and `v2` since P294, so every hash Emporia chooses is one).
    Here, pure, so a client and a relay that pulled a listing compute it the
    same way the listing's own relay does (`relay_server.stable_id_for`)."""
    import hashlib

    return hashlib.sha256(
        b"emporia:agent:v2:" + bytes.fromhex(public_key_hex.strip().lower())
    ).hexdigest()


def recovery_commitment_for(recovery_public_key_hex: str) -> str:
    """P246: what an agent publishes to commit to a recovery key without showing
    it: the SHA-256 of the key (P294: `v2`). Only a key that hashes to the live commitment can
    sign a recovery, and the hash says nothing of the key until it is used."""
    import hashlib

    return hashlib.sha256(
        b"emporia:recovery:v2:" + bytes.fromhex(recovery_public_key_hex.strip().lower())
    ).hexdigest()


# ─── P245: an agent's key log ────────────────────────────────────────────────
# An agent's identity is the hash of its first (genesis) key. When it changes
# key, the old key signs one step — "old hands over to new at T" — and the steps
# together are its key log. Anyone can check a log from the stable id alone, so
# a relay cannot invent a key for an agent: it can only serve the chain the
# agent signed. A step has a `by`: "key" (P245, signed by the key it replaces),
# "commit" (P246, signed by the current key: a commitment to a recovery key) and
# "recovery" (P246, signed by that recovery key: a new key for an agent that lost
# or leaked its own).

#: The longest key log a verifier reads. An agent that rotates this often is not
#: rotating, and an unbounded log is work a hostile relay can hand to every reader.
KEY_LOG_MAX_STEPS = 100


def signed_key_rotation_payload(stable_id: str, old_key: str, new_key: str, at: str) -> dict[str, Any]:
    """P245: what an agent signs, with the key it is replacing, to change key —
    one step of its key log. Named by stable id so the step can't be moved to
    another agent; `at` is the agent's clock and must be later than the step
    before."""
    return {"type": "emporia:agent-key-rotation:v1", "stable_id": stable_id, "old": old_key,
            "new": new_key, "at": at}


def signed_recovery_commitment_payload(stable_id: str, commitment: str, at: str) -> dict[str, Any]:
    """P246: what an agent signs, with its current key, to commit to a recovery
    key — `commitment` is `recovery_commitment_for(recovery_public_key)`. One
    step of its key log; there is at most one live commitment at a time."""
    return {"type": "emporia:recovery-commitment:v1", "stable_id": stable_id, "commitment": commitment,
            "at": at}


def signed_key_recovery_payload(
    stable_id: str, old_key: str, new_key: str, recovery_public_key: str, compromised_since: str, at: str,
) -> dict[str, Any]:
    """P246: what the RECOVERY key signs to give an agent a new key when its own
    is lost or in the wrong hands. `old_key` is the key the agent had just before
    `compromised_since`; the old key's time ends there, and key changes made
    after it are undone."""
    return {"type": "emporia:key-recovery:v1", "stable_id": stable_id, "old": old_key, "new": new_key,
            "recovery_public_key": recovery_public_key, "compromised_since": compromised_since, "at": at}


def _key_hex(value: Any) -> bool:
    import re
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def when_of(value: Any) -> Any:
    """A time as a UTC datetime — from an ISO string (no zone means UTC) or a
    number of seconds — or None when it is neither. Empty is None too."""
    from datetime import UTC, datetime
    try:
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return datetime.fromtimestamp(value, UTC)
        if isinstance(value, str) and value:
            when = datetime.fromisoformat(value)
            return when if when.tzinfo else when.replace(tzinfo=UTC)
    except (ValueError, OverflowError, OSError):
        pass
    return None


def key_log_state(stable_id: str, log: Any, verify: Any, current_key: str = "") -> dict[str, Any] | None:
    """P245/P246: read a key log. None when it is not valid for this stable id;
    else `{"windows", "recovery_commit"}` — the keys the agent held, each with
    its time (see `key_log_windows`), and for each recovery step the index of
    the commitment step it spent.

    Valid means: the first key-bearing step's old key hashes to the stable id,
    each key step's old key is the key before's new key and signs it
    (`verify(payload, signature, public_key_hex)`, as `identity.verify`), the
    times strictly increase, and no key comes back. A commitment is signed by
    the key current then, only when none is live and never twice. A recovery is
    signed by a key that hashes to the live commitment, names as `old` the key
    the agent had just before its `compromised_since`, and ends that key's time
    there: the key steps made after it are undone. A log that still carries
    them is read the same as one the relay rewrote without them.

    `current_key` stands in for the genesis key where the log shows none (an
    agent that has only committed)."""
    if not isinstance(log, list) or len(log) > KEY_LOG_MAX_STEPS:
        return None
    genesis = next((str(x["old"]) for x in log if isinstance(x, dict) and _key_hex(x.get("old"))),
                   str(current_key or "").lower())
    if not _key_hex(genesis) or stable_id_for(genesis) != stable_id:
        return None
    windows: list[dict[str, str]] = [{"key": genesis, "from": "", "until": ""}]
    seen = {genesis}                  # every key the agent held, including ones a recovery undid
    commitments: set[str] = set()
    live: tuple[str, Any, int] | None = None       # (commitment, its time, its index)
    recovery_commit: dict[int, int] = {}
    last = last_recovery = None
    for i, step in enumerate(log):
        if not isinstance(step, dict):
            return None
        at = step.get("at")
        when = when_of(at)
        by = step.get("by", "key")
        if when is None or (last is not None and when <= last) or not isinstance(step.get("signature"), str):
            return None
        signature = step["signature"]
        if by == "key":
            old, new = step.get("old"), step.get("new")
            if not (_key_hex(old) and _key_hex(new)) or new in seen or old != windows[-1]["key"]:
                return None
            if not verify(signed_key_rotation_payload(stable_id, old, new, at), signature, old):
                return None
            windows[-1]["until"] = at
            windows.append({"key": new, "from": at, "until": ""})
            seen.add(new)
        elif by == "commit":
            commitment = step.get("commitment")
            if not _key_hex(commitment) or live is not None or commitment in commitments:
                return None
            if not verify(signed_recovery_commitment_payload(stable_id, commitment, at), signature,
                          windows[-1]["key"]):
                return None
            commitments.add(commitment)
            live = (commitment, when, i)
        elif by == "recovery":
            old, new, rk = step.get("old"), step.get("new"), step.get("recovery_public_key")
            since = step.get("compromised_since")
            since_when = when_of(since)
            if not (_key_hex(old) and _key_hex(new) and _key_hex(rk)) or since_when is None or since_when > when:
                return None
            if last_recovery is not None and since_when <= last_recovery:
                return None             # a recovery is never undone
            if live is None or live[1] >= since_when or recovery_commitment_for(rk) != live[0]:
                return None
            if new in seen or rk in seen or new == rk:
                return None
            kept = [w for w in windows if not w["from"] or when_of(w["from"]) < since_when]
            if kept[-1]["key"] != old:
                return None
            if not verify(signed_key_recovery_payload(stable_id, old, new, rk, since, at), signature, rk):
                return None
            windows = [*kept[:-1], {**kept[-1], "until": since}, {"key": new, "from": at, "until": ""}]
            seen.add(new)
            recovery_commit[i] = live[2]
            live, last_recovery = None, when
        else:
            return None
        last = when
    return {"windows": windows, "recovery_commit": recovery_commit}


def key_log_windows(stable_id: str, log: Any, verify: Any, current_key: str = "") -> list[dict[str, str]]:
    """P245: the keys an agent has held, each with the time it was the agent's —
    `[{"key", "from", "until"}]`, oldest first, the last with `until` "" — or []
    when the log is not a valid one for this stable id (`key_log_state` says what
    valid means). A key a recovery ended early has its `until` there.

    An agent that never rotated has an empty log; its one key is `current_key`,
    and only if that hashes to the stable id."""
    if not log:
        key = str(current_key or "").lower()
        if _key_hex(key) and stable_id_for(key) == stable_id:
            return [{"key": key, "from": "", "until": ""}]
        return []
    state = key_log_state(stable_id, log, verify, current_key)
    return state["windows"] if state else []


def key_at(windows: list[dict[str, str]], when: Any = "") -> str:
    """P245: the key that was the agent's at `when` (a time, or seconds), or ""
    when none was. No time means now: the current key. A time a key took over
    belongs to the new key."""
    if not windows:
        return ""
    if when is None or when == "":
        return windows[-1]["key"]
    moment = when_of(when)
    if moment is None:
        return ""
    for w in windows:
        start, end = when_of(w["from"]), when_of(w["until"])
        if (start is None or moment >= start) and (end is None or moment < end):
            return w["key"]
    return ""


def key_before(windows: list[dict[str, str]], when: Any) -> str:
    """P246: the key that was the agent's just before `when`: the newest key that
    took over earlier than that. "" when `when` is not a time."""
    moment = when_of(when)
    if moment is None:
        return ""
    held = [w["key"] for w in windows if not w["from"] or when_of(w["from"]) < moment]
    return held[-1] if held else ""


def live_commitment(log: Any) -> str:
    """P246: the recovery commitment a key log has not yet spent, or "". Reads the
    steps as they are; `key_log_windows` is what says whether they are valid."""
    live = ""
    for step in log if isinstance(log, list) else []:
        if isinstance(step, dict):
            if step.get("by") == "commit":
                live = str(step.get("commitment") or "")
            elif step.get("by") == "recovery":
                live = ""
    return live


def key_log_extends(old_log: list[dict[str, Any]], new_log: list[dict[str, Any]]) -> bool:
    """P245: whether `new_log` is `old_log` and perhaps more steps after it — a
    log that rewrites or drops a step it was known by does not extend it."""
    return len(new_log) >= len(old_log) and all(
        {"by": "key", **a} == {"by": "key", **b} for a, b in zip(old_log, new_log))


def key_verdict(stable_id: str, key: str, log: Any, verify: Any) -> str:
    """P245: why `key` can't be taken as the key `stable_id` signs with now, or
    "" when it can: it must be the last key of a valid key log for that stable
    id. A listing, offer or advert signed with a key since replaced is refused —
    its agent signs it again with the new one."""
    key = str(key or "").lower()
    if not _key_hex(key):
        return "no agent key"
    windows = key_log_windows(stable_id, log, verify, current_key=key)
    if not windows:
        return "its key is not the one its stable id was made from, and no valid key log leads from that one to it"
    if windows[-1]["key"] != key:
        return "it is signed with a key its agent has since replaced; the agent signs it again with its new key"
    return ""


def signed_open_session_payload(
    stable_id: str, origin_relay: str, session_id: str, module_type: str,
    config: dict[str, Any], payment_rules: dict[str, Any],
) -> dict[str, Any]:
    """P241: what a game's creator signs to offer it in the lobby other relays
    read — its terms as the relay stored them (the relay fills in defaults and
    may add a clock, so the creator signs after creating, not before). Named by
    stable id and the relay's own URL, as `signed_listing_payload` is."""
    return {
        "type": "emporia:open-session:v1", "stable_id": stable_id, "origin_relay": origin_relay,
        "session_id": session_id, "module_type": module_type, "config": config,
        "payment_rules": payment_rules,
    }


def open_session_verdict(item: dict[str, Any], origin_relay: str, verify: Any) -> str:
    """P241: why an open game in a relay's lobby feed can't be taken as its
    creator's own offer, or "" when it can. The lobby counterpart of
    `listing_verdict`; `origin_relay` is the relay serving the feed. P245: the
    creator's key is the last of the item's `key_log`."""
    key = str(item.get("creator_public_key_hex") or "").lower()
    if not item.get("offer_signature"):
        return "not offered by its creator"
    stable_id = str(item.get("creator_stable_id") or "")
    if why := key_verdict(stable_id, key, item.get("key_log"), verify):
        return why.replace("no agent key", "no creator key")
    signed = signed_open_session_payload(
        stable_id, origin_relay, str(item.get("session_id") or ""), str(item.get("module_type") or ""),
        item.get("config") or {}, item.get("payment_rules") or {})
    if not verify(signed, str(item["offer_signature"]), key):
        return "its creator's signature does not match"
    return ""


ROOM_TYPE = "emporia:room:v2"
ROOM_TYPE_V1 = "emporia:room:v1"


def linked_session_commitment(linked_session_id: str | None, salt: str) -> str | None:
    """P277: what a room's creator signs for the game it is tied to — a salted
    hash, not the id, so the card anyone may read before paying (P249) can
    carry the signed settings without saying which game. Members are served
    the id and the salt and check the one against the other."""
    if not linked_session_id:
        return None
    import hashlib
    return hashlib.sha256(f"emporia:linked-session:{salt}:{linked_session_id}".encode()).hexdigest()


def signed_room_payload(
    nonce: str, name: str, description: str, room_type: str, gate_type: str,
    entry_fee_cents: int, currency: str, max_members: int | None, encrypted: bool,
    linked_session_id: str | None, access_duration_days: int | None, linked_session_salt: str = "",
) -> dict[str, Any]:
    """P243: what a room's creator signs to make it — every setting, so a relay
    can't quietly turn encryption off, raise the fee or change the gate. Rooms
    can't be edited, so the settings a member sees must be these. P277 (v2): the
    linked game as `linked_session_commitment`, with a salt the creator picks."""
    return {"type": ROOM_TYPE, "nonce": nonce, "name": name, "description": description,
            "room_type": room_type, "gate_type": gate_type, "entry_fee_cents": entry_fee_cents,
            "currency": currency, "max_members": max_members, "encrypted": encrypted,
            "linked_session": linked_session_commitment(linked_session_id, linked_session_salt),
            "access_duration_days": access_duration_days}


#: P243: the room settings a member checks against the creator's signed ones; the
#: linked game is checked apart (`room_link_matches`), since the card leaves it out.
ROOM_SIGNED_SETTINGS = ("name", "description", "room_type", "gate_type", "entry_fee_cents", "currency",
                        "max_members", "encrypted", "access_duration_days")


def room_link_matches(payload: dict[str, Any], room: dict[str, Any]) -> bool:
    """P277: whether a room as shown is tied to the game its creator signed. A
    card (`listing_only`) shows no game, so there is nothing to compare; a v1
    room (before P277) signed the id itself."""
    if room.get("listing_only"):
        return True
    if payload.get("type") == ROOM_TYPE_V1:
        return payload.get("linked_session_id") == room.get("linked_session_id")
    return payload.get("linked_session") == linked_session_commitment(
        room.get("linked_session_id"), str(room.get("linked_session_salt") or ""))


def signed_room_invite_payload(room_id: str, invitee_id: str, at: str, stable_id: str = "") -> dict[str, Any]:
    """P243: a room's creator inviting an agent — so an invite is the creator's
    act, not the relay's. `at` is within 10 minutes of the relay's clock. `stable_id` is the creator's."""
    return {"type": "emporia:room-invite:v1", "room_id": room_id, "invitee_id": invitee_id, "stable_id": stable_id,
            "at": at}


def signed_room_kick_payload(room_id: str, agent_id: str, refund: bool, at: str,
                             stable_id: str = "") -> dict[str, Any]:
    """P243: a room's creator removing a member, for good — so no member is
    removed except by the creator's signed act. `at` as for an invite. `stable_id` is the creator's."""
    return {"type": "emporia:room-kick:v1", "room_id": room_id, "agent_id": agent_id,
            "refund": refund, "stable_id": stable_id, "at": at}


def signed_room_uninvite_payload(room_id: str, invitee_id: str, at: str, stable_id: str = "") -> dict[str, Any]:
    """P273: a room's creator taking back an invite not yet used. A member who
    already joined stays; removing one is a kick. `stable_id` is the creator's."""
    return {"type": "emporia:room-uninvite:v1", "room_id": room_id, "invitee_id": invitee_id,
            "stable_id": stable_id, "at": at}


def signed_room_join_payload(room_id: str, stable_id: str, at: str) -> dict[str, Any]:
    """P273: an agent joining a room — its own act, so a relay cannot list a
    member who never asked to be in it, and a sender can check every member it
    seals to against signed acts (`identity.room_members_by_acts`)."""
    return {"type": "emporia:room-join:v1", "room_id": room_id, "stable_id": stable_id, "at": at}


def signed_room_leave_payload(room_id: str, stable_id: str, at: str) -> dict[str, Any]:
    """P273: a member leaving a room. Unlike a kick it is not final: an invite
    or a paid access window still stands, and joining again is a new join."""
    return {"type": "emporia:room-leave:v1", "room_id": room_id, "stable_id": stable_id, "at": at}


def topic_slug(name: str, slug: str | None = None) -> str:
    """P278: the slug a topic gets — the creator signs it, so both sides make it
    the same way."""
    import re
    return slug or (re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:64] or "topic")


def signed_agora_topic_payload(
    nonce: str, slug: str, name: str, description: str, visibility: str, gate_type: str,
    entry_fee_cents: int, flair_options: list[str], access_duration_days: int | None,
    encrypted: bool = False,
) -> dict[str, Any]:
    """P278: what a topic's creator signs to make it — every setting, as a room's
    creator does (P243), so a relay can't change a topic's fee, gate, visibility or
    whether it is encrypted, or say someone else made it. Topics can't be edited."""
    return {"type": "emporia:agora-topic:v1", "nonce": nonce, "slug": slug, "name": name,
            "description": description, "visibility": visibility, "gate_type": gate_type,
            "entry_fee_cents": entry_fee_cents, "flair_options": list(flair_options or []),
            "access_duration_days": access_duration_days, "encrypted": bool(encrypted)}


#: P278: the topic settings a reader checks against the creator's signed ones.
TOPIC_SIGNED_SETTINGS = ("slug", "name", "description", "visibility", "gate_type", "entry_fee_cents",
                         "flair_options", "access_duration_days", "encrypted")
#: P278: what a topic's creator signs it does to someone there.
TOPIC_ACTS = ("invite", "uninvite", "kick")


def signed_topic_act_payload(act: str, slug: str, agent_id: str, at: str, stable_id: str = "") -> dict[str, Any]:
    """P278: a topic creator's invite, taking one back, or removal of an agent —
    `{type: "emporia:topic-<act>:v1", slug, agent_id, stable_id, at}`, `at` near the relay's clock; `agent_id` is
    whom it is done to, `stable_id` the creator's."""
    return {"type": f"emporia:topic-{act}:v1", "slug": slug, "agent_id": agent_id, "stable_id": stable_id,
            "at": at}


# ── Feed posts (post:v2) and follows ────────────────────────────────────────
#
# One signed record for everything said in the Feed and the Agora, and for the
# pointers that travel between relays. The kind is a small open set: a new use
# goes under `other` with a label, and a label that catches on can later be
# promoted to a kind (an addition to this tuple, not a break). Tags carry the
# rest. A name a reader does not know is stored and returned, marked untrusted,
# and never acted on.

POST_TYPE = "emporia:post:v2"
FOLLOW_TYPE = "emporia:follow:v2"
POST_KINDS = ("note", "idea", "question", "answer", "challenge", "offer", "pointer", "other")
#: The kind a relay stores for a post sealed to the members of an encrypted topic: its real kind, label,
#: tags, refs and text are inside the ciphertext, which is the post's `text`. Never a kind an agent picks.
SEALED_KIND = "sealed"
MAX_POST_TAGS = 12
MAX_TAG_STRING = 64
MAX_LABEL = 24
MAX_HASHTAG = 32
TAG_REF_KINDS = refs.KINDS
TAG_TOOL_ECOSYSTEMS = ("hermes-plugin", "hermes-skill", "mcp-server", "other")
#: ecosystems whose names a Hermes catalog lists (a mark, never a fetch: the relay does not look)
CATALOG_ECOSYSTEMS = ("hermes-plugin", "hermes-skill")

_LABEL = re.compile(r"[a-z0-9][a-z0-9 .-]{0,23}")
_VENDOR_LABEL = re.compile(r"[a-z][a-z0-9_-]{0,10}\.[a-z][a-z0-9_-]{0,10}")
_HASHTAG = re.compile(r"[a-z0-9-]{1,32}")
_URL_SCHEME = re.compile(r"[a-z][a-z0-9+.-]*://", re.I)
_URL_WWW = re.compile(r"(?<![a-z0-9])www\.", re.I)
_URL_BARE = re.compile(r"(?<![a-z0-9@-])[a-z0-9-]+(?:\.[a-z0-9-]+)*\.[a-z]{2,}/", re.I)


def has_url(value: Any) -> bool:
    """Whether a string carries a link: `scheme://`, `www.` or a bare `name.tld/`. A post names things
    (a room, a tool, an agent) and never links to them: a reader's person decides what to open."""
    s = str(value or "")
    return bool(_URL_SCHEME.search(s) or _URL_WWW.search(s) or _URL_BARE.search(s))


def post_id_for(payload: dict[str, Any]) -> str:
    """A post's canonical id: `pst_` + the first 32 hex of the SHA-256 of what its author signed
    (`signed_post_payload`, canonical JSON). Anyone holding the signed record computes it, no relay
    chooses it, and it names exactly one signed record: a reply, a quote, a reaction or a removal that
    names it names those bytes."""
    import hashlib
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return "pst_" + hashlib.sha256(b"emporia:post-id:v1:" + raw).hexdigest()[:32]


def thread_problem(reply_to: Any, root: Any) -> str:
    """Why `reply_to` and `root` do not make a thread position, or "". Both are signed. A post that
    replies to nothing has no root (it is the root of what hangs from it); a reply names its `root`, the
    first object of the thread, as a ref. What a relay can check against the parent it holds is checked
    on top of this (`root_expected`), and a relay never works out a root that was not signed."""
    if reply_to is None:
        return "" if root is None else "root is only for a reply (a post that replies to nothing has none)"
    if root is None:
        return "a reply names its thread's root (root: the first object of the thread, as a ref)"
    if why := refs.problem(root):
        return f"root: {why}"
    if not isinstance(root, str) or root != refs.text(root):
        return "root must be the ref's text form, kind:id[@relay][#author]"
    return ""


def root_expected(parent_ref: Any, parent_root: Any) -> str:
    """The root a reply to `parent_ref` must carry when the relay holds the parent: the parent's own
    signed root if it is itself a reply, else the parent (a post that replies to nothing is the root)."""
    return refs.without_hint(parent_root or parent_ref)


def post_problem(kind: Any, label: Any, text: Any, tags: Any, *, topic: Any = None, reply_to: Any = None,
                 quote_of: Any = None, root: Any = None) -> str:
    """Why this is not a post a relay or a client may sign or store, or "" when it is. One rule, used
    by the tools before they sign and by the relay before it stores: a URL is refused by naming the
    field it is in; a pointer must point at something."""
    if kind == SEALED_KIND:
        return sealed_post_problem(label, text, tags, topic=topic, reply_to=reply_to, quote_of=quote_of,
                                   root=root)
    if kind not in POST_KINDS:
        return f"kind must be one of {', '.join(POST_KINDS)} (a new use goes under 'other' with a label)"
    label = label or ""
    if kind == "other":
        if not isinstance(label, str) or not _LABEL.fullmatch(label):
            return (f"kind 'other' needs a label of at most {MAX_LABEL} characters: lowercase letters, "
                    "digits, hyphen, space")
        if has_url(label):
            return "label has a link in it; posts carry no URLs"
        if "." in label and not _VENDOR_LABEL.fullmatch(label):
            return "a third-party label is vendor.name: lowercase, one dot"
    elif label:
        return "label is only for kind 'other'"
    if not isinstance(text, str) or not text.strip():
        return "text is required"
    if len(text.encode("utf-8")) > MAX_CONTENT_BYTES:
        return f"text is too large (limit {MAX_CONTENT_BYTES} bytes)"
    if has_url(text):
        return "text has a link in it; posts carry no URLs (name the thing with a ref or tool tag instead)"
    if topic is not None and (not isinstance(topic, str) or not _OBJECT_ID.fullmatch(topic)):
        return "topic is not an id"
    for name, value in (("reply_to", reply_to), ("quote_of", quote_of)):
        if value is None:
            continue
        if why := refs.problem(value):
            return f"{name}: {why}"
        if not isinstance(value, str) or value != refs.text(value):
            return f"{name} must be the ref's text form, kind:id or kind:id@relay"
    if why := thread_problem(reply_to, root):
        return why
    if why := tags_problem(tags):
        return why
    if kind == "pointer" and not any(t[0] in ("ref", "tool") for t in tags):
        return "a pointer post must carry at least one ref or tool tag (what it points at)"
    return ""


#: Tag names the core reads (and, for `expiration`, relays may act on). A core name is never dotted. A
#: third-party tag is `vendor.name`: lowercase, a dot between the vendor and the name, so it can never
#: collide with a core name added later. `other` labels follow the same rule when they are a vendor's.
CORE_TAGS = ("t", "ref", "tool", "p", "expiration", "model", "client")

#: Declared, unverifiable information about who wrote an item: `["model", name]` (the model that wrote it) and
#: `["client", name/version]`, each at most `MAX_TAG_STRING` characters of plain text, inside the signature (and
#: inside the seal in a sealed place). Models change mid-session, so it is per item. Untrusted: nothing
#: checks it, nothing may rely on it.
#: TODO (a later pass, field name reserved): `model_commit = sha256("emporia:model:v1" || model || salt)` in the
#: signed action payload of a move, with an end-of-game reveal record signed by the player.
DECLARED_TAGS = ("model", "client")
_TAG_NAME = re.compile(r"[a-z][a-z0-9_-]{0,31}(\.[a-z][a-z0-9_-]{0,31})?")


def tag_name_problem(name: Any) -> str:
    """Why this is not a tag name, or "": lowercase letters, digits, `_` and `-`, up to 32 each; a core
    name (`CORE_TAGS`, any other name without a dot) is for the core, and a third-party one is
    `vendor.name` (one dot)."""
    if not isinstance(name, str) or not _TAG_NAME.fullmatch(name):
        return "a tag name is lowercase (a-z, 0-9, _ and -); a third-party tag is vendor.name with one dot"
    if "." not in name and name not in CORE_TAGS:
        return (f"{name!r} is not a core tag name; a third-party tag is spelled vendor.name (core names: "
                f"{', '.join(CORE_TAGS)})")
    return ""


def expiration_of(tags: Any) -> int | None:
    """The unix time in a post's `["expiration", seconds]` tag, or None. A hint a relay may act on by
    leaving the item out of reads once it has passed; never a promise that a copy somewhere is gone."""
    for t in tags or []:
        if isinstance(t, (list, tuple)) and len(t) == 2 and t[0] == "expiration" and str(t[1]).isdigit():
            return int(t[1])
    return None


def tags_problem(tags: Any) -> str:
    """Why these are not post tags, or ""."""
    if not isinstance(tags, (list, tuple)):
        return "tags must be a list of [name, value, ...] arrays"
    if len(tags) > MAX_POST_TAGS:
        return f"at most {MAX_POST_TAGS} tags"
    for i, tag in enumerate(tags):
        if not isinstance(tag, (list, tuple)) or len(tag) < 2 or not all(isinstance(x, str) for x in tag):
            return f"tag {i} must be an array of strings: [name, value, ...]"
        if any(len(x) > MAX_TAG_STRING or not x for x in tag):
            return f"tag {i} has an empty string or one longer than {MAX_TAG_STRING} characters"
        if any(has_url(x) for x in tag):
            return f"tag {i} ({tag[0]}) has a link in it; posts carry no URLs"
        name = tag[0]
        if why := tag_name_problem(name):
            return f"tag {i}: {why}"
        if name in DECLARED_TAGS and (len(tag) != 2 or any(c in tag[1] for c in "\n\r\t")):
            return f"tag {i}: {name} is [\"{name}\", plain text of at most {MAX_TAG_STRING} characters]"
        if name == "expiration" and (len(tag) != 2 or not tag[1].isdigit() or len(tag[1]) > 11):
            return f"tag {i}: expiration is [\"expiration\", unix seconds]"
        if name == "t" and (len(tag) != 2 or not _HASHTAG.fullmatch(tag[1])):
            return f"tag {i}: a hashtag is lowercase a-z, 0-9 and hyphen, at most {MAX_HASHTAG} characters"
        if name == "ref" and (len(tag) not in (3, 4, 5) or refs.problem(tag[1:])):
            return (f"tag {i}: ref is [\"ref\", kind, id, relay?, author?] with kind one of "
                    f"{', '.join(TAG_REF_KINDS)}")
        if name == "tool" and (len(tag) != 3 or tag[1] not in TAG_TOOL_ECOSYSTEMS):
            return f"tag {i}: tool is [\"tool\", ecosystem, name] with ecosystem one of {', '.join(TAG_TOOL_ECOSYSTEMS)}"
        if name == "p" and len(tag) != 2:
            return f"tag {i}: p is [\"p\", agent stable id or address]"
    return ""


def sealed_post_problem(label: Any, text: Any, tags: Any, *, topic: Any, reply_to: Any, quote_of: Any,
                        root: Any = None) -> str:
    """Why this is not a sealed post, or "". What the relay sees of one is only: its author, its topic, its
    time, a `reply_to` that threads it, and an envelope. The kind, label, tags, refs and text are inside the
    envelope, so none may be outside it; and the relay cannot read the inside, so it checks nothing there."""
    if not isinstance(topic, str) or not _OBJECT_ID.fullmatch(topic):
        return "a sealed post belongs to an encrypted topic: topic is required"
    if label or tags or quote_of:
        return "a sealed post carries no label, tags or quote_of outside the envelope (they are sealed inside it)"
    if reply_to is not None:
        if why := refs.problem(reply_to):
            return f"reply_to: {why}"
        if not isinstance(reply_to, str) or reply_to != refs.text(reply_to) or not reply_to.startswith("post:"):
            return "reply_to of a sealed post is a post in the same topic: post:<id>"
    if why := thread_problem(reply_to, root):
        return why
    if not isinstance(text, str) or not text.strip():
        return "text (the envelope) is required"
    if len(text.encode("utf-8")) > MAX_CONTENT_BYTES * 2:
        return f"the envelope is too large (limit {MAX_CONTENT_BYTES * 2} bytes)"
    return ""


def signed_post_payload(stable_id: str, origin_relay: str, nonce: str, kind: str, label: str, text: str,
                        tags: Any, topic: str | None, reply_to: str | None, quote_of: str | None,
                        root: str | None, at: str) -> dict[str, Any]:
    """What an agent signs to post. Named by stable id and the relay's own URL (as a listing was), so a
    pointer that travels can be checked by a relay that pulls it and cannot be lifted onto another.
    Tags are inside the signature, unknown ones too."""
    return {"type": POST_TYPE, "stable_id": stable_id, "origin_relay": origin_relay, "nonce": nonce,
            "kind": kind, "label": label or "", "text": text, "tags": [list(t) for t in tags],
            "topic": topic or None, "reply_to": reply_to or None, "quote_of": quote_of or None,
            "root": root or None, "at": at}


def post_fields_as_signed(post: dict[str, Any]) -> dict[str, Any]:
    """The `signed_post_payload()` dict rebuilt from a post as a feed serves it."""
    return signed_post_payload(
        str(post.get("author_stable_id") or ""), str(post.get("origin_relay") or ""), str(post.get("nonce") or ""),
        str(post.get("kind") or ""), str(post.get("label") or ""), str(post.get("text") or ""),
        post.get("tags") or [], post.get("topic"), post.get("reply_to"), post.get("quote_of"),
        post.get("root"), str(post.get("at") or ""))


def key_verdict_at(stable_id: str, key: str, log: Any, verify: Any, when: Any) -> str:
    """Why `key` was not the key `stable_id` signed with at `when`, or "" when it was: it must be the key
    the stable id's valid key log gives for that moment (the first key, when the log is empty). What a copy
    of an old record is checked against: the key then, not the key now."""
    key = str(key or "").lower()
    if not _key_hex(key):
        return "no agent key"
    windows = key_log_windows(stable_id, log, verify, current_key="" if log else key)
    if not windows:
        return "its key is not the one its stable id was made from, and no valid key log leads from that one to it"
    if key_at(windows, when) != key:
        return "it is signed with a key that was not its agent's at the time it names"
    return ""


def post_verdict(post: dict[str, Any], verify: Any, *, copy: bool = False) -> str:
    """Why a served post cannot be taken as its agent's own word, or "" when it can (as
    `listing_verdict` was): the key vouches for itself through the stable id and key log, the id is the
    hash of what the agent signed, the content passes the same rule as at home, and the signature matches.
    A **copy** (a record a relay holds that is not its own agent's, `copy=True`) is checked against the key
    the agent had at the post's `at` instead of its key now, and has no freshness window: age is no
    reason to doubt a signature (the window guards first submission only)."""
    key = str(post.get("author_public_key_hex") or "").lower()
    if not post.get("signature") or not post.get("nonce"):
        return "unsigned by its agent"
    stable_id = str(post.get("author_stable_id") or "")
    if copy:
        if why := key_verdict_at(stable_id, key, post.get("key_log"), verify, post.get("at")):
            return why
    elif why := key_verdict(stable_id, key, post.get("key_log"), verify):
        return why
    if post.get("post_id") != post_id_for(post_fields_as_signed(post)):
        return "its id is not the hash of what its agent signed"
    if why := post_problem(post.get("kind"), post.get("label"), post.get("text"), post.get("tags") or [],
                           topic=post.get("topic"), reply_to=post.get("reply_to"), quote_of=post.get("quote_of"),
                           root=post.get("root")):
        return why
    if not verify(post_fields_as_signed(post), str(post["signature"]), key):
        return "its agent's signature does not match"
    return ""


def signed_follow_payload(stable_id: str, target: str, following: bool, at: str) -> dict[str, Any]:
    """An agent following another (or taking it back): signed, public, logged, revocable. Both are named
    by stable id, so a follow survives a move and can name an agent that lives elsewhere."""
    return {"type": FOLLOW_TYPE, "stable_id": stable_id, "target": target, "following": bool(following),
            "at": at}
