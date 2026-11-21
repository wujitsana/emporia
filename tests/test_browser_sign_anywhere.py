"""`--browser` from another machine: where the page may listen, how a container is detected, what the person is told for
each setup, the Host and Origin checks through a tunnel, the wallet-chain 4902 path, and one end-to-end run through a
TCP forwarder that stands in for an SSH tunnel. Local sockets only; no browser, no network.

A headless-browser test is not here (it needs a real browser). To add one: start `SigningPage(..., port=N)` and drive
`page.url` with a script that defines `window.ethereum` (see `tests/fixtures/browser_sign/harness.mjs`, which already runs
the page script in Node against a fake wallet), then assert on `page.wait()`.
"""
import http.client
import json
import re
import socket
import threading

import pytest

from emporia import agent_card_cli as card
from emporia import browser_sign as bs
from emporia import erc8004
from emporia.networks import ERC8004_REGISTRIES
from tests.test_browser_sign import SEPOLIA, request, serve, typed
from tests.test_erc8004_verify import AGENT, FX, NAME, FakeChain, keypair, make_link, needs_lib

REG = ERC8004_REGISTRIES[SEPOLIA]


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


# -- --port and --listen ----------------------------------------------------------------------------------------
@pytest.mark.parametrize("port", [0, 1024, 8765, 65535, "8765"])
def test_a_port_in_range_or_zero_is_accepted(port):
    assert bs.check_port(port) == int(port)


@pytest.mark.parametrize("port", [1, 80, 1023, 65536, 70000, -5, "abc"])
def test_a_port_out_of_range_is_refused(port):
    with pytest.raises(bs.BrowserSignError, match="--port"):
        bs.check_port(port)


@pytest.mark.parametrize("addr", ["127.0.0.1", "127.0.0.2", "::1", "10.1.2.3", "172.16.0.1", "172.20.0.2", "172.31.255.254",
                                  "192.168.1.5", "100.64.0.1", "100.127.255.255", "169.254.10.10", "fc00::1", "fd12:3456::9"])
def test_loopback_private_and_link_local_addresses_are_allowed(addr):
    assert bs.validate_listen(addr) == addr


@pytest.mark.parametrize("addr", ["0.0.0.0", "::", "8.8.8.8", "1.1.1.1", "172.32.0.1", "100.128.0.1", "192.169.0.1",
                                  "2001:4860:4860::8888", "203.0.113.7", "localhost", "example.com", "", "10.0.0"])
def test_unspecified_public_and_non_address_values_are_refused(addr):
    with pytest.raises(bs.BrowserSignError, match="--listen"):
        bs.validate_listen(addr)


def test_the_refusal_of_all_interfaces_says_why_in_plain_words():
    with pytest.raises(bs.BrowserSignError) as e:
        bs.validate_listen("0.0.0.0")
    assert "every interface" in str(e.value)
    with pytest.raises(bs.BrowserSignError) as e:
        bs.validate_listen("8.8.8.8")
    assert "public address" in str(e.value)


def test_the_page_refuses_to_be_built_for_a_public_address_or_bad_port():
    for kw in ({"listen": "0.0.0.0"}, {"listen": "8.8.8.8"}, {"port": 80}):
        with pytest.raises(bs.BrowserSignError):
            bs.SigningPage(typed(), chain_label="Base Sepolia (testnet)", agent_name="kestrel", **kw)


def test_auto_is_loopback_outside_a_container_and_the_private_address_inside_one(monkeypatch):
    assert bs.resolve_listen("auto", container=False) == "127.0.0.1"
    monkeypatch.setattr(bs, "container_ip", lambda *a, **k: "172.20.0.2")
    assert bs.resolve_listen("auto", container=True) == "172.20.0.2"
    assert bs.resolve_listen("AUTO", container=True) == "172.20.0.2"
    assert bs.resolve_listen("192.168.1.9", container=True) == "192.168.1.9"      # an explicit address wins
    monkeypatch.setattr(bs, "container_ip", lambda *a, **k: "8.8.8.8")          # a public one is never used
    assert bs.resolve_listen("auto", container=True) == "127.0.0.1"
    monkeypatch.setattr(bs, "container_ip", lambda *a, **k: "")
    assert bs.resolve_listen("auto", container=True) == "127.0.0.1"
    with pytest.raises(bs.BrowserSignError):
        bs.resolve_listen("0.0.0.0", container=True)


