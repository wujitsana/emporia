"""What a room or topic may be named as to other relays.

Adverts (a creator's signed advert, served to linked relays and pulled by them) are gone: a room or topic
is found from other relays by a pointer post (`emporia:post:v2`, kind `pointer`, a `ref` tag naming it).
What a pointer may name is decided here, once: a public open room, a paid room (however it is listed), a
public topic that is not invite-only. A private invite-only room or topic is never named to anyone who is
not in it.

Nothing is stored or pulled: only these two predicates remain.
"""

from __future__ import annotations


def room_may_be_advertised(room_type: str, gate_type: str) -> bool:
    """A public open room, or a paid one — which is `private` because only those
    who paid are in it, but whose terms its creator may choose to show. An
    invite-only room is never shown."""
    return gate_type == "paid" or (room_type == "public" and gate_type == "open")


def topic_may_be_advertised(visibility: str, gate_type: str) -> bool:
    return visibility == "public" and gate_type != "invite"



def topic_may_be_shared(visibility: str, gate_type: str, encrypted: bool = False, entry_fee_cents: int = 0,
                        is_deleted: bool = False) -> bool:
    """A topic other relays may carry (cross-relay shared topics): public, free, open to anyone, not sealed and
    not removed. Stricter than `topic_may_be_advertised`: a paid, invite-only, private or encrypted topic is
    never shared, and one that becomes any of those stops being shared at once (the feed, the inbox and the
    carrier's pull all ask this one function)."""
    return (not is_deleted and visibility == "public" and gate_type == "open" and not encrypted
            and not entry_fee_cents)


#: The only things a post that crosses may hang from: another post, or a topic. A room, a message in one, a game
#: or a lab thread is closed to people who are not in it, so its id never travels.
CROSSING_REF_KINDS = ("post", "topic")
CLOSED_REF_KINDS = ("room", "message", "session", "lab", "gh_issue", "gh_pr", "challenge")


def post_may_cross(kind: str, reply_to: str | None, quote_of: str | None, root: str | None,
                   tags: list | None = None) -> str:
    """Why a post may not travel to or from another relay, or "": sealed posts never cross, and neither does a
    post that names a room, a message, a game or a lab thread (as what it replies to, quotes, or roots in, or as a
    ref tag). Shared by the carrier before it sends and the host on arrival."""
    from emporia import refs
    if kind == "sealed":
        return "a sealed post never leaves the relay it was made on"
    for name, ref in (("reply_to", reply_to), ("quote_of", quote_of), ("root", root)):
        if not ref:
            continue
        try:
            k = refs.parse(ref)["kind"]
        except ValueError:
            return f"{name} is not a ref"
        if k not in CROSSING_REF_KINDS:
            return f"{name} names a {k}: only posts and topics may be named in a post that crosses relays"
    for t in tags or []:
        if t and t[0] == "ref" and len(t) > 1 and t[1] in CLOSED_REF_KINDS:
            return f"a ref tag names a {t[1]}, which is closed to people who are not in it: not in a post that crosses relays"
    return ""
