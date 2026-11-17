"""Shared topics on three real relays (the plan's pass criteria C1, C2, C3, C8, C9, C10, C11 in part):
A hosts `town`, B carries it, C is linked to both but carries nothing.

Each relay is a process with its own database and keys; agents act through the MCP tools, as in the DM tests."""
from __future__ import annotations

import json
import sqlite3
import time

import httpx
import pytest

from tests.test_federated_dms import _free_port
from tests.test_moving import _as, _start

SYNC = {"EMPORIA_FEDERATION_SYNC_INTERVAL_SECONDS": "1"}


def _wait(fn, what: str, seconds: float = 40.0):
    end = time.time() + seconds
    while time.time() < end:
        got = fn()
        if got:
            return got
        time.sleep(0.5)
    pytest.fail(f"timed out waiting for {what}")


@pytest.fixture
def three(tmp_path):
    pa, pb, pc = _free_port(), _free_port(), _free_port()
    a, b, c = (f"http://127.0.0.1:{p}" for p in (pa, pb, pc))
    procs = []
    try:
        procs.append(_start(tmp_path, pa, {**SYNC, "FEDERATED_RELAYS": f"{b},{c}", "EMPORIA_SHARED_TOPICS": "town",
                                            "EMPORIA_DIRECTORY": "1", "EMPORIA_DIRECTORY_LISTED": "1"}))
        procs.append(_start(tmp_path, pb, {**SYNC, "FEDERATED_RELAYS": f"{a},{c}", "EMPORIA_CARRY_TOPICS": f"town@127.0.0.1:{pa}",
                                            "EMPORIA_DIRECTORY_LISTED": "1"}))
        procs.append(_start(tmp_path, pc, {**SYNC, "FEDERATED_RELAYS": f"{a},{b}"}))      # linked, not carrying, not listed
        yield tmp_path, (pa, a), (pb, b), (pc, c)
    finally:
        for p in procs:
            if p.poll() is None:
                p.terminate()
                p.wait(timeout=10)


def _topic_posts(tmp, who, relay, key):
    r = _as(tmp, who, relay, f"r = m.read_feed(topic={key!r}, scope='readable', limit=50)")
    return r.get("posts", []) if r.get("success") else []


def _thread(tmp, who, relay, root_id):
    r = _as(tmp, who, relay, f"r = m.read_thread('post:{root_id}', limit=50)")
    return r.get("posts", []) if r.get("success") else []


def _count(tmp, port, sql, *args):
    return sqlite3.connect(tmp / f"r{port}" / "r.sqlite3").execute(sql, args).fetchone()[0]


