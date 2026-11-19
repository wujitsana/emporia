"""`link-8004 --browser`: the one-shot local signing page. A real local HTTP client against the real server; the page's
script is run in Node against a fake wallet when Node is there. No real wallet, no network beyond 127.0.0.1."""
import http.client
import json
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path

import pytest

from emporia import agent_card_cli as card
from emporia import browser_sign as bs
from emporia import erc8004, mcp_server
from emporia.networks import ERC8004_REGISTRIES
from tests.test_chain_link_cli import STABLE, _owned_by_eoa, chain, target  # noqa: F401  (fixtures)
from tests.test_erc8004_verify import AGENT, keypair, needs_lib

SEPOLIA = "eip155:84532"
HARNESS = Path(__file__).parent / "fixtures" / "browser_sign" / "harness.mjs"


def typed(signer="0x" + "11" * 20, issued=1_700_000_000):
    return erc8004.typed_data_json(erc8004.agent_link_typed_data(
        84532, ERC8004_REGISTRIES[SEPOLIA], AGENT, signer, STABLE, issued, "kestrel"))


def page(td=None, **kw):
    td = td or typed()
    return bs.SigningPage(td, chain_label="Base Sepolia (testnet)", agent_name="kestrel", **kw).start()


def request(p, method, path, body=None, headers=None, host=None):
    c = http.client.HTTPConnection(bs.HOST, p.port, timeout=5)
    h = {"Host": host or f"{bs.HOST}:{p.port}", **(headers or {})}
    if body is not None and "Content-Type" not in h:
        h["Content-Type"] = "application/json"
    c.request(method, path, body=json.dumps(body) if isinstance(body, (dict, list)) else body, headers=h)
    r = c.getresponse()
    out = (r.status, dict((k.lower(), v) for k, v in r.getheaders()), r.read())
    c.close()
    return out


def serve(p):
    """wait() in a thread; returns (thread, box) where box gets the result or the error."""
    box: dict = {}

    def run():
        try:
            box["result"] = p.wait()
        except Exception as e:  # noqa: BLE001
            box["error"] = e
    t = threading.Thread(target=run, daemon=True)
    t.start()
    return t, box


SIG = "0x" + "ab" * 65


# -- the server -------------------------------------------------------------------------------------------------
def test_it_listens_on_loopback_only_at_a_random_port_with_the_token_in_the_path():
    a, b = page(), page()
    try:
        assert a.url.startswith("http://127.0.0.1:") and a.url.endswith("/" + a.token)
        assert a._server.server_address[0] == "127.0.0.1"
        assert a.token != b.token and len(a.token) >= 30
    finally:
        a.close(), b.close()


def test_only_the_right_token_gets_the_page_and_everything_else_is_404():
    p = page()
    t, box = serve(p)
    try:
        assert request(p, "GET", "/" + p.token)[0] == 200
        assert request(p, "GET", "/" + p.token + "/")[0] == 200
        for path in ("/", "/wrong", "/" + p.token + "x", "/" + p.token[:-1], "/" + p.token + "/other", "/favicon.ico",
                     "/" + p.token + "/sign"):
            assert request(p, "GET", path)[0] == 404, path
        for path in ("/", "/wrong", "/wrong/sign", "/" + p.token):
            assert request(p, "POST", path, {"signature": SIG, "account": "0x" + "11" * 20})[0] == 404, path
        assert "result" not in box and p.result is None, "nothing wrong used the one signature"
    finally:
        p.timeout = 0
        t.join(3)


def test_a_foreign_host_header_is_refused_so_a_rebinding_page_gets_nothing():
    p = page()
    t, _ = serve(p)
    try:
        assert request(p, "GET", "/" + p.token, host="evil.example")[0] == 404
        for bad in (f"localhost.evil.example:{p.port}", f"127.0.0.1:{p.port + 1}", f"127.0.0.1.evil.example:{p.port}",
                    "127.0.0.1", f"[::1]:{p.port}", f"172.20.0.2:{p.port}"):
            assert request(p, "GET", "/" + p.token, host=bad)[0] == 404, bad
        assert request(p, "GET", "/" + p.token, host=f"localhost:{p.port}")[0] == 200      # the tunnel's own name
    finally:
        p.timeout = 0
        t.join(3)