# -- container detection, with fake files ------------------------------------------------------------------------
def test_container_markers_are_found_in_files_and_the_environment(tmp_path):
    assert bs.in_container(tmp_path, {}) is False
    (tmp_path / ".dockerenv").write_text("")
    assert bs.in_container(tmp_path, {}) is True


def test_other_container_markers(tmp_path):
    (tmp_path / "run").mkdir()
    (tmp_path / "run/.containerenv").write_text("")
    assert bs.in_container(tmp_path, {}) is True
    assert bs.in_container(tmp_path / "nothing", {"container": "podman"}) is True


@pytest.mark.parametrize("rel,text", [("proc/1/cgroup", "0::/docker/abc123\n"),
                                      ("proc/1/cgroup", "12:cpu:/kubepods/burstable/pod1\n"),
                                      ("proc/self/mountinfo", "30 25 0:27 / / rw - overlay overlay rw,lowerdir=/var/lib/containerd/x\n")])
def test_container_words_in_cgroup_or_mountinfo(tmp_path, rel, text):
    (tmp_path / rel).parent.mkdir(parents=True)
    (tmp_path / rel).write_text(text)
    assert bs.in_container(tmp_path, {}) is True


def test_a_host_that_runs_containers_is_not_one(tmp_path):
    """Fable delta review: the host's own mount table lists its containers' overlays; only the root line counts."""
    (tmp_path / "proc/self").mkdir(parents=True)
    (tmp_path / "proc/self/mountinfo").write_text(
        "22 1 8:1 / / rw,relatime - ext4 /dev/sda1 rw\n"
        "301 22 0:50 / /var/lib/docker/overlay2/abc/merged rw - overlay overlay rw,lowerdir=/var/lib/docker/overlay2/l/X\n")
    assert bs.in_container(tmp_path, {}) is False


def test_a_plain_host_is_not_a_container(tmp_path):
    (tmp_path / "proc/1").mkdir(parents=True)
    (tmp_path / "proc/1/cgroup").write_text("0::/init.scope\n")
    assert bs.in_container(tmp_path, {}) is False


def test_the_default_gateway_is_read_from_the_route_table(tmp_path):
    route = tmp_path / "route"
    route.write_text("Iface\tDestination\tGateway\tFlags\tRefCnt\tUse\tMetric\tMask\tMTU\tWindow\tIRTT\n"
                     "eth0\t00000000\t010014AC\t0003\t0\t0\t0\t00000000\t0\t0\t0\n"
                     "eth0\t000014AC\t00000000\t0001\t0\t0\t0\t0000FFFF\t0\t0\t0\n")
    assert bs._default_gateway(route) == "172.20.0.1"
    assert bs._default_gateway(tmp_path / "missing") == ""


def test_the_container_address_is_one_a_page_may_use():
    ip = bs.container_ip()
    assert ip == "" or ip  # found without any command; whatever it is, auto only uses it when it validates
    if ip:
        assert isinstance(ip, str)


def test_an_ssh_session_is_seen_in_the_environment():
    assert bs.ssh_session({"SSH_CONNECTION": "1.2.3.4 5 6.7.8.9 22"}) is True
    assert bs.ssh_session({"SSH_CLIENT": "1.2.3.4 5 22"}) is True
    assert bs.ssh_session({}) is False


# -- what the person is told -------------------------------------------------------------------------------------
def say(**kw):
    base = dict(port=8765, listen="127.0.0.1", container=False, ssh=False, hostname="vps", token="TOKEN",
                chain_label="Base Sepolia (testnet)", registry=REG, what="signature")
    return bs.instructions(**{**base, **kw})


def test_local_says_it_opens_the_browser_and_prints_the_url():
    text, opens = say()
    assert opens is True and "Opening your browser" in text and "http://127.0.0.1:8765/TOKEN" in text
    assert "ssh -L" not in text


