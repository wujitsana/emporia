"""The relay's front page says what kind of money it handles, from its own settings."""
from emporia import relay_server as rs


def test_a_testnet_relay_says_testnet_only(client, monkeypatch):
    monkeypatch.setattr(rs, "live_money", lambda: [])
    page = client.get("/").text
    assert "node online &middot; testnet &middot; relay holds no money" in page
    assert "{money_line}" not in page


def test_a_relay_with_live_money_does_not_claim_testnet(client, monkeypatch):
    monkeypatch.setattr(rs, "live_money", lambda: ["x402 on eip155:8453"])
    page = client.get("/").text
    assert "testnet" not in page
    assert "live money is on here" in page


def test_the_front_page_says_what_runs_here(client):
    page = client.get("/").text
    for word in ("feed", "dm", "rooms", "labs", "arena", "season", "pay", "trust", "relays", "record"):
        assert f"<dt>{word}</dt>" in page
    assert "Any other agent" in page and 'href="/agents.txt"' in page
    assert "network for agents" in page and "AI agents" not in page and "Anyone can run a relay" in page and "people" not in page
    assert "stores only ciphertext" not in page and "different people" not in page
    assert "Season standings" not in page
    assert "trustless between operators" in page and "agentic commerce" not in page   # the tagline leads; trade is one line


def test_the_wordmark_has_the_pixel_face_and_star(client):
    page = client.get("/").text
    assert '@font-face{font-family:"EmporiaPixel"' in page and "data:font/woff2;base64," in page
    assert "<h1>EMPORIA<svg" in page and page.count("<rect ") == 21


def test_the_front_page_leads_with_the_playground_and_serves_the_star_icon(client):
    page = client.get("/").text
    assert '<p class="lead">Experimental open playground network for agents</p>' in page
    assert page.index('class="lead"') < page.index("node online")
    for path, kind in (("/favicon.ico", "image/x-icon"), ("/favicon.svg", "image/svg+xml"),
                       ("/apple-touch-icon.png", "image/png")):
        r = client.get(path)
        assert r.status_code == 200 and r.headers["content-type"].startswith(kind) and r.content
        assert f'href="{path}"' in page
    assert "#b9f5c3" in client.get("/favicon.svg").text           # light green, the star in black


def test_the_page_claims_only_what_this_relay_does(client, monkeypatch):
    """Fable delta review: the log line and the money lines follow the relay's settings, not fixed text."""
    monkeypatch.setattr(rs, "live_money", lambda: [])
    monkeypatch.delenv("EMPORIA_ESCROW_NETWORK", raising=False)
    monkeypatch.setenv("EMPORIA_TRANSPARENCY_LOG", "off")
    page = client.get("/").text
    assert "keeps no transparency log" in page and 'href="/log/state">/log/state</a>)' not in page
    assert "the relay never holds the money" in page and "nor this relay with money or DMs" in page
    monkeypatch.setenv("EMPORIA_TRANSPARENCY_LOG", "on")
    monkeypatch.setenv("EMPORIA_ESCROW_NETWORK", "eip155:84532")     # set, but escrow is off in 2.1: the page says so
    page = client.get("/").text
    assert "keeps a transparency log anyone can check" in page
    assert "escrow is off here" in page and "escrow is on here" not in page
    assert "nor this relay with money or DMs" in page
    assert "{" not in page.split("<body>")[1]   # no placeholder left
