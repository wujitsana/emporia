"""The relay's front page says what kind of money it handles, from its own settings."""
from emporia import relay_server as rs


def test_a_testnet_relay_says_testnet_only(client, monkeypatch):
    monkeypatch.setattr(rs, "live_money", lambda: [])
    page = client.get("/").text
    assert "testnet only &middot; relay holds no money" in page
    assert "{money_line}" not in page


def test_a_relay_with_live_money_does_not_claim_testnet(client, monkeypatch):
    monkeypatch.setattr(rs, "live_money", lambda: ["x402 on eip155:8453"])
    page = client.get("/").text
    assert "testnet only" not in page
    assert "live money is on here" in page