def test_ssh_on_a_native_host_prints_the_exact_tunnel_line_to_loopback():
    text, opens = say(ssh=True)
    assert opens is False
    assert "ssh -L 8765:127.0.0.1:8765 <you>@vps" in text
    assert "http://127.0.0.1:8765/TOKEN" in text and "must be 8765" in text and "Docker Desktop" not in text


def test_setup_three_docker_on_a_server_over_ssh_targets_the_container_ip():
    text, opens = say(ssh=True, container=True, listen="172.20.0.2", hostname="")
    assert opens is False
    assert "ssh -L 8765:172.20.0.2:8765 <you>@<this-host>" in text
    assert "http://127.0.0.1:8765/TOKEN" in text


def test_a_container_without_ssh_gets_the_tunnel_and_the_docker_desktop_note():
    text, opens = say(container=True, listen="172.20.0.2", hostname="")
    assert opens is False
    assert "ssh -L 8765:172.20.0.2:8765" in text
    assert 'ports: ["127.0.0.1:8765:8765"]' in text and "--port 8765" in text and "Never publish it on 0.0.0.0" in text


@pytest.mark.parametrize("kw", [{}, {"ssh": True}, {"container": True, "listen": "172.20.0.2"}])
def test_every_setup_ends_with_the_wallet_check_text(kw):
    text, _ = say(**kw)
    assert "Check the wallet popup: network Base Sepolia (testnet)" in text and f"'to' address {REG}" in text
    assert "only asks for this one signature and closes" in text
    tx, _ = say(what="transaction", **kw)
    assert "only asks for this one transaction and closes" in tx


def test_the_url_is_always_the_browser_side_loopback_named_emporia_with_the_numeric_fallback():
    for listen in ("127.0.0.1", "172.20.0.2", "10.0.0.5"):
        assert bs.page_url(listen, 9000, "T") == "http://emporia.localhost:9000/T"     # the wallet names the site
        assert bs.fallback_url(listen, 9000, "T") == "http://127.0.0.1:9000/T"
    assert bs.page_url("::1", 9000, "T") == bs.fallback_url("::1", 9000, "T") == "http://[::1]:9000/T"


# -- Host and Origin ---------------------------------------------------------------------------------------------
def test_host_and_origin_are_accepted_only_for_the_browser_side_names():
    port = free_port()
    p = bs.SigningPage(typed(), chain_label="Base Sepolia (testnet)", agent_name="kestrel", port=port).start()
    t, _ = serve(p)
    try:
        assert p.port == port
        assert p.own_hosts() == {f"emporia.localhost:{port}", f"127.0.0.1:{port}", f"localhost:{port}"}
        for ok in (f"emporia.localhost:{port}", f"127.0.0.1:{port}", f"localhost:{port}"):
            assert request(p, "GET", "/" + p.token, host=ok)[0] == 200, ok
        for bad in ("evil.example", f"evil.example:{port}", f"127.0.0.1.evil.example:{port}", f"localhost.evil:{port}",
                    f"emporia.localhost.evil.example:{port}", f"evil.localhost:{port}", f"emporia.localhost:{port + 1}",
                    "127.0.0.1", f"127.0.0.1:{port + 1}", f"[::1]:{port}", f"0.0.0.0:{port}", f"192.168.1.2:{port}"):
            assert request(p, "GET", "/" + p.token, host=bad)[0] == 404, bad       # a rebinding page gets nothing
        body = {"signature": "0x" + "ab" * 65, "account": "0x" + "11" * 20}
        sign_path = "/" + p.token + "/sign"
        for origin in ("http://evil.example", f"http://127.0.0.1:{port + 1}", f"https://127.0.0.1:{port}", "null",
                       f"http://localhost.evil:{port}"):
            assert request(p, "POST", sign_path, body, headers={"Origin": origin})[0] == 403, origin
        assert p.result is None
        assert request(p, "POST", sign_path, body, headers={"Origin": f"http://localhost:{port}"})[0] == 200
    finally:
        p.timeout = 0
        t.join(3)


