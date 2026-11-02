"""P248 (a): a seller's signed listing travels from relay to relay, and a hostile hop cannot change it."""
import httpx

from emporia import identity, wire


def _pulled(relay) -> list[dict]:
    """The listings a relay shows that it got from another relay."""
    return [x for x in httpx.get(f"{relay.url}/listings").json()["listings"] if x.get("federated_from")]


def test_a_signed_listing_travels_and_a_hostile_hop_cannot_change_or_invent_one(fleet):
    a, b, victim, rogue = fleet.a, fleet.b, fleet.victim, fleet.rogue
    alpha = fleet.registered("alpha", a)
    bob = fleet.registered("bob", b)
    assert alpha.tool("create_listing", agent_id="alpha", title="Chess lessons", price_usd="3.00",
                      description="an hour, over chat")["success"]

    # B pulls from A, the way its timer does; bob then sees the listing exactly as alpha signed it
    record = b.sync()["peers"][a.url]
    assert record["ok"] and record["imported"] == 1, record
    shown = bob.tool("list_listings")
    seen = [x for x in shown["listings"] if x["title"] == "Chess lessons"]
    assert len(seen) == 1 and seen[0]["federated_from"] == a.url and "refused_federated" not in shown, shown
    assert seen[0]["agent_public_key_hex"] == alpha.public_key         # alpha's key, not a relay's
    assert wire.listing_verdict(seen[0], identity.verify) == ""        # and her signature checks, here, in the test
    assert float(seen[0]["price_usd"]) == 3.0

    # An operator in front of B signs B's feed with a relay key of its own (a valid batch signature) and
    # lowers bob's price, invents a listing, and adds one "signed" by a key that is not the seller's.
    assert bob.tool("create_listing", agent_id="bob", title="Go lessons", price_usd="5.00")["success"]
    thief = identity.ed25519.Ed25519PrivateKey.generate()

    def tamper(items: list[dict]) -> list[dict]:
        cheaper = [{**x, "price_usd": "0.01"} for x in items]
        made_up = {**items[0], "listing_id": "lst_made_up", "title": "Free money", "signature": None}
        forged = {**items[0], "listing_id": "lst_forged", "title": "Send me your key",
                  "signature": identity.sign_payload(thief, {"not": "the listing"})}
        return [*cheaper, made_up, forged]

    rogue.rewrite["listings"] = tamper
    rogue.claim_origin = True        # P264: a hop may speak only for itself, so it names itself
    record = victim.sync()["peers"][rogue.url]
    assert record["ok"] and record["imported"] == 0, record            # the batch itself was fine ...
    assert record["refused"] == 3, record                              # ... every listing in it was not
    assert set(record["refused_why"]) == {"its agent's signature does not match", "unsigned by its agent",
                                          "its id is not the one its agent's nonce makes"}, record
    assert _pulled(victim) == []

    # P264: the same hop, no longer tampering, is still not believed about B's listings. Naming
    # itself, bob's (signed for B) is "put up on another relay"; naming B, the batch is not its to send.
    rogue.rewrite.clear()
    record = victim.sync()["peers"][rogue.url]
    assert record["imported"] == 0 and record["refused_why"] == {
        "it was put up on another relay than the one serving it": 1}, record
    rogue.reset()
    record = victim.sync()["peers"][rogue.url]
    assert not record["ok"] and "not the relay linked" in record["error"], record
    assert _pulled(victim) == []        # (a listing pulled from its own relay is the first half above)
