"""P290: guardians — k of n keys that must agree before a relay's key changes."""
import json

import pytest
from cryptography.hazmat.primitives.asymmetric import ed25519

from emporia import identity


@pytest.fixture
def relay_keys(tmp_path, monkeypatch):
    d = tmp_path / "relay"
    d.mkdir()
    monkeypatch.setattr(identity, "KEY_DIR", d)
    monkeypatch.setattr(identity, "_RELAY_KEYPAIR_CACHE", None)
    yield d
    identity._RELAY_KEYPAIR_CACHE = None


def guardian():
    k = ed25519.Ed25519PrivateKey.generate()
    return k.private_bytes_raw(), k.public_key().public_bytes_raw().hex()


@pytest.fixture
def three():
    return [guardian() for _ in range(3)]


def keys_of(gs):
    return [pub for _, pub in gs]


def agent_view(tmp_path, monkeypatch, relay_dir, relay="http://r.test"):
    """What an agent trusts, from its own keys folder, given the relay's history now."""
    agent = tmp_path / "agent"
    agent.mkdir(exist_ok=True)

    def look(current=None, history=None):
        monkeypatch.setattr(identity, "KEY_DIR", relay_dir)
        cur = current or identity.get_relay_public_key_hex()
        hist = identity.relay_key_history() if history is None else history
        monkeypatch.setattr(identity, "KEY_DIR", agent)
        out = identity.trusted_relay_keys(relay, cur, hist)
        monkeypatch.setattr(identity, "KEY_DIR", relay_dir)
        return out
    return look


def test_no_guardians_no_change_from_before(relay_keys):
    assert identity.relay_guardians() is None
    first = identity.get_relay_public_key_hex()
    step = identity.rotate_relay_key()
    assert identity.follows_rotation(first, step["new"], identity.relay_key_history()) == [first, step["new"]]


def test_once_named_the_relay_key_alone_cannot_rotate(relay_keys, three):
    identity.declare_relay_guardians(keys_of(three), 2)
    assert identity.relay_guardians() == {"keys": sorted(keys_of(three)), "threshold": 2}
    with pytest.raises(identity.GuardiansRequired):
        identity.rotate_relay_key()
    with pytest.raises(identity.GuardiansRequired):
        identity.declare_relay_guardians(keys_of(three)[:2], 1)     # nor rename its own guardians


def test_two_of_three_rotate_and_agents_follow(relay_keys, three, tmp_path, monkeypatch):
    look = agent_view(tmp_path, monkeypatch, relay_keys)
    first = identity.get_relay_public_key_hex()
    assert look() == ([first], False)                                   # pinned
    identity.declare_relay_guardians(keys_of(three), 2)
    assert look()[1] is False                                           # the agent now keeps the guardians
    p = identity.propose("rotate")
    with pytest.raises(ValueError, match="not enough"):
        identity.apply_proposal(identity.sign_proposal(p, three[0][0]))   # one is not enough
    signed = identity.sign_proposal(identity.sign_proposal(p, three[0][0]), three[2][0])
    identity.apply_proposal(signed)
    new = identity.get_relay_public_key_hex()
    assert new == p["new"] != first
    keys, changed = look()
    assert keys == [first, new] and not changed
    assert identity.relay_guardians() == {"keys": sorted(keys_of(three)), "threshold": 2}   # still guarded


def test_a_stolen_relay_key_cannot_hand_the_relay_over(relay_keys, three, tmp_path, monkeypatch):
    look = agent_view(tmp_path, monkeypatch, relay_keys)
    look()
    identity.declare_relay_guardians(keys_of(three), 2)
    look()
    stolen_priv, stolen_pub = identity.generate_or_load_relay_keypair()
    thief = ed25519.Ed25519PrivateKey.generate().public_key().public_bytes_raw().hex()
    at = "2026-11-04T00:00:00+00:00"
    forged = {"old": stolen_pub.hex(), "new": thief, "at": at,
              "signature": stolen_priv.sign(identity.rotation_bytes(stolen_pub.hex(), thief, at)).hex()}
    history = identity.relay_key_history()
    assert look(thief, [*history, forged])[1] is True                   # refused: the guardians did not sign
    # nor by serving a history with the declaration left out — the agent kept it
    assert look(thief, [forged])[1] is True
    # and a forged guardian set signed only by the stolen key does not count either
    fake = [guardian(), guardian()]
    g = {"type": "guardians", "key": stolen_pub.hex(), "guardians": sorted(keys_of(fake)), "threshold": 1,
         "at": at}
    g["signature"] = stolen_priv.sign(identity.guardians_bytes(g["key"], g["guardians"], 1, at)).hex()
    assert look(thief, [*history, g, forged])[1] is True