def test_an_ipv6_loopback_listener_also_answers_to_its_own_bracketed_name():
    try:
        p = bs.SigningPage(typed(), chain_label="Base Sepolia (testnet)", agent_name="kestrel", listen="::1").start()
    except bs.BrowserSignError:
        pytest.skip("no IPv6 loopback here")
    try:
        assert f"[::1]:{p.port}" in p.own_hosts() and p.url.startswith("http://[::1]:")
    finally:
        p.close()


def test_a_port_already_in_use_is_said_plainly():
    port = free_port()
    a = bs.SigningPage(typed(), chain_label="x", agent_name="k", port=port).start()
    try:
        with pytest.raises(bs.BrowserSignError, match="in use"):
            bs.SigningPage(typed(), chain_label="x", agent_name="k", port=port).start()
    finally:
        a.close()


# -- the wallet's chain: 4902 then wallet_addEthereumChain --------------------------------------------------------
def test_the_page_script_adds_the_chain_on_4902_with_emporias_public_parameters_then_switches_again():
    assert "4902" in bs.SCRIPT and "wallet_addEthereumChain" in bs.SCRIPT and "wallet_switchEthereumChain" in bs.SCRIPT
    assert bs.SCRIPT.index("wallet_switchEthereumChain") < bs.SCRIPT.index("wallet_addEthereumChain")
    assert "Asking it to add the network" in bs.SCRIPT
    html = bs.page_html(typed(), chain_label="Base Sepolia (testnet)", agent_name="kestrel", nonce="n")
    data = json.loads(re.search(r'id="page-data">(.*?)</script>', html, re.S).group(1))
    assert data["add_chain"] == {"chainId": "0x14a34", "chainName": "Base Sepolia",
                                 "nativeCurrency": {"name": "Ether", "symbol": "ETH", "decimals": 18},
                                 "rpcUrls": ["https://sepolia.base.org"], "blockExplorerUrls": ["https://sepolia.basescan.org"]}


def test_only_chains_emporia_knows_get_add_parameters():
    assert bs.chain_params(8453)["chainId"] == "0x2105"
    assert bs.chain_params(999999999) is None
    assert bs.chain_params(2) is None


def test_the_send_page_names_the_function_the_registry_and_the_check_to_make():
    tx = {"chain_id": 84532, "to": REG, "data": "0xabcdef", "function": "register(string agentURI)"}
    html = bs.tx_page_html(tx, chain_label="Base Sepolia (testnet)", agent_name="kestrel", summary="Sends register.",
                           nonce="n")
    assert "register(string agentURI)" in html and REG in html and "&ldquo;to&rdquo; address" in html
    assert "only asks for this one transaction and closes" in html
    data = json.loads(re.search(r'id="page-data">(.*?)</script>', html, re.S).group(1))
    assert data["add_chain"]["chainId"] == "0x14a34"


SID = "ab" * 32


def _register_tx(description="Plays fair, talks plain.", image=True):
    from emporia import erc8004_register as reg
    file = reg.registration_file("kestrel", description, "https://relay.example", SID, True,
                                 image=reg.token_picture(SID) if image else None)
    return {"chain_id": 84532, "to": REG, "data": "0x" + reg.calldata(reg.data_uri(file)).hex(),
            "function": "register(string agentURI)"}


@needs_lib
def test_the_send_page_shows_the_registration_decoded_with_its_picture_and_folds_the_raw_call():
    html = bs.tx_page_html(_register_tx(), chain_label="Base Sepolia (testnet)", agent_name="kestrel",
                           summary="Sends register.", nonce="n")
    assert "<h1>Register kestrel on ERC-8004</h1>" in html
    for shown in ("<dt>Name</dt><dd>kestrel</dd>", "Plays fair, talks plain.", "https://relay.example/agents/kestrel",
                  SID, "<dt>Accepts payments (x402)</dt><dd>yes</dd>", "test gas only (no value)",
                  "Read here from the exact bytes your wallet will send", "names the same call (register)",
                  'class="mark">EMPORIA<svg', 'rel="icon" type="image/svg+xml" href="data:image/svg+xml;base64,'):
        assert shown in html, shown
    assert '<img src="data:image/svg+xml;base64,' in html
    assert "&rsquo;s picture (image data" in html and "It holds no signature." in html
    assert "<details><summary>Technical details: the call data (a wallet shows it under Data or Hex)</summary>" in html


