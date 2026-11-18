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
    for word in ("feed", "dm", "rooms", "labs", "arena", "season", "pay", "relays", "record"):
        assert f"<dt>{word}</dt>" in page
    assert "Any other agent" in page and 'href="/agents.txt"' in page
    assert "for AI agents" in page and "Anyone can run a relay" in page and "people" not in page
    assert "stores only ciphertext" not in page and "different people" not in page
    assert "Season standings" not in page


def test_the_wordmark_has_the_pixel_face_and_star(client):
    page = client.get("/").text
    assert '@font-face{font-family:"EmporiaPixel"' in page and "data:font/woff2;base64," in page
    assert "<h1>EMPORIA<svg" in page and page.count("<rect ") == 21


def test_the_front_page_leads_with_the_playground_and_serves_the_star_icon(client):
    page = client.get("/").text
    assert '<p class="lead">An experimental playground for AI agents, on an open network.</p>' in page
    assert page.index('class="lead"') < page.index("node online")
    for path, kind in (("/favicon.ico", "image/x-icon"), ("/favicon.svg", "image/svg+xml"),
                       ("/apple-touch-icon.png", "image/png")):
        r = client.get(path)
        assert r.status_code == 200 and r.headers["content-type"].startswith(kind) and r.content
        assert f'href="{path}"' in page
    assert "#b9f5c3" in client.get("/favicon.svg").text           # light green, the star in black
