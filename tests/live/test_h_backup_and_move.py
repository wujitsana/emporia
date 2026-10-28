"""P248 (h): an agent names a backup relay, then moves home to it; its old relay says where it went, its contacts
find it, and the record its old relay signed travels with it as that relay's word."""
import httpx

from emporia import moves
from emporia.identity import federation_signable_bytes, verify_raw
from tests.live.harness import play_fools_mate


def test_an_agent_adds_a_backup_then_moves_home_and_its_record_travels_as_its_old_relays_word(fleet):
    a, b = fleet.a, fleet.b
    alfred = fleet.registered("alfred", a)
    alpha = fleet.registered("alpha", a, EMPORIA_BACKUP_RELAYS=b.url)
    bob = fleet.registered("bob", b)
    old_address = f"alpha@{a.host}"

    # `emporia relay backup B alpha`: she registers on B with the same key and her signed relay list names it
    added = alpha.call("t = m._RELAY_CTX.set(m._backups()[0])\nr1 = m.register_agent(m.AGENT_ID)\n"
                       "m._RELAY_CTX.reset(t)\nr = [r1, m._publish_relay_list(m.AGENT_ID)]")
    assert added[0]["success"] and "ok" in added[1], added
    assert httpx.get(f"{b.url}/agents/alpha/card").json()["card"]["public_key_hex"] == alpha.public_key

    # she plays on A (and loses), so A has a record of her to sign; bob looks her up from B
    sid = play_fools_mate(alpha, alfred)
    assert alpha.tool("verify_session", session_id=sid)["verdict"] == "ok"
    seen = bob.tool("agent_card", agent=old_address)
    assert seen["verified"] and seen["card"]["relay_list"]["backups"] == [b.url], seen
    record_on_a = httpx.get(f"{a.url}/agents/alpha/record").json()
    assert record_on_a["relay"] == a.url and record_on_a["games"]["lost"] == 1, record_on_a

    # `emporia relay move B alpha`: a process with B as its home, told where she came from
    alpha.restart(home=b.url)
    moved = alpha.tool("move_home", old=a.url)
    assert moved["success"] and moved["old_relay"] == "answering", moved
    assert moved["record_carried"] is True and moved["old_relay_told"] is True, moved

    # A's card now says she moved, and A signs that; her own signed list on B names B as home
    card = httpx.get(f"{a.url}/agents/alpha/card").json()
    assert card["card"]["moved_to"] == f"alpha@{b.host}" and card["card"]["relay_list"]["home"] == b.url
    assert verify_raw(federation_signable_bytes(a.url, "agent-card", [card["card"]]), card["signature"],
                      a.health()["relay_public_key"])
    here = bob.tool("agent_card", agent="alpha")
    assert here["verified"] and here["card"]["relay_list"]["home"] == b.url and here["card"]["moved_to"] == "", here
    assert here["card"]["public_key_hex"] == seen["card"]["public_key_hex"]      # the same agent

    # contacts find her: a DM to her old address is told where she went; at her new home it arrives
    old_way = bob.tool("send_dm", to_agent_id=old_address, content="are you still on A?")
    assert not old_way["success"] and f"alpha@{b.host}" in str(old_way), old_way
    assert alpha.tool("set_dm_policy", other_relays=True)["success"]
    assert bob.tool("send_dm", to_agent_id="alpha", content="found you on B")["success"]
    got = alpha.tool("get_dm_messages", thread_id=next(t["thread_id"] for t in alpha.tool("list_dm_threads")))
    assert [(m["sender_id"], m["content"], m["verified"]) for m in got["messages"]] == [("bob", "found you on B", True)]

    # her record on B: B's own count is separate; A's is shown as A's signed word, and it checks against A's key
    shown = bob.tool("counterparty_record", agent_id="alpha")
    assert shown["success"] and shown["relay"] == b.url and shown["games"]["lost"] == 0, shown
    [elsewhere] = shown["elsewhere"]
    assert elsewhere["relay"] == a.url and elsewhere["games"]["lost"] == 1
    unsigned = (*moves.RECORD_UNSIGNED, "relay_key_fingerprint")
    assert verify_raw(federation_signable_bytes(a.url, "record", [{k: v for k, v in elsewhere.items()
                                                                    if k not in unsigned}]),
                      elsewhere["signature"], a.health()["relay_public_key"])

    # a record changed on the way is not accepted as A's word
    forged = {**record_on_a, "games": {**record_on_a["games"], "lost": 0, "won": 50}}
    status = alpha.call("r = m._bearer_request('post', m.RELAY_URL + '/agents/alpha/carried-record',"
                        f" agent_id='alpha', json={{'record': {forged!r}}}, timeout=10).status_code")
    assert status == 422
