"""P248 (i): an agent's home relay disappears; a contact who had looked her up still finds her card through the
backup relay her own signed list named - and only a card that is hers, unchanged."""


def _backup(agent, relay_url: str) -> None:
    """`emporia relay backup URL NAME`: she registers on that relay with the same key and republishes her list."""
    done = agent.call(f"t = m._RELAY_CTX.set({relay_url!r})\nr1 = m.register_agent(m.AGENT_ID)\n"
                      "m._RELAY_CTX.reset(t)\nr = [r1, m._publish_relay_list(m.AGENT_ID)]")
    assert done[0]["success"] and "ok" in done[1], done


def test_a_contact_finds_an_agents_card_through_its_backup_when_its_home_relay_is_gone(fleet):
    a, b, c, rogue = fleet.a, fleet.b, fleet.c, fleet.rogue
    # alpha's backup is B; dora's is B too, but reached at the address a hostile operator fronts for it
    alpha = fleet.registered("alpha", a, EMPORIA_BACKUP_RELAYS=b.url)
    dora = fleet.registered("dora", a, EMPORIA_BACKUP_RELAYS=rogue.url)
    _backup(alpha, b.url)
    _backup(dora, rogue.url)
    bob, carol = fleet.registered("bob", b), fleet.registered("carol", c)

    # bob looks both up while A is up: he keeps the key and the relay list each signed
    for name in ("alpha", "dora"):
        seen = bob.tool("agent_card", agent=f"{name}@{a.host}")
        assert seen["verified"] and seen["card"]["relay_list"]["home"] == a.url, seen
    alpha_key, dora_key = alpha.public_key, dora.public_key

    a.stop()                                                    # A is gone, unannounced
    assert a.proc.poll() is not None

    # bob still finds alpha's card, from the backup, signed by alpha's own key
    found = bob.tool("agent_card", agent=f"alpha@{a.host}")
    assert found["success"] and found["verified"] and found["found_on"] == b.url, found
    assert found["card"]["public_key_hex"] == alpha_key and found["card"]["relay_list"]["home"] == a.url

    # a stranger who never looked her up has nothing to go on: the backup is learned from her signed list
    nobody = carol.tool("agent_card", agent=f"alpha@{a.host}")
    assert not nobody["success"], nobody

    # dora's backup address leads through the hostile operator. A card with another key, or one whose relay list
    # was changed (her signature no longer matches), is not hers and is not accepted.
    assert bob.tool("agent_card", agent=f"dora@{a.host}")["found_on"] == rogue.url
    rogue.swap_signing_keys = True
    swapped = bob.tool("agent_card", agent=f"dora@{a.host}")
    assert not swapped["success"] and "none of the relays it listed" in swapped["error"], swapped
    rogue.reset()
    rogue.rewrite_card = lambda card: {**card, "relay_list": {**card["relay_list"], "home": "http://evil.example"}}
    changed = bob.tool("agent_card", agent=f"dora@{a.host}")
    assert not changed["success"], changed
    rogue.reset()
    again = bob.tool("agent_card", agent=f"dora@{a.host}")
    assert again["success"] and again["card"]["public_key_hex"] == dora_key and again["found_on"] == rogue.url