def test_the_page_carries_a_strict_csp_with_its_nonce_and_the_other_headers():
    p = page()
    t, _ = serve(p)
    try:
        status, h, body = request(p, "GET", "/" + p.token)
        html = body.decode()
        nonce = re.search(r"script-src 'nonce-([^']+)'", h["content-security-policy"]).group(1)
        assert h["content-security-policy"] == (
            f"default-src 'none'; script-src 'nonce-{nonce}'; style-src 'nonce-{nonce}'; connect-src 'self'; "
            "base-uri 'none'; form-action 'none'; frame-ancestors 'none'")
        assert html.count(f'nonce="{nonce}"') == 2, "the one script and the one style"
        assert "<script>" not in html and "<style>" not in html and "onclick" not in html
        assert h["x-content-type-options"] == "nosniff" and h["referrer-policy"] == "no-referrer"
        assert h["cache-control"] == "no-store" and h["content-type"].startswith("text/html")
        # a fresh nonce per load, and nothing is loaded from anywhere else
        assert request(p, "GET", "/" + p.token)[1]["content-security-policy"] != h["content-security-policy"]
        loaded = re.sub(r'<script type="application/json" id="page-data">.*?</script>', "", html, flags=re.S)
        assert not re.search(r"""(src|href)\s*=\s*["']?(https?:)?//""", loaded) and "https://" not in loaded
        assert request(p, "GET", "/nope")[1]["content-security-policy"] == "default-src 'none'"
    finally:
        p.timeout = 0
        t.join(3)


def test_the_page_says_what_is_signed_shows_the_typed_data_and_makes_the_wallet_calls():
    p = page(typed(signer="0x" + "11" * 20))
    t, _ = serve(p)
    try:
        html = request(p, "GET", "/" + p.token)[2].decode()
        assert "moves no funds and grants no permission" in html
        assert f"agent #{AGENT}" in html and "kestrel" in html and "Base Sepolia (testnet)" in html
        assert "Emporia agent link" in html and "0x" + STABLE in html     # the typed data is shown
        for call in ("eip6963:announceProvider", "eip6963:requestProvider", "window.ethereum", "eth_requestAccounts",
                     "eth_signTypedData_v4", "wallet_switchEthereumChain", "eth_chainId"):
            assert call in html, call
        assert "No wallet was found" in html
    finally:
        p.timeout = 0
        t.join(3)


def test_the_agent_name_is_escaped_in_the_page():
    p = bs.SigningPage(typed(), chain_label="Base", agent_name="<img src=x onerror=alert(1)>").start()
    t, _ = serve(p)
    try:
        html = request(p, "GET", "/" + p.token)[2].decode()
        assert "<img" not in html and "&lt;img" in html
    finally:
        p.timeout = 0
        t.join(3)


def test_one_good_signature_is_accepted_then_the_server_is_gone():
    signer = "0x" + "11" * 20
    p = page(typed(signer))
    t, box = serve(p)
    status, _, body = request(p, "POST", "/" + p.token + "/sign", {"signature": SIG, "account": signer},
                              headers={"Origin": f"http://127.0.0.1:{p.port}"})
    assert status == 200 and json.loads(body) == {"ok": True}
    t.join(5)
    assert box["result"] == {"signature": SIG, "account": signer}
    with pytest.raises(OSError):                                 # a second POST finds nothing listening
        request(p, "POST", "/" + p.token + "/sign", {"signature": SIG, "account": signer})
    with pytest.raises(OSError):
        request(p, "GET", "/" + p.token)


def test_a_signature_without_0x_is_given_one_and_the_account_may_differ_in_case():
    signer = "0x" + "ab" * 20
    p = page(typed(signer))
    t, box = serve(p)
    assert request(p, "POST", "/" + p.token + "/sign", {"signature": SIG[2:], "account": signer.upper().replace("0X", "0x")})[0] == 200
    t.join(5)
    assert box["result"]["signature"] == SIG