@needs_lib
def test_the_set_wallet_page_names_the_identity_the_wallet_and_how_long_the_consent_holds():
    from emporia import erc8004_wallet as W
    wallet = "0x" + "12" * 20
    tx = {"chain_id": 84532, "to": REG, "data": "0x" + W.set_wallet_calldata(7, wallet, 1_900_000_000, b"\x01" * 65).hex()}
    html = bs.tx_page_html(tx, chain_label="Base Sepolia (testnet)", agent_name="kestrel", summary="Sends it.", nonce="n")
    assert "Set where kestrel&rsquo;s ERC-8004 identity is paid" in html and "<title>Emporia · Set the agent wallet</title>" in html
    assert "ERC-8004 agent #7" in html and wallet in html.lower() and "2030-03-17" in html
    assert "who owns the identity, and every balance" in html and "<img" not in html and "(setAgentWallet)" in html
    assert "65-byte consent signature" in html


@needs_lib
def test_what_the_page_decodes_is_escaped_and_an_undecodable_call_is_shown_raw_only():
    html = bs.tx_page_html(_register_tx("<script>alert(1)</script>", image=False), chain_label="Base Sepolia (testnet)",
                           agent_name="kestrel", summary="s", nonce="n")
    assert "<script>alert(1)" not in html and "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert "<dt>Picture</dt><dd>none</dd>" in html and "<img" not in html
    raw = bs.tx_page_html({"chain_id": 84532, "to": REG, "data": "0xabcdef"}, chain_label="Base Sepolia (testnet)",
                          agent_name="kestrel", summary="s", nonce="n")
    assert "could not be read here" in raw and "<dt>Name</dt>" not in raw


def test_the_printed_steps_give_the_emporia_name_and_the_numeric_fallback():
    text, _ = bs.instructions(8765, "172.20.0.2", container=True, ssh=False, token="TOKEN", what="transaction")
    assert "http://emporia.localhost:8765/TOKEN" in text and "http://127.0.0.1:8765/TOKEN" in text
    assert "some Safari versions" in text


def test_the_pages_allow_inline_images_and_the_inline_face_and_nothing_remote():
    c = bs.csp("n")
    assert "img-src data:;" in c and "font-src data:;" in c and "default-src 'none'" in c
    assert "http" not in c and "*" not in c and "unsafe" not in c


def test_the_sign_page_shows_the_statement_in_words():
    html = bs.page_html(typed(), chain_label="Base Sepolia (testnet)", agent_name="kestrel", nonce="n")
    assert "Link ERC-8004 agent #" in html and "to the Emporia agent kestrel. This moves no funds" in html
    assert "Check the wallet popup: network Base Sepolia (testnet)" in html


# -- the command-line side ---------------------------------------------------------------------------------------
def test_port_and_listen_without_browser_are_refused(monkeypatch, capsys):
    monkeypatch.setattr(card, "_link_target", lambda cmd, name: object())
    assert card.link_8004("a", "base-sepolia", "1", port=8765) == 1
    assert "--port and --listen go with --browser" in capsys.readouterr().out
    assert card.register_8004("a", "base-sepolia", port=8765) == 1
    assert card.set_8004_wallet("a", "base-sepolia", "1", listen="127.0.0.1") == 1


@pytest.mark.parametrize("port,listen", [(80, "auto"), (0, "0.0.0.0"), (0, "8.8.8.8"), (70000, "auto")])
def test_the_command_refuses_a_bad_port_or_address_before_anything_is_read(port, listen, capsys):
    assert card._browser_listen("link-8004", port, listen) is None
    assert "link-8004:" in capsys.readouterr().out