def test_a_reply_crosses_and_every_relay_agrees(three):
    tmp, (pa, a), (pb, b), (pc, c) = three
    ha, hb = f"127.0.0.1:{pa}", f"127.0.0.1:{pb}"
    key = f"town@{ha}"
    for name, relay in (("alice", a), ("bob", b), ("cara", c)):
        assert _as(tmp, name, relay, f"r = m.register_agent({name!r})")["success"]
    # the host made `town` itself when it started (EMPORIA_SHARED_TOPICS names it; no agent owns it yet)
    listed = _as(tmp, "alice", a, "r = m.list_agora_topics()")
    assert "town" in json.dumps(listed), listed
    opening = _as(tmp, "alice", a, "r = m.post('first words in town', topic='town')")
    assert opening["success"], opening

    # B pulls the topic: bob sees it as town@A, with whose it is
    _wait(lambda: any(p["post_id"] == opening["post_id"] for p in _topic_posts(tmp, "bob", b, key)), "B to carry the topic")
    topics = {t["slug"]: t for t in _as(tmp, "bob", b, "r = m.list_agora_topics()")["topics"]}
    assert topics[key]["hosted_by"] == ha and topics[key]["carried_by"] == hb and topics[key]["shared"]

    # C1: bob replies from home; it is on A and on B, as bob@B, signature intact; C holds nothing
    reply = _as(tmp, "bob", b, f"r = m.post('bob replies from B', topic={key!r}, reply_to='post:{opening['post_id']}')")
    assert reply["success"], reply
    assert reply["author_id"] == f"bob@{hb}"
    root = opening["post_id"]
    on_a = _wait(lambda: [p for p in _thread(tmp, "alice", a, root) if p["post_id"] == reply["post_id"]], "the reply on A")
    assert on_a[0]["author_id"] == f"bob@{hb}"
    on_b = [p for p in _thread(tmp, "bob", b, root) if p["post_id"] == reply["post_id"]]
    assert on_b and on_b[0]["author_id"] == f"bob@{hb}"
    assert _count(tmp, pc, "SELECT COUNT(*) FROM posts WHERE topic IS NOT NULL") == 0
    assert _count(tmp, pc, "SELECT COUNT(*) FROM agora_topics WHERE slug LIKE '%@%'") == 0
    # the same signed bytes on both: the post as A holds it verifies on B (same id and signature)
    row_a = sqlite3.connect(tmp / f"r{pa}" / "r.sqlite3").execute(
        "SELECT signature, topic, signed_topic, author_id FROM posts WHERE post_id = ?", (reply["post_id"],)).fetchone()
    row_b = sqlite3.connect(tmp / f"r{pb}" / "r.sqlite3").execute(
        "SELECT signature, topic, signed_topic, author_id FROM posts WHERE post_id = ?", (reply["post_id"],)).fetchone()
    assert row_a[0] == row_b[0] and row_a[3] == row_b[3] == f"bob@{hb}"
    assert row_a[1] == "town" and row_a[2] == key and row_b[1] == key

    # C2: alice answers on A; after one sync the thread reads the same on both
    second = _as(tmp, "alice", a, f"r = m.post('alice answers bob', topic='town', reply_to='post:{reply['post_id']}')")
    assert second["success"], second
    _wait(lambda: any(p["post_id"] == second["post_id"] for p in _thread(tmp, "bob", b, root)), "alice's reply on B")
    ids_a = sorted(p["post_id"] for p in _thread(tmp, "alice", a, root))
    ids_b = sorted(p["post_id"] for p in _thread(tmp, "bob", b, root))
    assert ids_a == ids_b and len(ids_a) == 2        # the two replies; the opening is the thread's root

    # C8: nothing counts. alice's standing on A moved only by her own posts; bob has no record on A
    assert _count(tmp, pa, "SELECT COUNT(*) FROM posts WHERE federated_from IS NULL AND author_id LIKE 'bob%'") == 0
    assert _count(tmp, pa, "SELECT COUNT(*) FROM authorized_agents WHERE agent_id LIKE 'bob%'") == 0

    # C3: a topic the relay made has no agent owner; its operator hands it to alice (a direct write, as an operator
    # would with the relay's own tools), and alice, the topic's owner, removes bob's reply on A: gone on A, gone on B after a sync, markers on both
    with sqlite3.connect(tmp / f"r{pa}" / "r.sqlite3") as conn:
        conn.execute("UPDATE agora_topics SET creator_id = 'alice' WHERE slug = 'town'")
        conn.execute("INSERT OR REPLACE INTO agora_members (topic_id, agent_id, role, joined_at) "
                     "SELECT topic_id, 'alice', 'owner', created_at FROM agora_topics WHERE slug = 'town'")
    # (a moderator's removal is the person's: an untracked turn would count as unattended)
    gone = _as(tmp, "alice", a, "import os; os.environ['EMPORIA_UNTRACKED_TURNS'] = 'person'\n"
                                f"r = m.delete_item('post', {reply['post_id']!r})")
    assert gone["success"], gone
    _wait(lambda: _count(tmp, pb, "SELECT is_deleted FROM posts WHERE post_id = ?", reply["post_id"]) == 1, "the removal on B")
    for port in (pa, pb):
        assert _count(tmp, port, "SELECT COUNT(*) FROM deletions WHERE kind = 'post' AND item = ?", reply["post_id"]) == 1
        assert _count(tmp, port, "SELECT text FROM posts WHERE post_id = ?", reply["post_id"]) == ""

    # C9: A's directory lists itself and B (listed), not C (linked, not listed); each card verifies
    d = httpx.get(f"{a}/directory/v1/relays", timeout=10).json()
    ids = {x["relay_url"] for x in d["relays"]}
    assert ids == {a, b}, ids
    card_a = next(x for x in d["relays"] if x["relay_url"] == a)
    assert [t["slug"] for t in card_a["shared_topics"]] == ["town"] and card_a["listed"] is True
    from emporia import identity
    assert all(identity.verify_relay_card(x)["ok"] for x in d["relays"])
    # and an agent's scan over the directory finds the shared topic with its host's relay_id
    r = _as(tmp, "cara", c, f"r = m.scan_relays(relays=[{a!r}], kind='topic')")
    assert r["success"], r
    found = [x for x in r["results"] if x["id"] == key]
    assert found and found[0]["relay_id"] == card_a["relay_id"] and found[0]["hosted_by"] == ha

    # C10: pointer sync between new relays still works (the feed answers, signed), and C is not left out of it
    assert httpx.get(f"{c}/federation/v1/posts", timeout=10).json()["origin_relay"] == c
    peers = httpx.get(f"{b}/federation/peers", timeout=10).json()
    assert peers["shared_topics"]["carried"][0]["topic"] == key
    assert all(p["ok"] is not False for p in peers["peers"] if p["url"] == a)
    cards = httpx.get(f"{a}/relay/card", timeout=10).json()
    assert cards["version"] == 4 and cards["protocol"] == 4
    json.dumps(cards)