@pytest.mark.parametrize("body,headers,code", [
    ({"signature": "zz", "account": "0x" + "11" * 20}, {}, 400),
    ({"signature": "0xabc", "account": "0x" + "11" * 20}, {}, 400),                   # odd length
    ({"signature": "0x" + "ab" * 4097, "account": "0x" + "11" * 20}, {}, 400),        # over 4096 bytes
    ({"signature": SIG, "account": "0x" + "22" * 20}, {}, 400),                       # not the signer
    ({"signature": SIG}, {}, 400),
    ({"signature": 5, "account": "0x" + "11" * 20}, {}, 400),
    ("not json", {}, 400),
    ({"signature": SIG, "account": "0x" + "11" * 20}, {"Origin": "http://evil.example"}, 403),
    ({"signature": SIG, "account": "0x" + "11" * 20}, {"Content-Type": "text/plain"}, 415),
])
def test_a_bad_post_is_refused_and_does_not_use_the_one_signature(body, headers, code):
    p = page(typed("0x" + "11" * 20))
    t, box = serve(p)
    try:
        assert request(p, "POST", "/" + p.token + "/sign", body, headers=headers)[0] == code
        assert p.result is None and "result" not in box
        assert request(p, "POST", "/" + p.token + "/sign", {"signature": SIG, "account": "0x" + "11" * 20})[0] == 200
        t.join(5)
        assert box["result"]["signature"] == SIG
    finally:
        p.timeout = 0


def test_it_shuts_down_on_its_own_when_the_time_is_up():
    p = page(timeout=0.5)
    t0 = time.monotonic()
    with pytest.raises(bs.BrowserSignError, match="no signature arrived"):
        p.wait()
    assert time.monotonic() - t0 < 3
    with pytest.raises(OSError):
        request(p, "GET", "/" + p.token)


def test_the_default_lifetime_is_ten_minutes_and_ctrl_c_closes_it(monkeypatch):
    assert bs.TIMEOUT == 600
    p = page()
    monkeypatch.setattr(p._server, "handle_request", lambda: (_ for _ in ()).throw(KeyboardInterrupt()))
    with pytest.raises(KeyboardInterrupt):
        p.wait()
    with pytest.raises(OSError):
        request(p, "GET", "/" + p.token)


# -- the page's script, against a fake wallet (Node) -------------------------------------------------------------
needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")


def run_page(tmp_path, wallet, announce=True, post="ok", signer="0x" + "11" * 20):
    td = typed(signer)
    p = page(td)
    t, _ = serve(p)
    try:
        html = request(p, "GET", "/" + p.token)[2].decode()
    finally:
        p.timeout = 0
        t.join(3)
    script = re.search(r'<script nonce="[^"]+">(.*)</script>', html, re.S).group(1)
    f = tmp_path / "page.js"
    f.write_text(script)
    scenario = {"typed_data": td, "chain_label": "Base Sepolia (testnet)", "wallet": wallet, "announce": announce, "post": post}
    out = subprocess.run(["node", str(HARNESS), str(f), json.dumps(scenario)], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout.strip().splitlines()[-1]), td


SIGNER = "0x" + "11" * 20


@needs_node
def test_on_the_wrong_chain_the_page_asks_the_wallet_to_switch_then_signs_and_posts(tmp_path):
    log, td = run_page(tmp_path, {"chain": 1, "accounts": [SIGNER.upper().replace("0X", "0x")], "switch": "ok", "sign": "ok"})
    assert log["calls"] == ["eth_requestAccounts", "eth_chainId", "wallet_switchEthereumChain", "eth_chainId", "eth_signTypedData_v4"]
    assert log["switched_to"] == 84532
    assert log["signed"]["account"] == SIGNER and log["signed"]["typed"] == td
    (post,) = log["posts"]
    assert post["url"] == "/TOKEN/sign" and post["method"] == "POST" and post["body"] == {"signature": "0x" + "ab" * 65, "account": SIGNER}
    assert log["statusClass"] == "good" and "Signed" in log["status"]


@needs_node
def test_on_the_right_chain_it_does_not_switch(tmp_path):
    log, _ = run_page(tmp_path, {"chain": 84532, "accounts": [SIGNER], "switch": "refuse", "sign": "ok"})
    assert "wallet_switchEthereumChain" not in log["calls"] and len(log["posts"]) == 1


@needs_node
@pytest.mark.parametrize("how,words", [("refuse", "you declined"), ("unknown", "does not know that chain")])
def test_a_wallet_that_will_not_switch_is_reported_and_nothing_is_signed_or_sent(tmp_path, how, words):
    log, _ = run_page(tmp_path, {"chain": 1, "accounts": [SIGNER], "switch": how, "sign": "ok"})
    assert "eth_signTypedData_v4" not in log["calls"] and log["posts"] == []
    assert "did not switch to Base Sepolia (testnet)" in log["status"] and words in log["status"]
    assert log["statusClass"] == "bad" and log["signDisabled"] is False, "the person may try again"


