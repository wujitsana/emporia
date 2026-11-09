"""A sealed message's recorded time is floored to the minute. A message signed with a new key in the same minute
the key changed reads as earlier than the change, so it must still verify (against the key that was held
at some moment of that minute), while one a minute later or earlier than the span must not borrow a key."""
from cryptography.hazmat.primitives.asymmetric import ed25519

from emporia import identity
from emporia.wire import signed_dm_thread_payload, signed_room_message_payload

ROTATED_AT = "2026-11-09T05:21:42.617743+00:00"


def _key():
    priv = ed25519.Ed25519PrivateKey.generate()
    return priv, priv.public_key().public_bytes_raw().hex()


def _peer():
    old_priv, old = _key()
    new_priv, new = _key()
    peer = identity.SigningKey(new, [{"key": old, "from": "", "until": ROTATED_AT},
                                     {"key": new, "from": ROTATED_AT, "until": ""}])
    return old_priv, new_priv, old, new, peer


def _dm(priv, created_at):
    payload = signed_dm_thread_payload("dmx_t", "sealed-envelope", "sealed", "n1")
    return {"content": "sealed-envelope", "msg_type": "sealed", "nonce": "n1",
            "signature": identity.sign_payload(priv, payload), "created_at": created_at}


def test_a_message_in_the_minute_of_a_key_change_verifies_with_the_new_key():
    _, new_priv, _, _, peer = _peer()
    assert identity.verify_dm_message("dmx_t", _dm(new_priv, "2026-11-09T05:21:00+00:00"), peer) is True


def test_a_message_in_the_minute_of_a_key_change_still_verifies_with_the_old_key():
    old_priv, _, _, _, peer = _peer()
    assert identity.verify_dm_message("dmx_t", _dm(old_priv, "2026-11-09T05:21:00+00:00"), peer) is True


def test_the_old_key_signs_nothing_recorded_after_that_minute():
    old_priv, new_priv, _, _, peer = _peer()
    assert identity.verify_dm_message("dmx_t", _dm(old_priv, "2026-11-09T05:22:00+00:00"), peer) is False
    assert identity.verify_dm_message("dmx_t", _dm(new_priv, "2026-11-09T05:20:00+00:00"), peer) is False


def test_an_exact_time_still_picks_one_key():
    old_priv, new_priv, _, _, peer = _peer()
    assert identity.verify_dm_message("dmx_t", _dm(new_priv, "2026-11-09T05:21:50.5+00:00"), peer) is True
    assert identity.verify_dm_message("dmx_t", _dm(old_priv, "2026-11-09T05:21:50.5+00:00"), peer) is False


def test_rooms_follow_the_same_rule():
    _, new_priv, _, _, peer = _peer()
    payload = signed_room_message_payload("room_1", "sealed-envelope", "sealed", "n1", "")
    msg = {"content": "sealed-envelope", "msg_type": "sealed", "nonce": "n1",
           "signature": identity.sign_payload(new_priv, payload), "created_at": "2026-11-09T05:21:00+00:00"}
    assert identity.verify_room_message("room_1", msg, peer) is True