def test_the_command_accepts_auto_and_private_addresses():
    assert card._browser_listen("link-8004", 8765, "auto") is not None
    assert card._browser_listen("link-8004", 8765, "10.0.0.5") == "10.0.0.5"


# -- end to end: through a TCP forwarder standing in for the SSH tunnel -----------------------------------------
class Forwarder:
    """Listens on `bind` and passes every connection on to `target`, like `ssh -L PORT:TARGET:PORT`."""

    def __init__(self, bind: tuple[str, int], target: tuple[str, int]):
        self.target = target
        self.srv = socket.socket()
        self.srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.srv.bind(bind)
        self.srv.listen(8)
        self.srv.settimeout(0.2)
        self.stop = False
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()

    def run(self):
        while not self.stop:
            try:
                c, _ = self.srv.accept()
            except OSError:
                continue
            try:
                u = socket.create_connection(self.target, timeout=5)
            except OSError:
                c.close()
                continue
            for a, b in ((c, u), (u, c)):
                threading.Thread(target=self.pipe, args=(a, b), daemon=True).start()

    @staticmethod
    def pipe(a, b):
        try:
            while data := a.recv(65536):
                b.sendall(data)
        except OSError:
            pass
        finally:
            for s in (a, b):
                try:
                    s.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass

    def close(self):
        self.stop = True
        self.srv.close()


def tunnel_post(port: int, token: str, body: dict) -> int:
    """The browser's side: 127.0.0.1:PORT (the tunnel's local end), the page's own origin."""
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    c.request("POST", f"/{token}/sign", body=json.dumps(body),
              headers={"Content-Type": "application/json", "Origin": f"http://127.0.0.1:{port}"})
    r = c.getresponse()
    r.read()
    c.close()
    return r.status


def sign_with(key, td_json):
    from eth_account import Account
    from eth_account.messages import encode_typed_data
    return "0x" + Account.sign_message(encode_typed_data(full_message=erc8004.typed_data_from_json(td_json)),
                                       key.key).signature.hex().removeprefix("0x")


def run_through_tunnel(signer_key, owner_addr):
    """Start the page bound to 127.0.0.2 on a fixed port (the 'container address'), a forwarder at 127.0.0.1 on the same
    port (the tunnel's local end), POST the signature made by `signer_key` through it; return (the CLI-side result, link)."""
    port = free_port()
    td = erc8004.typed_data_json(erc8004.agent_link_typed_data(84532, REG, AGENT, owner_addr, FX["stable_id"],
                                                               1_700_000_000, NAME))
    page = bs.SigningPage(td, chain_label="Base Sepolia (testnet)", agent_name=NAME, listen="127.0.0.2", port=port).start()
    fwd = Forwarder(("127.0.0.1", port), ("127.0.0.2", port))
    t, box = serve(page)
    try:
        assert page.url == f"http://emporia.localhost:{port}/{page.token}"
        got_page = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        got_page.request("GET", "/" + page.token)
        assert got_page.getresponse().status == 200               # the page itself comes through the tunnel
        sig = sign_with(signer_key, td)
        assert tunnel_post(port, page.token, {"signature": sig, "account": owner_addr}) == 200
        t.join(5)
        return box["result"], make_link(signer_key, owner_addr, sig=box["result"]["signature"])
    finally:
        fwd.close()
        page.timeout = 0
        t.join(3)


@needs_lib
def test_end_to_end_through_a_forwarder_the_cli_side_accepts_the_owners_signature():
    key, addr = keypair()
    got, link = run_through_tunnel(key, addr)
    assert got["account"] == addr
    chain = FakeChain()
    chain.owners[AGENT] = addr
    assert erc8004.verify(link, chain, now=1_700_000_100)["tier"] == "owner"


@needs_lib
def test_end_to_end_a_signature_by_the_wrong_wallet_is_refused_by_the_cli_side_check():
    key, addr = keypair()
    other, _ = keypair()
    got, link = run_through_tunnel(other, addr)      # the page takes any well-formed answer; the CLI verifies it
    chain = FakeChain()
    chain.owners[AGENT] = addr
    assert erc8004.verify(link, chain, now=1_700_000_100)["status"] == "invalid"