@needs_node
def test_an_account_that_is_not_the_signer_is_never_asked_to_sign(tmp_path):
    log, _ = run_page(tmp_path, {"chain": 84532, "accounts": ["0x" + "22" * 20], "switch": "ok", "sign": "ok"})
    assert "eth_signTypedData_v4" not in log["calls"] and log["posts"] == []
    assert SIGNER in log["status"] and log["statusClass"] == "bad"


@needs_node
def test_declining_to_sign_sends_nothing(tmp_path):
    log, _ = run_page(tmp_path, {"chain": 84532, "accounts": [SIGNER], "switch": "ok", "sign": "refuse"})
    assert log["posts"] == [] and "declined" in log["status"] and log["signDisabled"] is False


@needs_node
def test_without_any_wallet_the_page_says_so_and_cannot_sign(tmp_path):
    log, _ = run_page(tmp_path, None)
    assert log["signDisabledBeforeClick"] is True and log["noWalletHidden"] is False and log["posts"] == []


@needs_node
def test_a_wallet_that_only_sets_window_ethereum_is_found(tmp_path):
    log, _ = run_page(tmp_path, {"chain": 84532, "accounts": [SIGNER], "switch": "ok", "sign": "ok"}, announce=False)
    assert len(log["posts"]) == 1 and log["noWalletHidden"] is True


@needs_node
def test_a_refusal_from_the_terminal_is_shown(tmp_path):
    log, _ = run_page(tmp_path, {"chain": 84532, "accounts": [SIGNER], "switch": "ok", "sign": "ok"}, post="bad")
    assert "did not accept it" in log["status"] and log["statusClass"] == "bad"


# -- the command ------------------------------------------------------------------------------------------------
needs = needs_lib


def _wallet(monkeypatch, key, sign_with=None):
    """A stand-in for the person's browser: when the CLI opens the URL, fetch the page and post a signature of the
    typed data embedded in it, made with `sign_with` (default `key`)."""
    from eth_account import Account
    from eth_account.messages import encode_typed_data
    seen = {}

    def opener(url):
        def go():
            u = re.match(r"http://127\.0\.0\.1:(\d+)(/.+)", url)
            port, path = int(u.group(1)), u.group(2)
            c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            c.request("GET", path, headers={"Host": f"127.0.0.1:{port}"})
            body = c.getresponse().read().decode()
            doc = json.loads(re.search(r'id="page-data">(.*?)</script>', body, re.S).group(1))
            td = erc8004.typed_data_from_json(doc["typed_data"])
            sig = Account.sign_message(encode_typed_data(full_message=td), (sign_with or key).key).signature.hex()
            seen["signer"] = doc["typed_data"]["message"]["signer"]
            c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            c.request("POST", path.rstrip("/") + "/sign", body=json.dumps({"signature": "0x" + sig.removeprefix("0x"),
                                                                           "account": seen["signer"]}),
                      headers={"Host": f"127.0.0.1:{port}", "Content-Type": "application/json"})
            seen["status"] = c.getresponse().status
        threading.Thread(target=go, daemon=True).start()
        return True
    monkeypatch.setattr("webbrowser.open", opener)
    return seen


def _post_recorder(monkeypatch):
    sent = {}

    def fake_request(method, url, *, agent_id="", json=None, **kw):
        sent.update(url=url, body=json)

        class R:
            status_code = 200
            def json(self_):
                return {"link": {"status": "claimed"}, "links": []}
        return R()
    monkeypatch.setattr(mcp_server, "_bearer_request", fake_request)
    monkeypatch.setattr(mcp_server, "_base", lambda: "http://relay.test")
    return sent


@needs
def test_browser_flow_prints_the_url_opens_it_verifies_the_signature_and_records_it(target, chain, capsys, monkeypatch):  # noqa: F811
    key, addr = _owned_by_eoa(chain)
    seen = _wallet(monkeypatch, key)
    sent = _post_recorder(monkeypatch)
    assert card.link_8004(target.name, "base-sepolia", str(AGENT), yes=True, browser=True) == 0
    out = capsys.readouterr().out
    assert re.search(r"http://127\.0\.0\.1:\d+/[\w-]{30,}", out) and "Ctrl-C" in out and "10 minutes" in out
    assert "Checked here at block 123: owner" in out and "On " in out
    assert seen["status"] == 200 and seen["signer"].lower() == addr.lower()
    body = sent["body"]
    assert body["signer"].lower() == addr.lower() and body["agent_id"] == str(AGENT) and len(body["wallet_sig"]) >= 130
    assert sent["url"].endswith(f"/agents/{target.name}/links/erc8004")