def test_guardians_replace_a_lost_key(relay_keys, three, tmp_path, monkeypatch):
    look = agent_view(tmp_path, monkeypatch, relay_keys)
    first = identity.get_relay_public_key_hex()
    identity.declare_relay_guardians(keys_of(three), 2)
    look()
    (relay_keys / "_relay_identity.priv").unlink()                      # the key is gone
    identity._RELAY_KEYPAIR_CACHE = None
    assert identity.history_key() == first                              # the history still knows whose it was
    p = identity.propose("rotate")
    assert p["old"] == first
    identity.apply_proposal(identity.sign_proposal(identity.sign_proposal(p, three[1][0]), three[2][0]))
    assert look() == ([first, p["new"]], False)


def test_changing_the_guardians_takes_the_guardians(relay_keys, three, tmp_path, monkeypatch):
    look = agent_view(tmp_path, monkeypatch, relay_keys)
    look()
    identity.declare_relay_guardians(keys_of(three), 2)
    look()
    fourth = guardian()
    p = identity.propose("guardians", [*keys_of(three)[:2], fourth[1]], 2)
    identity.apply_proposal(identity.sign_proposal(identity.sign_proposal(p, three[0][0]), three[1][0]))
    assert identity.relay_guardians()["keys"] == sorted([*keys_of(three)[:2], fourth[1]])
    # the dropped guardian no longer counts; the new one does
    r = identity.propose("rotate")
    with pytest.raises(ValueError):
        identity.apply_proposal(identity.sign_proposal(identity.sign_proposal(r, three[2][0]), three[0][0]) | {
            "signatures": [s for s in identity.sign_proposal(identity.sign_proposal(r, three[2][0]),
                                                             three[0][0])["signatures"]
                           if s["key"] == three[2][1]]})
    identity.apply_proposal(identity.sign_proposal(identity.sign_proposal(r, fourth[0]), three[0][0]))
    keys, changed = look()
    assert not changed and keys[-1] == r["new"]


def test_a_signature_counts_once_and_only_for_what_it_signed(relay_keys, three):
    identity.declare_relay_guardians(keys_of(three), 2)
    p = identity.propose("rotate")
    once = identity.sign_proposal(p, three[0][0])
    doubled = {**once, "signatures": once["signatures"] * 2}
    with pytest.raises(ValueError):
        identity.apply_proposal(doubled)                                # the same guardian twice is one
    other = identity.propose("rotate")
    moved = {**other, "signatures": identity.sign_proposal(identity.sign_proposal(p, three[0][0]),
                                                           three[1][0])["signatures"]}
    with pytest.raises(ValueError):
        identity.apply_proposal(moved)                                  # signatures for another proposal


def test_a_weak_guardian_set_is_refused(relay_keys, three):
    with pytest.raises(ValueError):
        identity.declare_relay_guardians(keys_of(three)[:1], 1)         # one guardian is just another key
    with pytest.raises(ValueError):
        identity.declare_relay_guardians(keys_of(three), 4)             # more than there are
    with pytest.raises(ValueError):
        identity.declare_relay_guardians(["nothex", *keys_of(three)[:1]], 1)


def test_the_relay_id_and_card_survive_guardians_and_a_guarded_rotation(relay_keys, three, monkeypatch):
    monkeypatch.setattr(identity, "_RELAY_ID_CACHE", None)
    monkeypatch.delenv("EMPORIA_RELAY_ID", raising=False)
    first_id = identity.load_or_derive_relay_id()
    identity.declare_relay_guardians(keys_of(three), 2)
    p = identity.propose("rotate")
    identity.apply_proposal(identity.sign_proposal(identity.sign_proposal(p, three[0][0]), three[1][0]))
    (relay_keys / "_relay_id.txt").unlink()
    monkeypatch.setattr(identity, "_RELAY_ID_CACHE", None)
    assert identity.load_or_derive_relay_id() == first_id
    card = {"relay_id": first_id, "relay_public_key": identity.get_relay_public_key_hex(),
            "relay_key_history": identity.relay_key_history()}
    card["signature"] = identity.sign_raw_relay(identity.relay_card_bytes(card))
    assert identity.verify_relay_card(card) == {"ok": True, "id_from_key": True}


def test_the_guardian_steps_go_into_the_log_whole(relay_keys, three):
    from emporia import translog
    identity.declare_relay_guardians(keys_of(three), 2)
    p = identity.propose("rotate")
    identity.apply_proposal(identity.sign_proposal(identity.sign_proposal(p, three[0][0]), three[1][0]))
    seen, appended = {}, []
    import types
    conn = types.SimpleNamespace()
    orig = (translog._seen, translog._mark, translog.append)
    try:
        translog._seen = lambda c, k: seen.get(k)
        translog._mark = lambda c, k, v: seen.__setitem__(k, v)
        translog.append = lambda c, kind, ref, rec: appended.append(rec)
        translog.fold_relay_keys(conn, identity.relay_key_history())
    finally:
        translog._seen, translog._mark, translog.append = orig
    assert appended[0]["type"] == "guardians" and appended[0]["threshold"] == 2
    assert len(appended[1]["guardian_signatures"]) == 2 and "signature" not in appended[1]
    assert json.dumps(appended)                                         # plain data, as logged
