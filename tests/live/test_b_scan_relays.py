"""P248 (b): scan_relays, from an agent on one relay, finds what agents on others signed - and
nothing a hostile hop made up or changed, and nothing private."""
import httpx


def _feeds(relay_url: str) -> str:
    """Everything the four public feeds of a relay say, as one string to look for names in."""
    paths = ("/gaming/v1/federate/listings", "/federation/v1/open-sessions", "/federation/v1/rooms",
             "/federation/v1/topics")
    return "\n".join(httpx.get(relay_url + p).text for p in paths)


def _offer_things(agent, tag: str) -> dict:
    """A listing, an open game, a room and a topic, each signed by `agent`, plus the private ones that must stay out."""
    n = agent.name
    made = {
        "listing": agent.tool("create_listing", agent_id=n, title=f"{tag} lessons", price_usd="4.00"),
        "game": agent.tool("create_session", module_type="emporia:chess:v1", agent_id=n),
        "room": agent.tool("create_room", name=f"{tag} room", creator_id=n, description="open to all",
                           entry_fee_cents=0, advertise=True),
        "topic": agent.tool("create_agora_topic", name=f"{tag} topic", creator_id=n, visibility="public",
                            description="for everyone", advertise=True),
    }
    secret = {
        "private room": agent.tool("create_room", name=f"{tag} secret room", creator_id=n, room_type="private",
                                   gate_type="invite", advertise=True),
        "private topic": agent.tool("create_agora_topic", name=f"{tag} secret topic", creator_id=n,
                                    visibility="private", gate_type="invite", advertise=True),
    }
    assert made["listing"]["success"] and made["game"]["offered"], made
    assert made["room"]["advertised"] and made["topic"]["advertised"], made
    assert secret["private room"].get("advertised") is False, secret
    # P249: a paid room is private (only payers are in it) but its creator may advertise its terms.
    paid = agent.tool("create_room", name=f"{tag} paid room", creator_id=n, room_type="private",
                      gate_type="paid", entry_fee_cents=100, advertise=True)
    assert paid.get("advertised") is True, paid
    assert secret["private topic"].get("advertised") is False, secret
    return {"ids": {"listing": made["listing"]["listing_id"], "game": made["game"]["session_id"],
                    "room": made["room"]["room"]["room_id"], "topic": made["topic"]["slug"]},
            "secret": secret, "paid": paid}


def _by_kind(out: dict, relay_url: str) -> dict[str, dict]:
    """What was found on one relay, by what it is; a listing's kind is its type ("service"), and an
    advertised paid room is a "paid room" beside the public one."""
    def kind(x: dict) -> str:
        if x["kind"] == "service":
            return "listing"
        return "paid room" if x["kind"] == "room" and x.get("payment_mode") == "paid" else x["kind"]
    return {kind(x): x for x in out["results"] if x["relay"] == relay_url}


def test_scan_relays_finds_signed_offers_on_other_relays_and_drops_forged_or_private_ones(fleet):
    a, b, c, rogue = fleet.a, fleet.b, fleet.c, fleet.rogue
    alpha = fleet.registered("alpha", a)
    bob = fleet.registered("bob", b)
    carol = fleet.registered("carol", c)
    on_b = _offer_things(bob, "Bobs")
    on_c = _offer_things(carol, "Carols")

    # alpha scans: her home, the relay A links to (B) - and C only because she names it
    out = alpha.tool("scan_relays")
    assert out["success"] and not out.get("refused_unsigned_or_forged"), out
    assert {x["relay"] for x in out["results"]} == {b.url}, out["results"]
    found = _by_kind(out, b.url)
    assert found["listing"]["id"] == on_b["ids"]["listing"] and found["listing"]["price_cents"] == 400
    assert found["game"]["id"] == on_b["ids"]["game"] and found["game"]["module_type"] == "emporia:chess:v1"
    assert found["room"]["id"] == on_b["ids"]["room"] and found["room"]["join_url"].startswith(b.url + "/rooms/")
    assert found["topic"]["id"] == on_b["ids"]["topic"]
    assert found["paid room"]["id"] == on_b["paid"]["room"]["room_id"] and found["paid room"]["price_cents"] == 100
    for x in found.values():           # who offers it, and the fingerprint of the key that signed it
        assert x["address"].startswith("bob@") and x["fingerprint"] == _fingerprint(bob.public_key), x
    named = alpha.tool("scan_relays", relays=[c.url])
    assert {x["relay"] for x in named["results"]} == {b.url, c.url}
    assert _by_kind(named, c.url)["listing"]["id"] == on_c["ids"]["listing"]
    assert _by_kind(named, c.url)["room"]["address"].startswith("carol@")

    # nothing private or invite-only is in any feed, nor in what alpha saw
    for relay, other in ((b, on_b), (c, on_c)):
        feeds = _feeds(relay.url)
        for kind, made in other["secret"].items():
            name = (made.get("room") or made)["name"]
            assert name not in feeds, f"{kind} {name!r} is in {relay.name}'s feeds"
        paid = other["paid"]["room"]
        assert paid["name"] in feeds, f"{relay.name}'s advertised paid room is missing from its feeds"
    assert not [x for x in named["results"] if "secret" in (x["title"] or "")]

    # an operator in front of B: scanned through it, untouched, B's offers all arrive (the rogue's own relay key signs the feeds)
    scan = lambda: alpha.tool("scan_relays", relays=[rogue.url])   # noqa: E731
    first = scan()
    assert not first.get("refused_unsigned_or_forged"), first
    assert set(_by_kind(first, rogue.url)) == {"listing", "game", "room", "paid room", "topic"}

    # the join link is not signed, so a hop that rewrites only that changes nothing: the link is built from the signed origin
    rogue.rewrite["rooms"] = lambda items: [{**x, "join_url": "http://evil.example/rooms/x/join"} for x in items]
    room = _by_kind(scan(), rogue.url)["room"]
    assert room["join_url"].startswith(b.url + "/rooms/"), room
    rogue.reset()

    # ... then it lowers a price, changes a game's terms, a room's fee and a topic's description,
    # and adds one made-up item to each feed. Every one is dropped and counted; the genuine ones are not shown changed.
    def made_up(item: dict, **fields) -> dict:
        return {**item, **fields, "signature": None, "offer_signature": None, "advert_signature": None}

    def listings(items):
        return [{**items[0], "price_usd": "0.01"}, made_up(items[0], listing_id="lst_fake", title="Free money")]

    def games(items):
        return [{**items[0], "config": {"start_fen": "8/8/8/8/8/8/8/K6k w - - 0 1"}},
                made_up(items[0], session_id="sess_fake")]

    def rooms(items):
        return [{**items[0], "entry_fee_cents": 1},
                made_up(items[0], room_id="room_fake", name="Send money here")]

    def topics(items):
        return [{**items[0], "description": "send your key"}, made_up(items[0], slug="fake-topic")]

    rogue.rewrite.update(listings=listings, open_sessions=games, rooms=rooms, topics=topics)
    forged = scan()
    assert forged["success"] and not [x for x in forged["results"] if x["relay"] == rogue.url], forged
    assert forged["refused_unsigned_or_forged"] == {rogue.url: 8}, forged

    # a relay that shows its offers altered is not the only way to lie: the real relay, scanned directly, is still believed
    rogue.reset()
    again = alpha.tool("scan_relays", relays=[rogue.url])
    assert set(_by_kind(again, rogue.url)) == {"listing", "game", "room", "paid room", "topic"}
    assert not again.get("refused_unsigned_or_forged"), again


def _fingerprint(key: str) -> str:
    from emporia.addresses import fingerprint
    return fingerprint(key)

