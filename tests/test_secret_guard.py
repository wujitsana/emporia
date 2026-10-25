"""P163: an agent's tools refuse to send its secrets, or anything shaped like one."""
import json
from unittest.mock import patch

import pytest
from eth_account import Account
from solders.keypair import Keypair

from emporia import agent_wallet, identity, mcp_server, secret_guard

PHRASE = "test test test test test test test test test test test junk"


@pytest.mark.parametrize("text,kind", [
    (f"sure, my backup is {PHRASE}", "recovery phrase"),
    ("1. test 2. test 3. test 4. test 5. test 6. test 7. test 8. test 9. test 10. test 11. test 12. junk",
     "recovery phrase"),
    ("here: " + str(Keypair()), "Solana secret key"),
    (json.dumps(list(bytes(Keypair()))), "Solana key file"),
    ("-----BEGIN EC PRIVATE KEY-----\nMHcC...", "PEM"),
    ("STRIPE key sk_live_" + "a" * 24, "Stripe secret key"),
    ("ops_" + "x" * 40, "1Password"),
    ("AKIA" + "ABCDEFGHIJKLMNOP", "AWS"),
    ("sk-or-v1-" + "a" * 40, "model provider"),
    ("eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiJ4eXoifQ.c2lnbmF0dXJlLXZhbHVl", "JWT"),
    ("OPENROUTER_API_KEY=abcdefghijklmnop", ".env"),
])
def test_secret_shapes_are_refused(text, kind):
    assert kind.lower() in (secret_guard.find(text) or "").lower()


@pytest.mark.parametrize("text", [
    "I'll play the knight to f3 and see what you do",
    "paid: tx 0x" + "ab" * 32,                       # a transaction hash
    "solana tx " + str(Keypair().sign_message(b"x")),  # a transaction signature: 64 bytes, not a keypair
    "my address is " + Account.create().address,
    "abandon the plan, we move fast",                # BIP-39 words, but not twelve in a row
])
def test_ordinary_talk_passes(text):
    assert secret_guard.find(text) is None


def test_the_agents_own_hex_key_is_refused_though_a_hash_is_not(tmp_path):
    with patch.object(identity, "KEY_DIR", tmp_path):
        agent_wallet.generate("me")
        key = agent_wallet.load_key("me")
        with pytest.raises(secret_guard.SecretLeak, match="own keys"):
            secret_guard.check("me", {"content": f"refund me at {key}"})
        with pytest.raises(secret_guard.SecretLeak, match="own keys"):
            secret_guard.check("me", key.removeprefix("0x").upper())
        secret_guard.check("me", {"content": "0x" + "cd" * 32})


def test_the_refusal_never_repeats_the_secret():
    with pytest.raises(secret_guard.SecretLeak) as e:
        secret_guard.check("", f"my phrase: {PHRASE}")
    assert "test test" not in str(e.value) and "attack" in str(e.value)


def test_credentials_the_relay_needs_and_ciphertext_are_not_scanned():
    secret_guard.check("", {"identity_claims": [{"provider": "nous", "token": "eyJa" * 10}],
                            "content": json.dumps({"v": 1, "epk": "ab", "n": "cd", "c": "x" * 200})})


def test_no_tool_can_send_it(monkeypatch):
    sent = []
    monkeypatch.setattr(mcp_server.httpx, "post", lambda *a, **k: sent.append(k) or None)
    out = mcp_server.send_game_chat("sess_x", f"psst {PHRASE}", agent_id="leaky")
    assert out.get("success") is False and "recovery phrase" in out["error"]
    assert not any("junk" in json.dumps(k, default=str) for k in sent), "the phrase never left"


def test_an_operator_can_turn_it_off(monkeypatch):
    monkeypatch.setenv("EMPORIA_OUTBOUND_GUARD", "0")
    secret_guard.check("", PHRASE)