@needs
def test_browser_flow_a_signature_by_the_wrong_wallet_records_nothing(target, chain, capsys, monkeypatch):  # noqa: F811
    key, addr = _owned_by_eoa(chain)
    other, _ = keypair()
    _wallet(monkeypatch, key, sign_with=other)
    monkeypatch.setattr(mcp_server, "_bearer_request", lambda *a, **k: pytest.fail("posted"))
    assert card.link_8004(target.name, "base-sepolia", str(AGENT), yes=True, browser=True) == 1
    assert "not recorded" in capsys.readouterr().out


@needs
def test_browser_flow_times_out_and_records_nothing(target, chain, capsys, monkeypatch):  # noqa: F811
    _owned_by_eoa(chain)
    monkeypatch.setattr("webbrowser.open", lambda url: False)            # nobody opens it
    monkeypatch.setattr(mcp_server, "_bearer_request", lambda *a, **k: pytest.fail("posted"))
    assert card._link_in_browser(target, target.name, "base-sepolia", str(AGENT), "", True, timeout=0.5) == 1
    assert "no signature arrived" in capsys.readouterr().out


@needs
def test_browser_flow_serves_nothing_when_the_chain_does_not_back_the_signer(target, chain, capsys, monkeypatch):  # noqa: F811
    chain["chain"].owners[AGENT] = keypair()[1]
    _, stranger = keypair()
    monkeypatch.setattr("webbrowser.open", lambda url: pytest.fail("a page was opened"))
    assert card.link_8004(target.name, "base-sepolia", str(AGENT), stranger, yes=True, browser=True) == 1
    assert "nothing to sign" in capsys.readouterr().out


@needs
def test_browser_flow_refuses_a_signature_flag_and_needs_a_terminal_to_ask(target, chain, capsys, monkeypatch):  # noqa: F811
    _owned_by_eoa(chain)
    assert card.link_8004(target.name, "base-sepolia", str(AGENT), signature="0xab", yes=True, browser=True) == 1
    assert "do not give --signature" in capsys.readouterr().out
    monkeypatch.setattr(card._cli(), "_persons_terminal", lambda: False)
    monkeypatch.setattr("webbrowser.open", lambda url: pytest.fail("a page was opened"))
    assert card.link_8004(target.name, "base-sepolia", str(AGENT), browser=True) == 1
    assert "no terminal to ask in" in capsys.readouterr().out


@needs
def test_browser_flow_is_refused_while_an_unattended_turn_is_live(target, chain, capsys):  # noqa: F811
    target.live = "relay"
    assert card.link_8004(target.name, "base-sepolia", str(AGENT), browser=True) == 1
    assert "person's to change" in capsys.readouterr().out


def test_an_idle_connection_does_not_hold_the_server_past_its_time_limit():
    import socket
    p = page(timeout=1.0)
    idle = socket.create_connection((bs.HOST, p.port))          # a browser's spare pre-connection: sends nothing
    try:
        t0 = time.monotonic()
        with pytest.raises(bs.BrowserSignError, match="no signature arrived"):
            p.wait()
        assert time.monotonic() - t0 < 4
    finally:
        idle.close()


def test_an_idle_connection_does_not_stop_the_page_or_the_signature(monkeypatch):
    import socket
    p = page(timeout=10.0)
    idle = socket.create_connection((bs.HOST, p.port))
    try:
        t, box = serve(p)
        assert request(p, "GET", "/" + p.token)[0] == 200
        status, _, _ = request(p, "POST", "/" + p.token + "/sign", {"signature": SIG, "account": p._signer})
        t.join(5)
        assert status == 200 and box.get("result", {}).get("signature") == SIG
    finally:
        idle.close()


def test_a_connection_that_stops_mid_request_is_dropped_after_the_socket_timeout(monkeypatch):
    import socket
    monkeypatch.setattr(bs, "CONN_TIMEOUT", 0.5)
    p = page(timeout=3.0)
    t, _box = serve(p)
    s = socket.create_connection((bs.HOST, p.port))
    try:
        s.sendall(b"POST / HTTP/1.1\r\nHost: x\r\nContent-Length: 50\r\n")     # never finishes
        s.settimeout(5)
        assert s.recv(10) == b""                                              # the server hung up on it
    finally:
        s.close()
        t.join(6)
